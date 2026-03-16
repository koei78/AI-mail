"""メールクライアントのページ表示と JSON API。"""
import json
import logging
from threading import Thread

from django.conf import settings

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .imap_client import ImapConnectionError, MailClient, SmtpConnectionError, test_connection
from .models import Email, Label, MailAccount, MailFolder
from .sync import sync_account

logger = logging.getLogger(__name__)

ACCOUNT_FIELDS = [
    'email_address',
    'password',
    'imap_host',
    'imap_port',
    'smtp_host',
    'smtp_port',
    'username',
]
CONNECTION_TEST_FIELDS = [
    'imap_host',
    'imap_port',
    'smtp_host',
    'smtp_port',
    'username',
    'password',
]


# =============================
# ページView（テンプレートを返す）
# =============================

class MailIndexView(LoginRequiredMixin, TemplateView):
    """メインページ: アカウントがあれば受信トレイ、なければセットアップへ"""
    template_name = 'mailer/index.html'

    def get(self, request, *args, **kwargs):
        has_account = MailAccount.objects.filter(user=request.user, is_active=True).exists()
        if not has_account:
            return redirect('mailer:setup')
        return super().get(request, *args, **kwargs)


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
    """JSON ボディを辞書として返す。失敗時はエラーレスポンスを返す。"""
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return _json_error('不正なJSONです')


def _require_method(request, method: str) -> JsonResponse | None:
    """HTTP メソッドを検証する。"""
    if request.method != method:
        return _json_error(f'{method}メソッドのみ受け付けます', 405)
    return None


def _validate_required_fields(data: dict, fields: list[str]) -> JsonResponse | None:
    """必須フィールドの存在を検証する。"""
    for field in fields:
        if not data.get(field):
            return _json_error(f'{field} は必須です')
    return None


def _get_account_or_403(account_id, user) -> MailAccount | JsonResponse:
    """アカウントを取得し、所有者チェックを行う。"""
    try:
        account = MailAccount.objects.get(id=account_id)
    except MailAccount.DoesNotExist:
        return _json_error('アカウントが見つかりません', status=404)
    if account.user != user:
        return _json_error('アクセス権限がありません', status=403)
    return account


def _get_email_or_403(email_id, user) -> Email | JsonResponse:
    """メールを取得し、所有者チェックを行う。"""
    try:
        email = Email.objects.select_related('account', 'folder').get(id=email_id)
    except Email.DoesNotExist:
        return _json_error('メールが見つかりません', 404)
    if email.account.user != user:
        return _json_error('アクセス権限がありません', 403)
    return email


def _get_folder_or_403(folder_id, user) -> MailFolder | JsonResponse:
    """フォルダを取得し、所有者チェックを行う。"""
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


def _serialize_email_summary(email: Email) -> dict:
    return {
        'id': email.id,
        'uid': email.uid,
        'subject': email.subject,
        'from_address': email.from_address,
        'to_addresses': email.to_addresses,
        'is_read': email.is_read,
        'is_starred': email.is_starred,
        'has_attachments': email.has_attachments,
        'received_at': email.received_at.isoformat() if email.received_at else None,
        'preview': email.body_text[:120] if email.body_text else '',
        'labels': [_serialize_label(l) for l in email.labels.all()],
    }


def _serialize_email_detail(email: Email) -> dict:
    return {
        'id': email.id,
        'subject': email.subject,
        'from_address': email.from_address,
        'to_addresses': email.to_addresses,
        'cc_addresses': email.cc_addresses,
        'body_text': email.body_text,
        'body_html': email.body_html,
        'is_read': email.is_read,
        'is_starred': email.is_starred,
        'has_attachments': email.has_attachments,
        'received_at': email.received_at.isoformat() if email.received_at else None,
        'labels': [_serialize_label(l) for l in email.labels.all()],
    }


def _refresh_unread_count(folder: MailFolder) -> None:
    folder.unread_count = Email.objects.filter(folder=folder, is_read=False).count()
    folder.save(update_fields=['unread_count'])


def _start_account_sync(account_id: int, log_label: str) -> None:
    """同期処理をバックグラウンドで起動する。"""

    def _run_sync(target_account_id: int) -> None:
        try:
            sync_account(target_account_id)
        except Exception as exc:
            logger.error('%s account_id=%s: %s', log_label, target_account_id, exc)

    Thread(target=_run_sync, args=(account_id,), daemon=True).start()


def _load_email_body_if_needed(email: Email) -> None:
    """本文が未保存なら IMAP から取得して保存する。"""
    if email.body_text or email.body_html or not email.folder:
        return

    try:
        client = MailClient(email.account)
        client.connect_imap()
        body_data = client.fetch_email_body(email.uid, email.folder.remote_name)
        client.disconnect_imap()
    except ImapConnectionError as exc:
        logger.warning('本文取得失敗 email_id=%s: %s', email.id, exc)
        return

    email.body_text = body_data['body_text']
    email.body_html = body_data['body_html']
    email.has_attachments = body_data['has_attachments']
    email.cc_addresses = body_data['cc_addresses']
    email.save(update_fields=['body_text', 'body_html', 'has_attachments', 'cc_addresses'])


