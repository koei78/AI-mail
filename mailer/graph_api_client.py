"""
mailer/graph_api_client.py
Microsoft Graph API を使った Outlook メールクライアント

IMAP/SMTP の代わりに Graph API を使うことで、ユーザーが IMAP を
手動で有効化する必要がなくなる。MailClient と同じメソッドシグネチャを持つ。
"""
import base64
import hashlib
import logging

import requests

from .models import MailAccount

logger = logging.getLogger(__name__)

BASE_URL = 'https://graph.microsoft.com/v1.0/me'


# =============================
# 独自例外クラス
# =============================

class GraphConnectionError(Exception):
    """Graph API 接続・認証エラー"""
    pass


# =============================
# フォルダ種別マッピング
# =============================

_WELL_KNOWN_FOLDER_MAP = {
    'inbox':        ('受信トレイ', 'inbox'),
    'sentitems':    ('送信済み',   'sent'),
    'drafts':       ('下書き',     'draft'),
    'deleteditems': ('ゴミ箱',     'trash'),
    'junkemail':    ('スパム',     'spam'),
    'outbox':       ('送信トレイ', 'custom'),
    'archive':      ('アーカイブ', 'custom'),
}


# =============================
# UID マッピングユーティリティ
# =============================

def _graph_uid(graph_id: str) -> int:
    """Graph メッセージ ID (文字列) を int UID に変換する（決定論的）"""
    return int(hashlib.md5(graph_id.encode()).hexdigest(), 16) % (2 ** 30)


def _cache_key(account_id: int, uid: int) -> str:
    return f'graph_uid:{account_id}:{uid}'


def _set_uid_cache(account_id: int, graph_id: str):
    from django.core.cache import cache
    uid = _graph_uid(graph_id)
    cache.set(_cache_key(account_id, uid), graph_id, timeout=3600)
    return uid


def _get_uid_cache(account_id: int, uid: int) -> str | None:
    from django.core.cache import cache
    return cache.get(_cache_key(account_id, uid))


# =============================
# アクセストークン取得
# =============================

def _get_graph_access_token(account) -> str:
    """リフレッシュトークンを使って Graph API アクセストークンを取得する"""
    from django.conf import settings as _settings

    refresh_token = account.get_refresh_token()
    if not refresh_token:
        raise GraphConnectionError('リフレッシュトークンが設定されていません')

    resp = requests.post(
        'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': _settings.MICROSOFT_CLIENT_ID,
            'client_secret': _settings.MICROSOFT_CLIENT_SECRET,
            'scope': 'Mail.Read Mail.ReadWrite Mail.Send',
        },
        timeout=30,
    )
    data = resp.json()
    if 'access_token' not in data:
        raise GraphConnectionError(
            f"トークン取得失敗: {data.get('error_description', data.get('error', 'unknown'))}"
        )

    # 新しいリフレッシュトークンが返ってきた場合は保存する
    if 'refresh_token' in data and data['refresh_token'] != refresh_token:
        account.set_refresh_token(data['refresh_token'])
        account.save(update_fields=['oauth2_refresh_token_encrypted'])

    return data['access_token']


# =============================
# GraphMailClient クラス
# =============================

