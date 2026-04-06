"""メールクライアントのページ表示と JSON API。
メール本文はIMAPサーバーから直接取得する（DBキャッシュなし）。
"""
import json
import logging
from threading import Thread

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .imap_client import ImapConnectionError, MailClient, SmtpConnectionError, test_connection
from .models import EmailClassification, EmailLabel, Label, MailAccount, MailFolder
from .sync import sync_account


def _get_mail_client(account):
    """アカウント種別に応じてメールクライアントを返す"""
    if getattr(account, 'auth_type', None) == 'microsoft_oauth2':
        from .graph_api_client import GraphMailClient
        return GraphMailClient(account)
    return MailClient(account)

logger = logging.getLogger(__name__)

ACCOUNT_FIELDS = [
    'email_address', 'password', 'imap_host', 'imap_port',
    'smtp_host', 'smtp_port', 'username',
]
CONNECTION_TEST_FIELDS = [
    'imap_host', 'imap_port', 'smtp_host', 'smtp_port', 'username', 'password',
]


# =============================
# ページView（テンプレートを返す）
# =============================

class MailIndexView(LoginRequiredMixin, TemplateView):
    """メインページ"""
    template_name = 'mailer/index.html'

    def get(self, request, *args, **kwargs):
        has_account = MailAccount.objects.filter(user=request.user, is_active=True).exists()
        if not has_account:
            return redirect('mailer:setup')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        accounts = list(MailAccount.objects.filter(user=self.request.user, is_active=True))
        ctx['accounts_json'] = json.dumps([_serialize_account(a) for a in accounts])

        initial_account = accounts[0] if accounts else None
        if initial_account:
            folders = list(MailFolder.objects.filter(account=initial_account))
            ctx['folders_json'] = json.dumps([_serialize_folder(f) for f in folders])
            inbox = next((f for f in folders if f.folder_type == 'inbox'), folders[0] if folders else None)
            ctx['initial_folder_id'] = inbox.id if inbox else 0
        else:
            ctx['folders_json'] = '[]'
            ctx['initial_folder_id'] = 0

        # メールはIMAP直接取得 — サーバー側では渡さない（JS側でAPIを呼ぶ）
        ctx['emails_json'] = '[]'
        ctx['emails_total'] = 0

        labels = list(Label.objects.filter(user=self.request.user))
        ctx['labels_json'] = json.dumps([_serialize_label(l) for l in labels])
        return ctx


class MailSetupView(LoginRequiredMixin, TemplateView):
    """メールアカウント設定ページ"""
    template_name = 'mailer/setup.html'


# =============================
# ユーティリティ
# =============================

def _json_ok(data: dict | None = None, **kwargs) -> JsonResponse:
    payload = {'ok': True}
    if data:
        payload.update(data)
    payload.update(kwargs)
    return JsonResponse(payload)


def _json_error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'ok': False, 'error': message}, status=status)


def _parse_json_body(request) -> dict | JsonResponse:
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return _json_error('不正なJSONです')


def _require_method(request, method: str) -> JsonResponse | None:
    if request.method != method:
        return _json_error(f'{method}メソッドのみ受け付けます', 405)
    return None


def _validate_required_fields(data: dict, fields: list[str]) -> JsonResponse | None:
    for field in fields:
        if not data.get(field):
            return _json_error(f'{field} は必須です')
    return None


def _get_account_or_403(account_id, user) -> MailAccount | JsonResponse:
    try:
        account = MailAccount.objects.get(id=account_id)
    except MailAccount.DoesNotExist:
        return _json_error('アカウントが見つかりません', 404)
    if account.user != user:
        return _json_error('アクセス権限がありません', 403)
    return account


def _get_folder_or_403(folder_id, user) -> MailFolder | JsonResponse:
    try:
        folder = MailFolder.objects.select_related('account').get(id=folder_id)
    except MailFolder.DoesNotExist:
        return _json_error('フォルダが見つかりません', 404)
    if folder.account.user != user:
        return _json_error('アクセス権限がありません', 403)
    return folder


def _serialize_account(account: MailAccount) -> dict:
    return {
        'id': account.id,
        'email_address': account.email_address,
        'display_name': account.display_name,
        'imap_host': account.imap_host,
        'imap_port': account.imap_port,
        'smtp_host': account.smtp_host,
        'smtp_port': account.smtp_port,
        'last_synced_at': account.last_synced_at.isoformat() if account.last_synced_at else None,
    }


def _serialize_folder(folder: MailFolder) -> dict:
    return {
        'id': folder.id,
        'name': folder.name,
        'folder_type': folder.folder_type,
        'remote_name': folder.remote_name,
        'unread_count': folder.unread_count,
    }


def _serialize_label(label: Label) -> dict:
    return {'id': label.id, 'name': label.name, 'color': label.color}


def _serialize_imap_email(email_data: dict, folder_id: int, labels: list | None = None) -> dict:
    """IMAPから取得したメールデータをJSONシリアライズ用に変換する"""
    return {
        'uid': email_data['uid'],
        'folder_id': folder_id,
        'message_id': email_data.get('message_id', ''),
        'subject': email_data.get('subject', ''),
        'from_address': email_data.get('from_address', ''),
        'to_addresses': email_data.get('to_addresses', []),
        'is_read': email_data.get('is_read', False),
        'is_starred': email_data.get('is_starred', False),
        'has_attachments': email_data.get('has_attachments', False),
        'received_at': email_data.get('received_at'),
        'preview': '',
        'labels': labels or [],
    }


def _start_account_sync(account_id: int, log_label: str) -> None:
    def _run_sync(target_account_id: int) -> None:
        try:
            sync_account(target_account_id)
        except Exception as exc:
            logger.error('%s account_id=%s: %s', log_label, target_account_id, exc)

    Thread(target=_run_sync, args=(account_id,), daemon=True).start()


# =============================
# セットアップ登録（フォームPOST）
# =============================