# =============================
# 接続テストAPI
# =============================

@login_required
def api_test_connection(request):
    """
    POST /mail/api/test-connection/
    IMAP/SMTP接続テストを実行して各ステップの結果を返す
    """
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
        data = [_serialize_account(account) for account in accounts]
        return _json_ok({'accounts': data})

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
    """POST: 手動同期"""
    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    _start_account_sync(account.id, '手動同期エラー')
    return _json_ok({'message': '同期を開始しました'})


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
    data = [_serialize_folder(folder) for folder in folders]
    return _json_ok({'folders': data})


# =============================
# メールAPI
# =============================

@login_required
def api_emails(request):
    """GET: メール一覧（?folder_id= or ?label_id=, ?page=）"""
    label_id = request.GET.get('label_id')
    folder_id = request.GET.get('folder_id')

    page = max(1, int(request.GET.get('page', 1)))
    per_page = 50
    offset = (page - 1) * per_page

    if label_id:
        try:
            label = Label.objects.get(id=label_id, user=request.user)
        except Label.DoesNotExist:
            return _json_error('ラベルが見つかりません', 404)
        qs = Email.objects.filter(labels=label, account__user=request.user)
    elif folder_id:
        folder = _get_folder_or_403(folder_id, request.user)
        if isinstance(folder, JsonResponse):
            return folder
        qs = Email.objects.filter(folder=folder)
    else:
        return _json_error('folder_id または label_id パラメータが必要です')

    qs = qs.order_by('-received_at').prefetch_related('labels')
    total = qs.count()
    emails = qs[offset:offset + per_page]

    data = [_serialize_email_summary(email) for email in emails]
    return _json_ok({'emails': data, 'total': total, 'page': page, 'per_page': per_page})


@login_required
def api_email_detail(request, email_id):
    """GET: メール詳細（本文含む） / DELETE: 削除"""
    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    if request.method == 'GET':
        _load_email_body_if_needed(email)
        return _json_ok({'email': _serialize_email_detail(email)})

    if request.method == 'DELETE':
        if email.folder:
            try:
                client = MailClient(email.account)
                client.connect_imap()
                client.delete_email(email.uid, email.folder.remote_name)
                client.disconnect_imap()
            except ImapConnectionError as exc:
                logger.warning('IMAP削除失敗 email_id=%s: %s', email_id, exc)
        email.delete()
        return _json_ok()

    return _json_error('許可されていないメソッドです', 405)


@login_required
def api_email_read(request, email_id):
    """POST: 既読にする"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    email.is_read = True
    email.save(update_fields=['is_read'])

    # IMAPにも反映
    if email.folder:
        try:
            client = MailClient(email.account)
            client.connect_imap()
            client.mark_as_read(email.uid, email.folder.remote_name)
            client.disconnect_imap()
        except ImapConnectionError:
            pass

    if email.folder:
        _refresh_unread_count(email.folder)

    return _json_ok()


@login_required
def api_email_star(request, email_id):
    """POST: スター切り替え"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    email.is_starred = not email.is_starred
    email.save(update_fields=['is_starred'])
    return _json_ok({'is_starred': email.is_starred})


@login_required
def api_email_move(request, email_id):
    """POST: フォルダ移動（body: {folder_id}）"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    target_folder_id = body.get('folder_id')
    if not target_folder_id:
        return _json_error('folder_id は必須です')

    try:
        target_folder = MailFolder.objects.get(id=target_folder_id, account=email.account)
    except MailFolder.DoesNotExist:
        return _json_error('移動先フォルダが見つかりません', 404)

    # IMAP上でも移動
    if email.folder:
        try:
            client = MailClient(email.account)
            client.connect_imap()
            client.move_email(email.uid, email.folder.remote_name, target_folder.remote_name)
            client.disconnect_imap()
        except ImapConnectionError as exc:
            logger.warning('IMAP移動失敗 email_id=%s: %s', email_id, exc)

    email.folder = target_folder
    email.save(update_fields=['folder'])
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
def api_email_label(request, email_id, label_id):
    """POST: ラベル付与 / DELETE: ラベル外す"""
    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    try:
        label = Label.objects.get(id=label_id, user=request.user)
    except Label.DoesNotExist:
        return _json_error('ラベルが見つかりません', 404)

    if request.method == 'POST':
        email.labels.add(label)
        return _json_ok()

    if request.method == 'DELETE':
        email.labels.remove(label)
        return _json_ok()

    return _json_error('許可されていないメソッドです', 405)


# =============================
# 送信・返信・転送API
# =============================

@login_required
def api_send(request):
    """POST: メール送信"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    account_id = body.get('account_id')
    if not account_id:
        return _json_error('account_id は必須です')

    account = _get_account_or_403(account_id, request.user)
    if isinstance(account, JsonResponse):
        return account

    to = body.get('to', [])
    subject = body.get('subject', '')
    text_body = body.get('body', '')
    html_body = body.get('body_html', '')
    cc = body.get('cc', [])

    if not to:
        return _json_error('宛先（to）は必須です')

    try:
        client = MailClient(account)
        client.send_email(to=to, subject=subject, body=text_body, body_html=html_body, cc=cc)
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