class GraphMailClient:
    """Microsoft Graph API を使った Outlook メールクライアント"""

    def __init__(self, account: MailAccount):
        self.account = account
        self._token: str | None = None

    def _get_token(self) -> str:
        if not self._token:
            self._token = _get_graph_access_token(self.account)
        return self._token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self._get_token()}',
            'Content-Type': 'application/json',
        }

    def _get(self, url: str, params: dict = None) -> dict:
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 401:
            # トークン期限切れ → 再取得して1回リトライ
            self._token = _get_graph_access_token(self.account)
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if not resp.ok:
            raise GraphConnectionError(f'GET {url} 失敗 ({resp.status_code}): {resp.text[:200]}')
        return resp.json()

    def _patch(self, url: str, body: dict) -> dict:
        resp = requests.patch(url, headers=self._headers(), json=body, timeout=30)
        if resp.status_code == 401:
            self._token = _get_graph_access_token(self.account)
            resp = requests.patch(url, headers=self._headers(), json=body, timeout=30)
        if not resp.ok:
            raise GraphConnectionError(f'PATCH {url} 失敗 ({resp.status_code}): {resp.text[:200]}')
        return resp.json() if resp.content else {}

    def _post(self, url: str, body: dict) -> dict:
        resp = requests.post(url, headers=self._headers(), json=body, timeout=30)
        if resp.status_code == 401:
            self._token = _get_graph_access_token(self.account)
            resp = requests.post(url, headers=self._headers(), json=body, timeout=30)
        if not resp.ok:
            raise GraphConnectionError(f'POST {url} 失敗 ({resp.status_code}): {resp.text[:200]}')
        return resp.json() if resp.content else {}

    def _delete(self, url: str):
        resp = requests.delete(url, headers=self._headers(), timeout=30)
        if resp.status_code == 401:
            self._token = _get_graph_access_token(self.account)
            resp = requests.delete(url, headers=self._headers(), timeout=30)
        if not resp.ok and resp.status_code != 404:
            raise GraphConnectionError(f'DELETE {url} 失敗 ({resp.status_code}): {resp.text[:200]}')

    # --------------------------------------------------
    # IMAP互換メソッド（no-op）
    # --------------------------------------------------

    def connect_imap(self):
        """Graph API では不要（no-op）"""
        pass

    def disconnect_imap(self):
        """Graph API では不要（no-op）"""
        pass

    # --------------------------------------------------
    # フォルダ操作
    # --------------------------------------------------

    def fetch_folders(self) -> list[dict]:
        """
        フォルダ一覧を取得する
        戻り値: [{'name': '表示名', 'remote_name': 'Graph_folder_id', 'folder_type': 'inbox'}, ...]
        """
        try:
            data = self._get(f'{BASE_URL}/mailFolders', params={'$top': 100})
        except GraphConnectionError as e:
            raise GraphConnectionError(f'フォルダ一覧取得エラー: {e}') from e

        # wellKnownName が使えないテナントのために、well-known パスで各フォルダ ID を取得する
        well_known_ids: dict[str, str] = {}  # folder_type -> folder_id
        _well_known_api_names = {
            'inbox': 'inbox',
            'sentitems': 'sentItems',
            'drafts': 'drafts',
            'deleteditems': 'deletedItems',
            'junkemail': 'junkemail',
        }
        for wk_key, wk_api_name in _well_known_api_names.items():
            try:
                wk_data = self._get(f'{BASE_URL}/mailFolders/{wk_api_name}')
                if wk_data.get('id'):
                    well_known_ids[wk_data['id']] = wk_key
            except GraphConnectionError:
                pass

        folders = []
        for f in data.get('value', []):
            folder_id = f['id']
            well_known = (f.get('wellKnownName') or '').lower()
            if well_known in _WELL_KNOWN_FOLDER_MAP:
                display_name, folder_type = _WELL_KNOWN_FOLDER_MAP[well_known]
            elif folder_id in well_known_ids:
                wk_key = well_known_ids[folder_id]
                display_name, folder_type = _WELL_KNOWN_FOLDER_MAP[wk_key]
            else:
                display_name = f.get('displayName', folder_id)
                folder_type = 'custom'
            folders.append({
                'name': display_name,
                'remote_name': folder_id,
                'folder_type': folder_type,
            })
        return folders

    def get_folder_unread_count(self, folder_remote_name: str) -> int:
        """指定フォルダの未読数を取得する"""
        try:
            data = self._get(f'{BASE_URL}/mailFolders/{folder_remote_name}')
            return int(data.get('unreadItemCount', 0))
        except GraphConnectionError as e:
            logger.warning('未読数取得エラー folder=%s: %s', folder_remote_name, e)
            return 0

    def get_folder_uids(self, folder_remote_name: str) -> set[int]:
        """
        指定フォルダ内の全メッセージ UID セットを返す。
        Graph メッセージ ID を int UID にハッシュしてキャッシュに保存する。
        """
        try:
            uids = set()
            url = f'{BASE_URL}/mailFolders/{folder_remote_name}/messages'
            params = {'$select': 'id', '$top': 200}
            fetched = 0
            while url and fetched < 500:
                data = self._get(url, params=params)
                for msg in data.get('value', []):
                    uid = _set_uid_cache(self.account.id, msg['id'])
                    uids.add(uid)
                    fetched += 1
                url = data.get('@odata.nextLink')
                params = None  # nextLink にはパラメータが含まれている
            return uids
        except GraphConnectionError as e:
            raise GraphConnectionError(f'UID一覧取得エラー: {e}') from e

    def fetch_recent_emails_meta(self, folder_remote_name: str, count: int = 50) -> list[dict]:
        """
        最新 count 件のメールメタ情報を1回のAPIで取得して返す（分類用）。
        UID→GraphIDキャッシュ解決を経由しないためキャッシュ依存がない。
        戻り値: [{'uid': int, 'message_id': str, 'subject': str, 'from_address': str}, ...]
        """
        try:
            data = self._get(
                f'{BASE_URL}/mailFolders/{folder_remote_name}/messages',
                params={
                    '$select': 'id,subject,from,internetMessageId',
                    '$top': count,
                },
            )
            emails = []
            for msg in data.get('value', []):
                uid = _set_uid_cache(self.account.id, msg['id'])
                f = msg.get('from', {}).get('emailAddress', {})
                name = f.get('name', '')
                addr = f.get('address', '')
                from_email = f'{name} <{addr}>' if name else addr
                emails.append({
                    'uid': uid,
                    'message_id': msg.get('internetMessageId', ''),
                    'subject': msg.get('subject') or '（件名なし）',
                    'from_address': from_email,
                })
            return emails
        except GraphConnectionError as e:
            raise GraphConnectionError(f'最新メール取得エラー: {e}') from e

    def _resolve_graph_id(self, uid: int, folder_remote_name: str) -> str:
        """
        uid → Graph メッセージ ID を解決する。
        キャッシュにない場合はフォルダを再スキャンして補充する。
        """
        graph_id = _get_uid_cache(self.account.id, uid)
        if graph_id:
            return graph_id
        # キャッシュミス: フォルダを再スキャン
        self.get_folder_uids(folder_remote_name)
        graph_id = _get_uid_cache(self.account.id, uid)
        if not graph_id:
            raise GraphConnectionError(f'メッセージ UID {uid} が見つかりません')
        return graph_id

    # --------------------------------------------------
    # メール一覧取得
    # --------------------------------------------------

    def fetch_emails_by_uids(self, folder_remote_name: str, uids: list[int]) -> list[dict]:
        """
        指定 UID リストのメタ情報を取得する
        """
        if not uids:
            return []

        emails = []
        for uid in uids:
            try:
                graph_id = self._resolve_graph_id(uid, folder_remote_name)
                data = self._get(
                    f'{BASE_URL}/messages/{graph_id}',
                    params={
                        '$select': 'id,subject,from,toRecipients,isRead,flag,receivedDateTime,internetMessageId'
                    },
                )
                from_email = ''
                f = data.get('from', {}).get('emailAddress', {})
                name = f.get('name', '')
                addr = f.get('address', '')
                from_email = f'{name} <{addr}>' if name else addr

                to_addrs = [
                    r['emailAddress']['address']
                    for r in data.get('toRecipients', [])
                ]

                emails.append({
                    'uid': uid,
                    'message_id': data.get('internetMessageId', ''),
                    'subject': data.get('subject') or '（件名なし）',
                    'from_address': from_email,
                    'to_addresses': to_addrs,
                    'is_read': data.get('isRead', False),
                    'is_starred': data.get('flag', {}).get('flagStatus') == 'flagged',
                    'received_at': data.get('receivedDateTime'),
                })
            except GraphConnectionError as e:
                logger.warning('メール取得失敗 uid=%s: %s', uid, e)
                continue

        return emails

    # --------------------------------------------------
    # メール本文取得
    # --------------------------------------------------

    def fetch_email_body(self, uid: int, folder_remote_name: str) -> dict:
        """指定 UID のメール本文を取得する"""
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            data = self._get(
                f'{BASE_URL}/messages/{graph_id}',
                params={'$select': 'body,hasAttachments,ccRecipients,attachments,internetMessageId,subject,from,toRecipients,receivedDateTime,flag,isRead'},
            )
            body = data.get('body', {})
            content_type = body.get('contentType', 'text')
            content = body.get('content', '')

            body_text = content if content_type == 'text' else ''
            body_html = content if content_type == 'html' else ''

            cc_addresses = [
                r['emailAddress']['address']
                for r in data.get('ccRecipients', [])
            ]

            attachments = []
            if data.get('hasAttachments'):
                try:
                    att_data = self._get(f'{BASE_URL}/messages/{graph_id}/attachments')
                    for i, att in enumerate(att_data.get('value', [])):
                        attachments.append({
                            'index': i,
                            'filename': att.get('name', f'attachment_{i}'),
                            'content_type': att.get('contentType', 'application/octet-stream'),
                            'size': att.get('size', 0),
                        })
                except GraphConnectionError:
                    pass

            return {
                'body_text': body_text,
                'body_html': body_html,
                'has_attachments': data.get('hasAttachments', False),
                'attachments': attachments,
                'cc_addresses': cc_addresses,
            }
        except GraphConnectionError as e:
            raise GraphConnectionError(f'メール本文取得エラー: {e}') from e

    def fetch_attachment(self, uid: int, folder_remote_name: str, index: int) -> dict:
        """指定インデックスの添付ファイルデータを取得する"""
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            att_data = self._get(f'{BASE_URL}/messages/{graph_id}/attachments')
            attachments = att_data.get('value', [])
            if index >= len(attachments):
                raise GraphConnectionError('添付ファイルが見つかりません')
            att = attachments[index]
            raw = att.get('contentBytes', '')
            return {
                'filename': att.get('name', f'attachment_{index}'),
                'content_type': att.get('contentType', 'application/octet-stream'),
                'data': base64.b64decode(raw) if raw else b'',
            }
        except GraphConnectionError:
            raise
        except Exception as e:
            raise GraphConnectionError(f'添付ファイル取得エラー: {e}') from e

    # --------------------------------------------------
    # 既読/未読/スター
    # --------------------------------------------------

    def mark_as_read(self, uid: int, folder_remote_name: str) -> bool:
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            self._patch(f'{BASE_URL}/messages/{graph_id}', {'isRead': True})
            return True
        except GraphConnectionError as e:
            logger.error('既読設定エラー uid=%s: %s', uid, e)
            return False

    def mark_as_unread(self, uid: int, folder_remote_name: str) -> bool:
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            self._patch(f'{BASE_URL}/messages/{graph_id}', {'isRead': False})
            return True
        except GraphConnectionError as e:
            logger.error('未読設定エラー uid=%s: %s', uid, e)
            return False

    def toggle_star(self, uid: int, folder_remote_name: str) -> bool:
        """スター（フラグ）をトグルする。現在の状態を取得して切り替える。"""
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            data = self._get(
                f'{BASE_URL}/messages/{graph_id}',
                params={'$select': 'flag'},
            )
            current = data.get('flag', {}).get('flagStatus', 'notFlagged')
            new_status = 'notFlagged' if current == 'flagged' else 'flagged'
            self._patch(
                f'{BASE_URL}/messages/{graph_id}',
                {'flag': {'flagStatus': new_status}},
            )
            return new_status == 'flagged'
        except GraphConnectionError as e:
            logger.error('スタートグルエラー uid=%s: %s', uid, e)
            return False

    # --------------------------------------------------
    # 移動・削除
    # --------------------------------------------------

    def move_email(self, uid: int, from_folder: str, to_folder: str) -> bool:
        try:
            graph_id = self._resolve_graph_id(uid, from_folder)
            self._post(
                f'{BASE_URL}/messages/{graph_id}/move',
                {'destinationId': to_folder},
            )
            return True
        except GraphConnectionError as e:
            logger.error('移動エラー uid=%s: %s', uid, e)
            return False

    def delete_email(self, uid: int, folder_remote_name: str) -> bool:
        try:
            graph_id = self._resolve_graph_id(uid, folder_remote_name)
            self._delete(f'{BASE_URL}/messages/{graph_id}')
            return True
        except GraphConnectionError as e:
            logger.error('削除エラー uid=%s: %s', uid, e)
            return False

    def empty_folder(self, folder_remote_name: str) -> bool:
        """フォルダ内の全メッセージを削除する"""
        try:
            url = f'{BASE_URL}/mailFolders/{folder_remote_name}/messages'
            params = {'$select': 'id', '$top': 100}
            while url:
                data = self._get(url, params=params)
                for msg in data.get('value', []):
                    try:
                        self._delete(f'{BASE_URL}/messages/{msg["id"]}')
                    except GraphConnectionError:
                        pass
                url = data.get('@odata.nextLink')
                params = None
            return True
        except GraphConnectionError as e:
            logger.error('フォルダ空化エラー folder=%s: %s', folder_remote_name, e)
            return False

    # --------------------------------------------------
    # 送信・返信・転送
    # --------------------------------------------------

    def _build_recipients(self, addresses: list[str]) -> list[dict]:
        return [{'emailAddress': {'address': addr}} for addr in (addresses or [])]

    def _build_attachments(self, attachments: list[dict] | None) -> list[dict]:
        result = []
        for att in (attachments or []):
            result.append({
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': att.get('filename', 'attachment'),
                'contentType': att.get('content_type', 'application/octet-stream'),
                'contentBytes': base64.b64encode(att.get('data', b'')).decode(),
            })
        return result

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_html: str = '',
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[dict] | None = None,
        save_to_sent: str | None = None,
    ) -> bool:
        """メールを送信する（Graph API は自動的に送信済みに保存する）"""
        try:
            content = body_html if body_html else body
            content_type = 'html' if body_html else 'text'

            message = {
                'subject': subject,
                'body': {'contentType': content_type, 'content': content},
                'toRecipients': self._build_recipients(to),
            }
            if cc:
                message['ccRecipients'] = self._build_recipients(cc)
            if bcc:
                message['bccRecipients'] = self._build_recipients(bcc)
            if attachments:
                message['attachments'] = self._build_attachments(attachments)

            self._post(
                f'{BASE_URL}/sendMail',
                {'message': message, 'saveToSentItems': True},
            )
            return True
        except GraphConnectionError as e:
            logger.error('送信エラー: %s', e)
            raise

    def reply_email(
        self,
        original_data: dict,
        body: str,
        attachments: list[dict] | None = None,
        save_to_sent: str | None = None,
    ) -> bool:
        """メールに返信する"""
        try:
            from_address = original_data.get('from_address', '')
            subject = original_data.get('subject', '')
            message_id = original_data.get('message_id', '')
            body_text = original_data.get('body_text', '')

            quoted = '\n'.join(f'> {line}' for line in body_text.splitlines())
            full_body = f'{body}\n\n{quoted}'

            message = {
                'subject': f'Re: {subject}' if not subject.startswith('Re:') else subject,
                'body': {'contentType': 'text', 'content': full_body},
                'toRecipients': self._build_recipients([from_address]),
            }
            if message_id:
                message['internetMessageHeaders'] = [
                    {'name': 'In-Reply-To', 'value': message_id},
                    {'name': 'References', 'value': message_id},
                ]
            if attachments:
                message['attachments'] = self._build_attachments(attachments)

            self._post(
                f'{BASE_URL}/sendMail',
                {'message': message, 'saveToSentItems': True},
            )
            return True
        except GraphConnectionError as e:
            logger.error('返信エラー: %s', e)
            raise

    def forward_email(
        self,
        original_data: dict,
        to: list[str],
        body: str,
        attachments: list[dict] | None = None,
        save_to_sent: str | None = None,
    ) -> bool:
        """メールを転送する"""
        try:
            subject = original_data.get('subject', '')
            from_address = original_data.get('from_address', '')
            received_at = original_data.get('received_at', '')
            to_addresses = original_data.get('to_addresses', [])
            body_text = original_data.get('body_text', '')

            header = (
                f'\n\n---------- 転送メッセージ ----------\n'
                f'差出人: {from_address}\n'
                f'日時: {received_at}\n'
                f'件名: {subject}\n'
                f'宛先: {", ".join(to_addresses)}\n\n'
            )
            full_body = f'{body}{header}{body_text}'

            message = {
                'subject': f'Fwd: {subject}',
                'body': {'contentType': 'text', 'content': full_body},
                'toRecipients': self._build_recipients(to),
            }
            if attachments:
                message['attachments'] = self._build_attachments(attachments)

            self._post(
                f'{BASE_URL}/sendMail',
                {'message': message, 'saveToSentItems': True},
            )
            return True
        except GraphConnectionError as e:
            logger.error('転送エラー: %s', e)
            raise

    # --------------------------------------------------
    # 検索
    # --------------------------------------------------

    def search_emails(self, folder_remote_name: str, query: str) -> list[int]:
        """
        フォルダ内でメールを検索し UID リストを返す（新しい順）
        """
        try:
            data = self._get(
                f'{BASE_URL}/mailFolders/{folder_remote_name}/messages',
                params={
                    '$search': f'"{query}"',
                    '$select': 'id',
                    '$top': 50,
                },
            )
            uids = []
            for msg in data.get('value', []):
                uid = _set_uid_cache(self.account.id, msg['id'])
                uids.append(uid)
            return uids
        except GraphConnectionError as e:
            logger.warning('Graph 検索エラー: %s', e)
            return []