@login_required
def setup_register(request):
    if request.method != 'POST':
        return redirect('mailer:setup')

    data = request.POST
    required = ['email_address', 'password', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port']
    for field in required:
        if not data.get(field):
            return redirect('mailer:setup')

    account = MailAccount(
        user=request.user,
        email_address=data['email_address'],
        display_name=data.get('display_name') or data['email_address'],
        imap_host=data['imap_host'],
        imap_port=int(data['imap_port']),
        smtp_host=data['smtp_host'],
        smtp_port=int(data['smtp_port']),
        username=data.get('username') or data['email_address'],
        use_ssl=data.get('ssl') == 'ssl',
        ssl_verify='ssl_verify' in data,
    )
    account.set_password(data['password'])
    account.save()
    _start_account_sync(account.id, 'バックグラウンド同期エラー')
    return redirect('mailer:index')


# =============================
# 接続テストAPI
# =============================

@login_required
def api_test_connection(request):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    validation_error = _validate_required_fields(body, CONNECTION_TEST_FIELDS)
    if validation_error:
        return validation_error

    results = test_connection(
        imap_host=body['imap_host'],
        imap_port=int(body['imap_port']),
        smtp_host=body['smtp_host'],
        smtp_port=int(body['smtp_port']),
        username=body['username'],
        password=body['password'],
        use_ssl=body.get('use_ssl', True),
        ssl_verify=body.get('ssl_verify', True),
    )

    all_ok = all(r['ok'] for r in results)
    return _json_ok({'results': results, 'all_ok': all_ok})


# =============================
# アカウントAPI
# =============================

@login_required
def api_accounts(request):
    """GET: アカウント一覧 / POST: アカウント新規登録"""
    if request.method == 'GET':
        accounts = MailAccount.objects.filter(user=request.user, is_active=True)
        return _json_ok({'accounts': [_serialize_account(a) for a in accounts]})

    if request.method != 'POST':
        return _json_error('許可されていないメソッドです', 405)

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    validation_error = _validate_required_fields(body, ACCOUNT_FIELDS)
    if validation_error:
        return validation_error

    account = MailAccount(
        user=request.user,
        email_address=body['email_address'],
        display_name=body.get('display_name', body['email_address']),
        imap_host=body['imap_host'],
        imap_port=int(body['imap_port']),
        smtp_host=body['smtp_host'],
        smtp_port=int(body['smtp_port']),
        username=body['username'],
        use_ssl=body.get('use_ssl', True),
        ssl_verify=body.get('ssl_verify', True),
    )
    account.set_password(body['password'])
    account.save()

    _start_account_sync(account.id, 'バックグラウンド同期エラー')
    return _json_ok({'id': account.id})


@login_required
def api_account_detail(request, account_id):
    """DELETE: アカウント削除"""
    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    if request.method == 'DELETE':
        account.is_active = False
        account.save(update_fields=['is_active'])
        return _json_ok()

    return _json_error('許可されていないメソッドです', 405)


@login_required
def api_account_sync(request, account_id):
    """POST: 手動同期（フォルダ一覧と未読数）"""
    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    _start_account_sync(account.id, '手動同期エラー')
    return _json_ok({'message': '同期を開始しました'})


@login_required
def api_folder_sync(request, folder_id):
    """POST: フォルダ未読数を同期"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    def _run():
        try:
            from .sync import sync_folder
            sync_folder(folder.account.id, folder.id)
        except Exception as exc:
            logger.error('フォルダ同期エラー folder_id=%s: %s', folder_id, exc)

    Thread(target=_run, daemon=True).start()
    return _json_ok({'message': 'フォルダ同期を開始しました'})


# =============================
# フォルダAPI
# =============================

@login_required
def api_folders(request):
    """GET: フォルダ一覧（?account_id= 必須）"""
    account_id = request.GET.get('account_id')
    if not account_id:
        return _json_error('account_id パラメータが必要です')

    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    folders = MailFolder.objects.filter(account=account)
    return _json_ok({'folders': [_serialize_folder(f) for f in folders]})


# =============================
# メールAPI（IMAP直接アクセス）
# =============================

@login_required
def api_folder_empty(request, folder_id):
    """POST: フォルダ内のメールをすべて完全削除"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        client.empty_folder(folder.remote_name)
        client.disconnect_imap()
    except Exception as exc:
        logger.warning('フォルダ一括削除失敗 folder_id=%s: %s', folder_id, exc)
        return _json_error(f'削除失敗: {exc}')

    folder.unread_count = 0
    folder.save(update_fields=['unread_count'])
    return _json_ok()


@login_required
def api_emails(request):
    """GET: メール一覧（?folder_id=, ?page=）"""
    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id パラメータが必要です')

    page = max(1, int(request.GET.get('page', 1)))
    per_page = 50

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        all_uids = sorted(client.get_folder_uids(folder.remote_name), reverse=True)
        total = len(all_uids)

        offset = (page - 1) * per_page
        page_uids = all_uids[offset:offset + per_page]
        emails_data = client.fetch_emails_by_uids(folder.remote_name, page_uids)
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    # ラベル情報を付加
    message_ids = [e.get('message_id') for e in emails_data if e.get('message_id')]
    labels_by_msgid: dict[str, list] = {}
    if message_ids:
        for el in EmailLabel.objects.filter(
            account=folder.account, message_id__in=message_ids
        ).select_related('label'):
            labels_by_msgid.setdefault(el.message_id, []).append(_serialize_label(el.label))

    # 分類情報を付加
    classification_by_uid: dict[int, dict] = {}
    for cls in EmailClassification.objects.filter(
        account=folder.account, folder=folder, uid__in=page_uids
    ).values('uid', 'category', 'summary'):
        classification_by_uid[cls['uid']] = {'category': cls['category'], 'summary': cls['summary']}

    data = [
        {
            **_serialize_imap_email(e, folder.id, labels_by_msgid.get(e.get('message_id', ''), [])),
            **classification_by_uid.get(e['uid'], {'category': None, 'summary': None}),
        }
        for e in emails_data
    ]
    return _json_ok({'emails': data, 'total': total, 'page': page, 'per_page': per_page})


@login_required
def api_email_detail(request, uid):
    """GET: メール詳細（本文含む） / DELETE: スマート削除"""
    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    if request.method == 'GET':
        try:
            client = _get_mail_client(folder.account)
            client.connect_imap()
            summary_list = client.fetch_emails_by_uids(folder.remote_name, [uid])
            if not summary_list:
                client.disconnect_imap()
                return _json_error('メールが見つかりません', 404)
            summary = summary_list[0]
            body_data = client.fetch_email_body(uid, folder.remote_name)
            was_unread = not summary.get('is_read', True)
            if was_unread:
                client.mark_as_read(uid, folder.remote_name)
            client.disconnect_imap()
        except ImapConnectionError as e:
            return _json_error(str(e))

        if was_unread:
            folder.unread_count = max(0, folder.unread_count - 1)
            folder.save(update_fields=['unread_count'])

        message_id = summary.get('message_id', '')
        labels = []
        if message_id:
            labels = [
                _serialize_label(el.label)
                for el in EmailLabel.objects.filter(
                    account=folder.account, message_id=message_id
                ).select_related('label')
            ]

        email_data = {**summary, **body_data, 'folder_id': folder.id, 'labels': labels}
        return _json_ok({'email': email_data})

    if request.method == 'DELETE':
        is_trash = folder.folder_type == 'trash'
        try:
            client = _get_mail_client(folder.account)
            client.connect_imap()
            if is_trash:
                client.delete_email(uid, folder.remote_name)
                client.disconnect_imap()
                return _json_ok({'action': 'deleted'})
            else:
                trash_folder = MailFolder.objects.filter(
                    account=folder.account, folder_type='trash'
                ).first()
                if trash_folder:
                    client.move_email(uid, folder.remote_name, trash_folder.remote_name)
                    client.disconnect_imap()
                    return _json_ok({'action': 'trashed'})
                else:
                    client.delete_email(uid, folder.remote_name)
                    client.disconnect_imap()
                    return _json_ok({'action': 'deleted'})
        except ImapConnectionError as e:
            return _json_error(str(e))

    return _json_error('許可されていないメソッドです', 405)


@login_required
def api_attachment(request, uid, index):
    """GET: 添付ファイルをダウンロードする (?folder_id=X)"""
    from django.http import HttpResponse
    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id は必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        att = client.fetch_attachment(uid, folder.remote_name, index)
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    import urllib.parse
    filename = att['filename']
    encoded = urllib.parse.quote(filename)
    response = HttpResponse(att['data'], content_type=att.get('content_type', 'application/octet-stream'))
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded}"
    return response