@login_required
def api_reply(request, email_id):
    """POST: 返信"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    original = _get_email_or_403(email_id, request.user)
    if isinstance(original, JsonResponse):
        return original

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    text_body = body.get('body', '')
    if not text_body:
        return _json_error('本文は必須です')

    try:
        client = MailClient(original.account)
        client.reply_email(original=original, body=text_body)
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


@login_required
def api_forward(request, email_id):
    """POST: 転送"""
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    original = _get_email_or_403(email_id, request.user)
    if isinstance(original, JsonResponse):
        return original

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    to = body.get('to', [])
    text_body = body.get('body', '')

    if not to:
        return _json_error('宛先（to）は必須です')

    try:
        client = MailClient(original.account)
        client.forward_email(original=original, to=to, body=text_body)
        return _json_ok()
    except SmtpConnectionError as e:
        return _json_error(str(e))


# =============================
# AI返信生成API
# =============================

_AI_SYSTEM_PROMPT = """
あなたはプロのビジネスメールアシスタントです。
ユーザーから渡されたメールへの返信文を作成するのが役割です。

## 行動ルール

### 情報が揃っている場合
- 返信文のみを出力してください。
- 前置き・説明・「以下が返信文です」などの一切の補足は不要です。

### 情報が不足している場合
- 返信の核心に関わる情報（日程・金額・承認可否・担当者など）が不明なときのみ質問してください。
- 以下のJSON形式のみを返してください。余計なテキストは一切不要です。
  {"questions": ["質問1", "質問2"]}
- 質問は最大3つまでに絞ってください。

## 返信文を書くときの注意
- 指定されたトーンと長さを厳守してください。
- 含めたいポイントが指定されている場合は、必ず自然な形で本文に盛り込んでください。
- 添付ファイルや画像がある場合は内容を読み取り、返信に活かしてください。
- 挨拶・締めの言葉など、日本語ビジネスメールの慣習に従ってください。
- 署名は含めないでください（ユーザーが別途追加します）。
""".strip()


def _build_ai_user_prompt(
    email_body: str,
    tone: str,
    length: str,
    points: str = "",
    extra_qa: str = "",
) -> str:
    sections = [
        f"【元メール】\n{email_body}",
        f"【トーン】{tone}",
        f"【長さ】{length}",
    ]
    if points:
        sections.append(f"【含めたいポイント】\n{points}")
    if extra_qa:
        sections.append(f"【追加情報（確認済み）】\n{extra_qa}")
    return "\n\n".join(sections)


@login_required
def api_ai_reply(request, email_id):
    """
    POST /mail/api/emails/<email_id>/ai-reply/
    AI によるメール返信文草案を生成する。

    リクエストボディ（JSON）:
      tone      : str  - 返信のトーン（例: "丁寧", "カジュアル"）
      length    : str  - 返信の長さ（例: "短め", "普通", "長め"）
      points    : str  - 含めたいポイント（任意）
      extra_qa  : str  - 不足情報の質問と回答（2回目以降、任意）

    レスポンス:
      {"ok": true, "reply": "..."} 返信文が生成された場合
      {"ok": true, "questions": [...]} 情報不足で質問が返された場合
    """
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error

    email = _get_email_or_403(email_id, request.user)
    if isinstance(email, JsonResponse):
        return email

    _load_email_body_if_needed(email)

    body = _parse_json_body(request)
    if isinstance(body, JsonResponse):
        return body

    tone = body.get('tone', '丁寧')
    length = body.get('length', '普通')
    points = body.get('points', '')
    extra_qa = body.get('extra_qa', '')

    email_body = email.body_text or ''
    if not email_body:
        return _json_error('メール本文が取得できませんでした')

    api_key = getattr(settings, 'OPENAI_API_KEY', None)
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
                    email_body, tone, length, points, extra_qa
                )},
            ],
        )
    except Exception as exc:
        logger.error('AI返信生成エラー email_id=%s: %s', email_id, exc)
        return _json_error(f'AI生成に失敗しました: {exc}', 500)

    content = response.choices[0].message.content.strip()

    # モデルが質問JSONを返した場合
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and 'questions' in parsed:
            return _json_ok({'questions': parsed['questions']})
    except (json.JSONDecodeError, ValueError):
        pass

    return _json_ok({'reply': content})