@login_required
def api_email_read(request, uid):
    """POST: 既読にする"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        client.mark_as_read(uid, folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as e:
        logger.warning('既読設定失敗 uid=%s: %s', uid, e)

    folder.unread_count = max(0, folder.unread_count - 1)
    folder.save(update_fields=['unread_count'])
    return _json_ok()


@login_required
def api_email_unread(request, uid):
    """POST: 未読にする"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        client.mark_as_unread(uid, folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as e:
        logger.warning('未読設定失敗 uid=%s: %s', uid, e)

    folder.unread_count = folder.unread_count + 1
    folder.save(update_fields=['unread_count'])
    return _json_ok()


@login_required
def api_email_star(request, uid):
    """POST: スター切り替え"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        is_starred = client.toggle_star(uid, folder.remote_name)
        client.disconnect_imap()
    except Exception as e:
        return _json_error(f'スター操作失敗: {e}')

    return _json_ok({'is_starred': is_starred})


@login_required
def api_email_move(request, uid):
    """POST: フォルダ移動（body: {folder_id: 移動先}、?folder_id=移動元）"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    target_folder_id = body.get('folder_id')
    if not target_folder_id:
        return _json_error('folder_id（移動先）は必須です')

    try:
        target_folder = MailFolder.objects.get(id=target_folder_id, account=folder.account)
    except MailFolder.DoesNotExist:
        return _json_error('移動先フォルダが見つかりません', 404)

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        client.move_email(uid, folder.remote_name, target_folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    return _json_ok()


# =============================
# ラベルAPI
# =============================

@login_required
def api_labels(request):
    """GET: ラベル一覧 / POST: ラベル作成"""
    if request.method == 'GET':
        labels = Label.objects.filter(user=request.user)
        return _json_ok({'labels': [_serialize_label(l) for l in labels]})

    if request.method == 'POST':
        body = _parse_json_body(request)
        if isinstance(body, JsonResponse):
            return body
        name = body.get('name', '').strip()
        if not name:
            return _json_error('ラベル名は必須です')
        color = body.get('color', '#0078d4')
        label, created = Label.objects.get_or_create(
            user=request.user, name=name, defaults={'color': color}
        )
        if not created:
            return _json_error('同名のラベルが既に存在します')
        return _json_ok({'label': _serialize_label(label)})

    return _json_error('許可されていないメソッドです', 405)


@login_required
def api_label_detail(request, label_id):
    """DELETE: ラベル削除"""
    try:
        label = Label.objects.get(id=label_id, user=request.user)
    except Label.DoesNotExist:
        return _json_error('ラベルが見つかりません', 404)

    if request.method == 'DELETE':
        label.delete()
        return _json_ok()

    return _json_error('許可されていないメソッドです', 405)


@login_required
def api_email_label(request, uid, label_id):
    """POST: ラベル付与 / DELETE: ラベル外す（?folder_id= 必須）"""
    folder_id = request.GET.get('folder_id')
    if not folder_id:
        return _json_error('folder_id クエリパラメータが必要です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        label = Label.objects.get(id=label_id, user=request.user)
    except Label.DoesNotExist:
        return _json_error('ラベルが見つかりません', 404)

    # メッセージIDをIMAPから取得（EmailLabelのキーとして使用）
    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        summary_list = client.fetch_emails_by_uids(folder.remote_name, [uid])
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    if not summary_list:
        return _json_error('メールが見つかりません', 404)

    message_id = summary_list[0].get('message_id', '') or f'uid-{uid}-folder-{folder.id}'

    if request.method == 'POST':
        EmailLabel.objects.get_or_create(
            account=folder.account, message_id=message_id, label=label
        )
        return _json_ok()

    if request.method == 'DELETE':
        EmailLabel.objects.filter(
            account=folder.account, message_id=message_id, label=label
        ).delete()
        return _json_ok()

    return _json_error('許可されていないメソッドです', 405)


# =============================
# 検索API
# =============================

@login_required
def api_search(request):
    """GET: メール検索（IMAP SEARCH）?folder_id=X&q=keyword"""
    folder_id = request.GET.get('folder_id')
    query = request.GET.get('q', '').strip()
    if not folder_id:
        return _json_error('folder_id は必須です')
    if not query:
        return _json_error('検索クエリは必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        uids = client.search_emails(folder.remote_name, query)
        if not uids:
            client.disconnect_imap()
            return _json_ok({'emails': [], 'total': 0})
        emails_raw = client.fetch_emails_by_uids(folder.remote_name, uids[:50])
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    message_ids = [e.get('message_id') for e in emails_raw if e.get('message_id')]
    labels_map = {}
    if message_ids:
        for el in EmailLabel.objects.filter(
            account=folder.account, message_id__in=message_ids
        ).select_related('label'):
            labels_map.setdefault(el.message_id, []).append({
                'id': el.label.id, 'name': el.label.name, 'color': el.label.color,
            })

    result = [_serialize_imap_email(e, folder.id, labels_map.get(e.get('message_id'), [])) for e in emails_raw]
    return _json_ok({'emails': result, 'total': len(result)})


# =============================
# 送信・返信・転送API
# =============================

@login_required
def api_send(request):
    """POST: メール送信"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    # FormDataとJSONの両方に対応
    ct = request.content_type or ''
    if 'multipart' in ct:
        account_id = request.POST.get('account_id')
        to_raw = request.POST.get('to', '')
        cc_raw = request.POST.get('cc', '')
        bcc_raw = request.POST.get('bcc', '')
        subject = request.POST.get('subject', '')
        body_text = request.POST.get('body', '')
        body_html = request.POST.get('body_html', '')
        to = [s.strip() for s in to_raw.split(',') if s.strip()]
        cc = [s.strip() for s in cc_raw.split(',') if s.strip()]
        bcc = [s.strip() for s in bcc_raw.split(',') if s.strip()]
        attachments = [
            {'filename': f.name, 'content_type': f.content_type or 'application/octet-stream', 'data': f.read()}
            for f in request.FILES.getlist('attachments')
        ]
    else:
        data = _parse_json_body(request)
        if isinstance(data, JsonResponse):
            return data
        account_id = data.get('account_id')
        to = data.get('to', [])
        cc = data.get('cc', [])
        bcc = data.get('bcc', [])
        subject = data.get('subject', '')
        body_text = data.get('body', '')
        body_html = data.get('body_html', '')
        attachments = []

    if not account_id:
        return _json_error('account_id は必須です')
    if not to:
        return _json_error('宛先（to）は必須です')

    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    sent_folder = MailFolder.objects.filter(account=account, folder_type='sent').first()
    save_to_sent = sent_folder.remote_name if sent_folder else None

    try:
        client = _get_mail_client(account)
        client.send_email(
            to=to,
            subject=subject,
            body=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            attachments=attachments or None,
            save_to_sent=save_to_sent,
        )
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


@login_required
def api_reply(request, uid):
    """POST: 返信（body: {folder_id, body}）"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    ct = request.content_type or ''
    if 'multipart' in ct:
        folder_id = request.POST.get('folder_id')
        text_body = request.POST.get('body', '')
        attachments = [
            {'filename': f.name, 'content_type': f.content_type or 'application/octet-stream', 'data': f.read()}
            for f in request.FILES.getlist('attachments')
        ]
    else:
        data = _parse_json_body(request)
        if isinstance(data, JsonResponse):
            return data
        folder_id = data.get('folder_id')
        text_body = data.get('body', '')
        attachments = []

    if not folder_id:
        return _json_error('folder_id は必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    if not text_body:
        return _json_error('本文は必須です')

    # IMAPから返信元メール情報を取得
    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        summary_list = client.fetch_emails_by_uids(folder.remote_name, [uid])
        if not summary_list:
            client.disconnect_imap()
            return _json_error('返信元メールが見つかりません', 404)
        original_data = summary_list[0]
        body_data = client.fetch_email_body(uid, folder.remote_name)
        client.disconnect_imap()
        original_data['body_text'] = body_data.get('body_text', '')
    except ImapConnectionError as e:
        return _json_error(str(e))

    sent_folder = MailFolder.objects.filter(account=folder.account, folder_type='sent').first()
    save_to_sent = sent_folder.remote_name if sent_folder else None

    try:
        client.reply_email(original_data=original_data, body=text_body, attachments=attachments or None, save_to_sent=save_to_sent)
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


@login_required
def api_forward(request, uid):
    """POST: 転送（body: {folder_id, to, body}）"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    ct = request.content_type or ''
    if 'multipart' in ct:
        folder_id = request.POST.get('folder_id')
        to_raw = request.POST.get('to', '')
        to = [s.strip() for s in to_raw.split(',') if s.strip()]
        fwd_body = request.POST.get('body', '')
        attachments = [
            {'filename': f.name, 'content_type': f.content_type or 'application/octet-stream', 'data': f.read()}
            for f in request.FILES.getlist('attachments')
        ]
    else:
        data = _parse_json_body(request)
        if isinstance(data, JsonResponse):
            return data
        folder_id = data.get('folder_id')
        to = data.get('to', [])
        fwd_body = data.get('body', '')
        attachments = []

    if not folder_id:
        return _json_error('folder_id は必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    if not to:
        return _json_error('宛先（to）は必須です')

    # IMAPから転送元メール情報を取得
    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        summary_list = client.fetch_emails_by_uids(folder.remote_name, [uid])
        if not summary_list:
            client.disconnect_imap()
            return _json_error('転送元メールが見つかりません', 404)
        original_data = summary_list[0]
        body_data = client.fetch_email_body(uid, folder.remote_name)
        client.disconnect_imap()
        original_data['body_text'] = body_data.get('body_text', '')
    except ImapConnectionError as e:
        return _json_error(str(e))

    sent_folder = MailFolder.objects.filter(account=folder.account, folder_type='sent').first()
    save_to_sent = sent_folder.remote_name if sent_folder else None

    try:
        client.forward_email(
            original_data=original_data,
            to=to,
            body=fwd_body,
            attachments=attachments or None,
            save_to_sent=save_to_sent,
        )
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


# =============================
# AI返信生成API
# =============================

_AI_SYSTEM_PROMPT = """
あなたはプロのビジネスメールアシスタントです。
ユーザーから渡されたメールへの返信文を作成するのが役割です。

## 最重要ルール: 不明な情報は必ずユーザーに質問する

以下の情報がメールの文脈から確定できない場合は、**絶対に推測・でたらめ・プレースホルダーで埋めてはいけません**。
必ず質問モードに切り替えてください。

### 質問が必須になる情報の種類
1. **承諾・拒否の意思** — 依頼・提案・招待に対してYES/NOを答える必要がある場合
2. **日程・期日・時間** — 「いつ」「何時」「何日まで」など具体的な日時が必要な場合
3. **金額・数量・条件** — 見積もり・価格・数量・契約条件など数字が必要な場合
4. **担当者・送信者の名前・役職** — 誰が対応するか、誰の名前で送るかが不明な場合
5. **理由・背景・経緯** — なぜそうするのか、何があったのかをユーザーしか知らない場合
6. **対応方針・スタンス** — どういう立場・方向性で返信するかが不明な場合
7. **具体的な内容・詳細** — 「詳細を教えてください」と言われているが内容が不明な場合
8. **添付・別途送付するもの** — 何を送るかがユーザーにしか分からない場合

### 質問モードの動作
- 上記の情報が1つでも欠けていたら、返信文を生成せずに質問のみを返してください。
- 以下のJSON形式**だけ**を返してください。前後に説明文を一切含めないでください。
  {"questions": ["質問1", "質問2", "質問3"]}
- 質問は具体的かつ簡潔に書いてください（「〇〇についてはどうしますか？」など）。
- 質問は最大4つまでに絞ってください。

### 返信文生成モードの動作（情報が全て揃っている場合のみ）
- 返信文のみを出力してください。
- 「以下が返信文です」などの前置き・補足は一切不要です。
- 指定されたトーンと長さを厳守してください。
- 含めたいポイントが指定されている場合は、必ず自然な形で本文に盛り込んでください。
- 挨拶・締めの言葉など、日本語ビジネスメールの慣習に従ってください。
- 署名は含めないでください（ユーザーが別途追加します）。

## 判断に迷ったら質問する
少しでも「これはユーザーにしか分からないのでは？」と思ったら、質問してください。
推測で返信文を作って送信されてしまうほうが、質問するよりはるかに問題です。

## 【絶対厳守】既に回答された質問は絶対に再度聞かない
プロンプトに「【追加情報（確認済み）】」セクションがある場合、そこにはユーザーが既に答えた内容が入っています。
- その内容について同じ質問を再度してはいけません。
- 全ての確認済み情報を返信文に反映させてください。
- 確認済み情報が揃っていれば、それ以上質問せず必ず返信文を生成してください。
- 「まだ情報が足りない」と感じても、確認済み情報で答えられる範囲で返信文を作成してください。

## 【絶対厳守】「スキップ・不要」はそのまま省略して進む
回答が「（スキップ・不要）」になっている質問は、ユーザーが「この情報は返信に不要」と判断したものです。
- その項目について再度質問してはいけません。
- その情報なしで返信文を作成してください。書けない場合は自然に省略または汎用的な表現で代替してください。
- 空白・スキップ項目が多くても、質問せずに返信文を生成してください。
""".strip()


def _build_ai_user_prompt(email_body: str, tone: str, length: str, points: str = '', extra_qa: str = '') -> str:
    sections = [
        f"【元メール】\n{email_body}",
        f"【トーン】{tone}",
        f"【長さ】{length}",
    ]
    if points:
        sections.append(f"【含めたいポイント】\n{points}")
    if extra_qa:
        sections.append(f"【追加情報（確認済み）】\n{extra_qa}")
        sections.append("【重要指示】ユーザーは既に質問に回答しました。これ以上質問せず、必ず今すぐ返信文を生成してください。")
    return "\n\n".join(sections)


@login_required
def api_ai_reply(request, uid):
    """POST: AI返信文生成（body: {folder_id, tone, length, points, extra_qa}）"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    folder_id = body.get('folder_id')
    if not folder_id:
        return _json_error('folder_id は必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        body_data = client.fetch_email_body(uid, folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    email_body = body_data.get('body_text', '')
    if not email_body:
        return _json_error('メール本文が取得できませんでした')

    import os as _os
    api_key = getattr(settings, 'OPENAI_API_KEY', None) or _os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return _json_error('OPENAI_API_KEY が設定されていません', 500)

    try:
        import openai
        openai_client = openai.OpenAI(api_key=api_key)
        response = openai_client.chat.completions.create(
            model='gpt-4o-mini',
            max_tokens=1000,
            messages=[
                {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
                {'role': 'user', 'content': _build_ai_user_prompt(
                    email_body,
                    body.get('tone', '丁寧'),
                    body.get('length', '普通'),
                    body.get('points', ''),
                    body.get('extra_qa', ''),
                )},
            ],
        )
    except Exception as exc:
        logger.error('AI返信生成エラー uid=%s: %s', uid, exc)
        return _json_error(f'AI生成に失敗しました: {exc}', 500)

    content = response.choices[0].message.content.strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and 'questions' in parsed:
            return _json_ok({'questions': parsed['questions']})
    except (json.JSONDecodeError, ValueError):
        pass

    return _json_ok({'reply': content})


@login_required
def api_ai_chat(request, uid):
    """
    POST /mail/api/emails/<uid>/ai-chat/
    メール内容を文脈としてAIとマルチターンチャットする。

    body: { folder_id, messages: [{role, content}, ...] }
    response: { reply: "..." }
    """
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    folder_id = body.get('folder_id')
    if not folder_id:
        return _json_error('folder_id は必須です')

    folder = _get_folder_or_403(folder_id, request.user)
    if isinstance(folder, JsonResponse):
        return folder

    messages = body.get('messages', [])
    if not messages:
        return _json_error('messages は必須です')

    # メール内容を文脈として取得
    try:
        client = _get_mail_client(folder.account)
        client.connect_imap()
        summary_list = client.fetch_emails_by_uids(folder.remote_name, [uid])
        body_data = client.fetch_email_body(uid, folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as e:
        return _json_error(str(e))

    if not summary_list:
        return _json_error('メールが見つかりません', 404)

    summary = summary_list[0]
    email_context = (
        f"件名: {summary.get('subject', '')}\n"
        f"差出人: {summary.get('from_address', '')}\n"
        f"日時: {summary.get('received_at', '')}\n"
        f"本文:\n{body_data.get('body_text', '') or '（本文なし）'}"
    )

    system_prompt = (
        "あなたはメールアシスタントです。ユーザーが開いているメールの内容を把握しており、"
        "メールに関する質問に答えたり、返信文を作成したりできます。\n\n"
        "## 現在のメール\n"
        f"{email_context}\n\n"
        "## 会話のルール\n"
        "- 返信文の作成を依頼されたとき、必要な情報が揃っていれば即座に返信文を出力してください。\n"
        "- 必要な情報が不足している場合（承諾/拒否の意思、日程・期日、金額・条件、担当者名、理由・背景など）は、"
        "返信文を作らずに質問してください。その際、メッセージの先頭に必ず「[QUESTION]」と付けてください。"
        "1回に聞く質問は最大2〜3個までにしてください。\n"
        "- ユーザーが質問に答えたら、その情報を使って返信文を作成してください。"
        "一度答えた質問は絶対に再度聞かないでください。\n"
        "- ユーザーが「スキップ」「不要」「わからない」と答えた項目はその情報なしで返信文を作成してください。\n"
        "- 返信文以外の質問（要約、翻訳など）にも普通に答えてください。\n"
        "- 署名は含めないでください（ユーザーが別途追加します）。\n"
        "- 返信文を提示するときは「---」などで区切り、そのままコピーして使えるよう仕上げてください。"
    )

    import os as _os
    api_key = getattr(settings, 'OPENAI_API_KEY', None) or _os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return _json_error('OPENAI_API_KEY が設定されていません', 500)

    try:
        import openai
        openai_client = openai.OpenAI(api_key=api_key)
        response = openai_client.chat.completions.create(
            model='gpt-4o-mini',
            max_tokens=1000,
            messages=[
                {'role': 'system', 'content': system_prompt},
                *[{'role': m['role'], 'content': m['content']} for m in messages],
            ],
        )
    except Exception as exc:
        logger.error('AIチャットエラー uid=%s: %s', uid, exc)
        return _json_error(f'AI応答に失敗しました: {exc}', 500)

    content = response.choices[0].message.content.strip()
    if content.startswith('[QUESTION]'):
        return _json_ok({'reply': content[len('[QUESTION]'):].strip(), 'type': 'question'})
    return _json_ok({'reply': content, 'type': 'reply'})


# =============================
# Gmail OAuth2
# =============================

@login_required
def gmail_oauth_start(request):
    """Gmail OAuth2認証を開始する（Googleの認証ページへリダイレクト）"""
    import os
    # ローカル開発環境でHTTPを許可（本番はHTTPS必須）
    if settings.DEBUG:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    from google_auth_oauthlib.flow import Flow
    from django.conf import settings as _settings

    if not _settings.GOOGLE_CLIENT_ID or not _settings.GOOGLE_CLIENT_SECRET:
        from django.contrib import messages
        messages.error(request, 'Google OAuth2の設定が不足しています。管理者に連絡してください。')
        return redirect('mailer:setup')

    flow = Flow.from_client_config(
        {
            'web': {
                'client_id': _settings.GOOGLE_CLIENT_ID,
                'client_secret': _settings.GOOGLE_CLIENT_SECRET,
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [_settings.GOOGLE_REDIRECT_URI],
            }
        },
        scopes=[
            'https://mail.google.com/',
            'https://www.googleapis.com/auth/userinfo.email',
            'https://www.googleapis.com/auth/userinfo.profile',
            'openid',
        ],
        redirect_uri=_settings.GOOGLE_REDIRECT_URI,
    )
    # client_secret を持つ confidential client なので PKCE は不要
    flow.autogenerate_code_verifier = False
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
    )
    request.session['gmail_oauth_state'] = state
    return redirect(auth_url)


@csrf_exempt
def gmail_oauth_callback(request):
    """Gmail OAuth2コールバック処理（Google外部リダイレクト受信）"""
    # ログイン済みでなければログインページへ（next付き）
    if not request.user.is_authenticated:
        from urllib.parse import urlencode
        next_url = request.get_full_path()
        return redirect(f'/accounts/login/?next={next_url}')

    import requests as _requests
    from google_auth_oauthlib.flow import Flow
    from django.conf import settings as _settings

    error = request.GET.get('error')
    if error:
        return redirect(f'/mail/setup/?error={error}')

    state = request.GET.get('state')
    saved_state = request.session.get('gmail_oauth_state')
    # stateが一致しない場合でもコードがあれば続行（セッション切れ対策）
    if not state:
        return redirect('/mail/setup/?error=no_state')

    flow = Flow.from_client_config(
        {
            'web': {
                'client_id': _settings.GOOGLE_CLIENT_ID,
                'client_secret': _settings.GOOGLE_CLIENT_SECRET,
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [_settings.GOOGLE_REDIRECT_URI],
            }
        },
        scopes=[
            'https://mail.google.com/',
            'https://www.googleapis.com/auth/userinfo.email',
            'https://www.googleapis.com/auth/userinfo.profile',
            'openid',
        ],
        redirect_uri=_settings.GOOGLE_REDIRECT_URI,
        state=state,
    )

    try:
        flow.fetch_token(code=request.GET.get('code'))
    except Exception as e:
        logger.error('OAuth2トークン取得エラー: %s', e)
        return redirect('/mail/setup/?error=token_error')

    credentials = flow.credentials
    access_token = credentials.token
    refresh_token = credentials.refresh_token

    # ユーザー情報取得
    try:
        resp = _requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        user_info = resp.json()
        email = user_info.get('email', '')
        name = user_info.get('name', '')
    except Exception as e:
        logger.error('ユーザー情報取得エラー: %s', e)
        return redirect('/mail/setup/?error=userinfo_error')

    if not email:
        return redirect('/mail/setup/?error=no_email')

    # MailAccount を作成または更新
    account, created = MailAccount.objects.get_or_create(
        user=request.user,
        email_address=email,
        defaults={
            'display_name': name,
            'imap_host': 'imap.gmail.com',
            'imap_port': 993,
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'username': email,
            'use_ssl': True,
            'ssl_verify': True,
            'auth_type': 'oauth2',
            'is_active': True,
        },
    )

    if refresh_token:
        account.set_refresh_token(refresh_token)
    account.auth_type = 'oauth2'
    account.display_name = name or account.display_name
    account.imap_host = 'imap.gmail.com'
    account.imap_port = 993
    account.smtp_host = 'smtp.gmail.com'
    account.smtp_port = 587
    account.username = email
    account.use_ssl = True
    account.is_active = True

    # OAuth2アカウントにはダミーパスワードを設定（未設定の場合のみ）
    if not account.password_encrypted:
        account.set_password('oauth2_no_password')

    account.save()

    # フォルダ同期
    try:
        Thread(target=sync_account, args=(account.id,), daemon=True).start()
    except Exception as e:
        logger.warning('同期開始エラー: %s', e)

    return redirect('mailer:index')


# =============================
# Outlook OAuth2
# =============================

@login_required
def outlook_oauth_start(request):
    """Outlook OAuth2認証を開始する（Microsoftの認証ページへリダイレクト）"""
    import secrets
    from urllib.parse import urlencode
    from django.conf import settings as _settings
    from django.http import HttpResponseRedirect

    if not _settings.MICROSOFT_CLIENT_ID or not _settings.MICROSOFT_CLIENT_SECRET:
        from django.contrib import messages
        messages.error(request, 'Microsoft OAuth2の設定が不足しています。管理者に連絡してください。')
        return redirect('mailer:setup')

    state = secrets.token_urlsafe(32)
    redirect_uri = request.build_absolute_uri('/mail/oauth/outlook/callback/')
    request.session['outlook_oauth_state'] = state
    request.session['outlook_oauth_redirect_uri'] = redirect_uri

    params = {
        'client_id': _settings.MICROSOFT_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'Mail.Read Mail.ReadWrite Mail.Send offline_access openid email profile',
        'state': state,
        'response_mode': 'query',
    }
    auth_url = 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?' + urlencode(params)
    logger.info('Outlook auth_url: %s', auth_url)
    return HttpResponseRedirect(auth_url)


@csrf_exempt
def outlook_oauth_callback(request):
    """Outlook OAuth2コールバック処理（Microsoft外部リダイレクト受信）"""
    if not request.user.is_authenticated:
        from urllib.parse import urlencode
        next_url = request.get_full_path()
        return redirect(f'/accounts/login/?next={next_url}')

    import requests as _requests
    from django.conf import settings as _settings

    error = request.GET.get('error')
    if error:
        return redirect(f'/mail/setup/?error={error}')

    state = request.GET.get('state')
    if not state:
        return redirect('/mail/setup/?error=no_state')

    # start時にセッションに保存した redirect_uri を使う（ポート動的対応）
    redirect_uri = request.session.get(
        'outlook_oauth_redirect_uri',
        request.build_absolute_uri('/mail/oauth/outlook/callback/'),
    )

    try:
        token_resp = _requests.post(
            'https://login.microsoftonline.com/common/oauth2/v2.0/token',
            data={
                'client_id': _settings.MICROSOFT_CLIENT_ID,
                'client_secret': _settings.MICROSOFT_CLIENT_SECRET,
                'code': request.GET.get('code'),
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
                'scope': 'Mail.Read Mail.ReadWrite Mail.Send offline_access openid email profile',
            },
            timeout=15,
        )
        result = token_resp.json()
    except Exception as e:
        logger.error('Outlook OAuth2トークン取得エラー: %s', e)
        return redirect('/mail/setup/?error=token_error')

    if 'access_token' not in result:
        err_detail = result.get('error_description') or result.get('error') or str(result)
        logger.error('Outlookトークン取得失敗: %s', err_detail)
        return redirect('/mail/setup/?error=token_error')


    refresh_token = result.get('refresh_token', '')

    # id_token（JWT）からユーザー情報を取得
    try:
        import base64, json as _json

        def _decode_jwt(token):
            payload_b64 = token.split('.')[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            return _json.loads(base64.urlsafe_b64decode(payload_b64))

        id_token = result.get('id_token', '')
        claims = _decode_jwt(id_token) if id_token else {}
        email = claims.get('preferred_username') or claims.get('email') or claims.get('upn', '')
        name = claims.get('name', '')
    except Exception as e:
        logger.error('id_tokenデコードエラー: %s', e)
        email = ''
        name = ''

    if not email:
        return redirect('/mail/setup/?error=no_email')

    # MailAccount を作成または更新
    account, created = MailAccount.objects.get_or_create(
        user=request.user,
        email_address=email,
        defaults={
            'display_name': name,
            'imap_host': 'imap.outlook.com',
            'imap_port': 993,
            'smtp_host': 'smtp.office365.com',
            'smtp_port': 587,
            'username': email,
            'use_ssl': True,
            'ssl_verify': True,
            'auth_type': 'microsoft_oauth2',
            'is_active': True,
        },
    )

    if refresh_token:
        account.set_refresh_token(refresh_token)
    account.auth_type = 'microsoft_oauth2'
    account.display_name = name or account.display_name
    account.imap_host = 'imap.outlook.com'
    account.imap_port = 993
    account.smtp_host = 'smtp.office365.com'
    account.smtp_port = 587
    account.username = email
    account.use_ssl = True
    account.is_active = True

    if not account.password_encrypted:
        account.set_password('oauth2_no_password')

    account.save()

    # フォルダ同期
    try:
        Thread(target=sync_account, args=(account.id,), daemon=True).start()
    except Exception as e:
        logger.warning('同期開始エラー: %s', e)

    return redirect('mailer:index')


# =============================
# AI メール分類ページ
# =============================

class ClassifyView(LoginRequiredMixin, TemplateView):
    """AI分類ページ"""
    template_name = 'mailer/classify.html'

    def get(self, request, *args, **kwargs):
        has_account = MailAccount.objects.filter(user=request.user, is_active=True).exists()
        if not has_account:
            return redirect('mailer:setup')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        accounts = MailAccount.objects.filter(user=self.request.user, is_active=True)
        classifications = (
            EmailClassification.objects
            .filter(account__in=accounts)
            .select_related('account', 'folder')
            .order_by('category', '-classified_at')
        )
        ctx['classifications'] = classifications
        return ctx


@login_required
def api_classify_emails(request):
    """POST: 未分類の新着メールをAIで分類して保存 / GET: 分類結果一覧を返す / DELETE: 分類結果を全削除"""
    if request.method == 'DELETE':
        accounts = MailAccount.objects.filter(user=request.user, is_active=True)
        deleted, _ = EmailClassification.objects.filter(account__in=accounts).delete()
        return _json_ok({'deleted': deleted})
    if request.method == 'GET':
        accounts = MailAccount.objects.filter(user=request.user, is_active=True)
        results = []
        for c in (
            EmailClassification.objects
            .filter(account__in=accounts)
            .select_related('account', 'folder')
            .order_by('category', '-classified_at')
        ):
            results.append({
                'id': c.id,
                'category': c.category,
                'subject': c.subject,
                'sender': c.sender,
                'summary': c.summary,
                'folder_id': c.folder_id,
                'uid': c.uid,
                'classified_at': c.classified_at.isoformat(),
                'account_email': c.account.email_address,
            })
        return _json_ok({'classifications': results})

    if request.method != 'POST':
        return _json_error('許可されていないメソッドです', 405)

    try:
        result = _classify_emails_for_user(request.user.id)
    except ValueError as exc:
        return _json_error(str(exc), 500)
    if result.get('no_accounts'):
        return _json_error('メールアカウントが登録されていません')
    return _json_ok(result)


def _classify_emails_for_user(user_id: int) -> dict:
    """指定ユーザーの受信トレイに対してAI分類を実行して保存する。
    api_classify_emails と Celery タスクの両方から呼ばれる純粋関数。"""
    import os as _os
    api_key = getattr(settings, 'OPENAI_API_KEY', None) or _os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        raise ValueError('OPENAI_API_KEY が設定されていません')

    accounts = list(MailAccount.objects.filter(user_id=user_id, is_active=True))
    if not accounts:
        return {'classified': 0, 'message': 'アカウントなし', 'errors': [], 'no_accounts': True}

    # 分類対象メールを収集（各アカウントの受信トレイから）
    to_classify = []  # [(account, folder, uid, subject, sender, message_id), ...]
    errors = []

    for account in accounts:
        inbox = MailFolder.objects.filter(account=account, folder_type='inbox').first()
        if not inbox:
            continue

        # 既分類済みの UID セットを取得
        classified_uids = set(
            EmailClassification.objects
            .filter(account=account, folder=inbox)
            .values_list('uid', flat=True)
        )

        try:
            client = _get_mail_client(account)
            client.connect_imap()
            all_uids = sorted(client.get_folder_uids(inbox.remote_name), reverse=True)
            # 直近50件を対象に未分類のものを抽出
            target_uids = [u for u in all_uids[:50] if u not in classified_uids]
            if target_uids:
                emails_data = client.fetch_emails_by_uids(inbox.remote_name, target_uids)
                for em in emails_data:
                    to_classify.append((
                        account, inbox,
                        em.get('uid', 0),
                        em.get('subject', ''),
                        em.get('from_address', ''),
                        em.get('message_id', ''),
                    ))
            client.disconnect_imap()
        except Exception as exc:
            logger.warning('分類用メール取得失敗 account=%s: %s', account.id, exc)
            errors.append(str(exc))

    if not to_classify:
        return {'classified': 0, 'message': '新しく分類するメールはありません', 'errors': errors}

    # OpenAI でバッチ分類（20件ずつ）
    import openai
    import json as _json

    BATCH_SIZE = 20
    saved_count = 0

    for i in range(0, len(to_classify), BATCH_SIZE):
        batch = to_classify[i:i + BATCH_SIZE]

        email_list_text = '\n'.join(
            f'[{j}] 件名: {subject or "(件名なし)"} | 送信者: {sender or "(不明)"}'
            for j, (_, _, _, subject, sender, _) in enumerate(batch)
        )

        system_prompt = (
            'あなたはメール管理AIです。以下のメール一覧を優先度でA/B/Cに分類し、'
            '各メールの要約を日本語で1文（30文字以内）で生成してください。\n\n'
            '【分類基準】\n'
            'A: 最優先 — 自分宛てに返信・対応が必要で期限が迫っている。例: 至急の依頼、緊急連絡、締め切り付きの確認依頼\n'
            'B: 重要 — 返信や対応が必要だが急ぎでない。例: 通常の業務依頼、質問、打ち合わせ調整\n'
            'C: 低優先 — 返信不要・確認だけでよい。例: サービスからのお知らせ、自動送信メール、ニュースレター、'
            '会員登録・購入確認、メルマガ、システム通知、広告メール、定期レポート\n\n'
            '【重要ルール】\n'
            '- 送信者がサービス・企業・システム（no-reply、noreply、notification、info@、support@など）の場合は原則C\n'
            '- 件名に「お知らせ」「通知」「確認」「ご案内」「登録」「受付」「完了」「newsletter」が含まれる場合は原則C\n'
            '- 営業メール・セールスメール・製品紹介・サービス提案・広告・スポンサーメール・キャンペーン・割引案内は原則C\n'
            '- 見知らぬ企業や初めての送信者からの一方的な提案・宣伝はCとする\n'
            '- 人から人への直接のやり取り（既存の関係者・知人・取引先からの具体的な依頼や質問）のみA・Bに分類する\n\n'
            '以下のJSON形式のみを返してください（説明文は不要）:\n'
            '{"results":[{"index":0,"category":"A","summary":"要約文"},...]}'
        )

        try:
            openai_client = openai.OpenAI(api_key=api_key)
            response = openai_client.chat.completions.create(
                model='gpt-4o-mini',
                max_tokens=800,
                response_format={'type': 'json_object'},
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'メール一覧:\n{email_list_text}'},
                ],
            )
            raw = response.choices[0].message.content.strip()
            parsed = _json.loads(raw)
            results_list = parsed.get('results', [])
        except Exception as exc:
            logger.error('OpenAI分類エラー batch=%d: %s', i, exc)
            errors.append(f'AI分類エラー: {exc}')
            continue

        for item in results_list:
            idx = item.get('index')
            if idx is None or idx >= len(batch):
                continue
            account, folder, uid, subject, sender, message_id = batch[idx]
            category = item.get('category', 'C')
            if category not in ('A', 'B', 'C'):
                category = 'C'
            summary = item.get('summary', '')[:200]

            try:
                EmailClassification.objects.update_or_create(
                    account=account,
                    folder=folder,
                    uid=uid,
                    defaults={
                        'message_id': message_id,
                        'subject': subject[:500] if subject else '',
                        'sender': sender[:255] if sender else '',
                        'summary': summary,
                        'category': category,
                    },
                )
                saved_count += 1
            except Exception as exc:
                logger.warning('分類保存失敗 uid=%s: %s', uid, exc)

    return {
        'classified': saved_count,
        'message': f'{saved_count}件のメールを分類しました',
        'errors': errors,
    }


def _serialize_schedule(schedule) -> dict:
    return {
        'is_enabled': schedule.is_enabled,
        'hour': schedule.hour,
        'minute': schedule.minute,
        'weekdays': schedule.weekdays,
        'last_run_at': schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        'next_run_at': schedule.next_run_at().isoformat() if schedule.is_enabled else None,
    }


@login_required
def api_classify_schedule(request):
    """GET: スケジュール設定取得 / POST: スケジュール設定保存"""
    from .models import ClassifySchedule

    if request.method == 'GET':
        schedule, _ = ClassifySchedule.objects.get_or_create(user=request.user)
        return _json_ok({'schedule': _serialize_schedule(schedule)})

    if request.method == 'POST':
        body = _parse_json_body(request)
        if isinstance(body, JsonResponse):
            return body

        is_enabled = bool(body.get('is_enabled', False))
        try:
            hour = int(body.get('hour', 8))
            minute = int(body.get('minute', 0))
        except (TypeError, ValueError):
            return _json_error('hour・minute は整数で指定してください')
        if not (0 <= hour <= 23):
            return _json_error('hour は 0〜23 で指定してください')
        if not (0 <= minute <= 59):
            return _json_error('minute は 0〜59 で指定してください')

        raw_wd = body.get('weekdays', [])
        if not isinstance(raw_wd, list):
            return _json_error('weekdays はリストで指定してください')
        weekdays = sorted({int(w) for w in raw_wd if isinstance(w, (int, float)) and 0 <= int(w) <= 6})

        schedule, _ = ClassifySchedule.objects.get_or_create(user=request.user)
        schedule.is_enabled = is_enabled
        schedule.hour = hour
        schedule.minute = minute
        schedule.weekdays = weekdays
        schedule.save()
        return _json_ok({'schedule': _serialize_schedule(schedule)})

    return _json_error('許可されていないメソッドです', 405)
