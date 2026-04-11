"""
mailer/imap_client.py
IMAP/SMTPの接続・操作をまとめたクライアントクラス
"""
import smtplib
import logging
import ssl
from email import policy
from email.header import decode_header, make_header, Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.utils import formataddr

import imapclient

from .models import MailAccount

logger = logging.getLogger(__name__)


def _make_from_header(display_name: str, email_address: str) -> str:
    """RFC 5322 準拠の From ヘッダー値を生成する（非ASCII文字を自動エンコード）"""
    name = display_name or ''
    try:
        name.encode('ascii')
        return formataddr((name, email_address)) if name else email_address
    except UnicodeEncodeError:
        return formataddr((str(Header(name, 'utf-8')), email_address))


# =============================
# 独自例外クラス
# =============================

class ImapConnectionError(Exception):
    """IMAP接続・認証エラー"""
    pass


class SmtpConnectionError(Exception):
    """SMTP接続・認証エラー"""
    pass


# =============================
# ヘルパー関数
# =============================

def _decode_str(value) -> str:
    """RFC 2047エンコードされた文字列をデコードする（文字化け対策）"""
    if value is None:
        return ''
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_addresses(header_value: str) -> list[str]:
    """To/CC ヘッダーをアドレスのリストに変換する"""
    if not header_value:
        return []
    return [addr.strip() for addr in header_value.split(',') if addr.strip()]


def _get_oauth2_access_token(account) -> str:
    """OAuth2アカウントのアクセストークンを取得（必要に応じてリフレッシュ）"""
    from django.conf import settings as _settings
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest

    refresh_token = account.get_refresh_token()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=_settings.GOOGLE_CLIENT_ID,
        client_secret=_settings.GOOGLE_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://mail.google.com/'],
    )
    creds.refresh(GoogleRequest())
    return creds.token



def _parse_body(msg) -> tuple[str, str, bool, list]:
    """
    MIMEメッセージから本文と添付ファイル情報を取得する
    戻り値: (body_text, body_html, has_attachments, attachments)
    attachments: [{'index': int, 'filename': str, 'content_type': str, 'size': int}]
    """
    body_text = ''
    body_html = ''
    has_attachments = False
    attachments = []
    attach_index = 0

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get('Content-Disposition', ''))

            if 'attachment' in disposition:
                has_attachments = True
                filename = part.get_filename()
                if filename:
                    payload = part.get_payload(decode=True)
                    attachments.append({
                        'index': attach_index,
                        'filename': _decode_str(filename),
                        'content_type': content_type,
                        'size': len(payload) if payload else 0,
                    })
                attach_index += 1
                continue

            if content_type == 'text/plain' and not body_text:
                charset = part.get_content_charset() or 'utf-8'
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(charset, errors='replace')
            elif content_type == 'text/html' and not body_html:
                charset = part.get_content_charset() or 'utf-8'
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode(charset, errors='replace')
    else:
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or 'utf-8'
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(charset, errors='replace')
            if content_type == 'text/html':
                body_html = text
            else:
                body_text = text

    return body_text, body_html, has_attachments, attachments


# =============================
# フォルダ種別の推定
# =============================

_FOLDER_TYPE_MAP = {
    'INBOX': 'inbox',
    'Sent': 'sent',
    'Sent Messages': 'sent',
    '送信済み': 'sent',
    'Drafts': 'draft',
    '下書き': 'draft',
    'Trash': 'trash',
    'Deleted Messages': 'trash',
    'ゴミ箱': 'trash',
    'Spam': 'spam',
    'Junk': 'spam',
    'Junk Email': 'spam',
    'スパム': 'spam',
}


def _guess_folder_type(remote_name: str) -> str:
    # まず完全一致・末尾一致で判定（INBOX.Sent の末尾が "Sent" など）
    lower = remote_name.lower()
    for key, ftype in _FOLDER_TYPE_MAP.items():
        if lower == key.lower():
            return ftype
    # 末尾一致（INBOX.Sent → Sent）
    for key, ftype in _FOLDER_TYPE_MAP.items():
        if lower.endswith('.' + key.lower()):
            return ftype
    return 'custom'


# =============================
# メインクライアントクラス
# =============================

class MailClient:
    """IMAP/SMTPの操作をまとめたクライアントクラス"""

    def __init__(self, account: MailAccount):
        self.account = account
        self._imap: imapclient.IMAPClient | None = None

    # --------------------------------------------------
    # IMAP接続
    # --------------------------------------------------

    def connect_imap(self):
        """IMAPサーバーにSSL接続してログインする"""
        try:
            ssl_context = None
            if self.account.use_ssl and not self.account.ssl_verify:
                # ⚠️ さくら/Xserverなど共用ホスティングはSSL証明書がサーバー名で
                # 発行されるためホスト名不一致になる。ssl_verify=Falseで回避する。
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            self._imap = imapclient.IMAPClient(
                host=self.account.imap_host,
                port=self.account.imap_port,
                ssl=self.account.use_ssl,
                ssl_context=ssl_context,
                timeout=30,
            )
            auth_type = getattr(self.account, 'auth_type', 'password')
            if auth_type == 'oauth2':
                access_token = _get_oauth2_access_token(self.account)
                self._imap.oauth2_login(self.account.email_address, access_token)
            else:
                self._imap.login(
                    self.account.username,
                    self.account.get_password(),
                )
        except imapclient.IMAPClient.Error as e:
            raise ImapConnectionError(f'IMAP接続エラー: {e}') from e
        except Exception as e:
            raise ImapConnectionError(f'IMAP予期せぬエラー: {e}') from e

    def disconnect_imap(self):
        """IMAP接続を切断する"""
        if self._imap:
            try:
                self._imap.logout()
            except Exception:
                pass
            finally:
                self._imap = None

    def _require_imap(self):
        if self._imap is None:
            raise ImapConnectionError('IMAPに接続されていません。connect_imap()を先に呼んでください。')

    # --------------------------------------------------
    # フォルダ操作
    # --------------------------------------------------

    def fetch_folders(self) -> list[dict]:
        """
        サーバーからフォルダ一覧を取得する
        戻り値: [{'name': '表示名', 'remote_name': 'INBOX', 'folder_type': 'inbox'}, ...]
        """
        self._require_imap()
        try:
            raw_folders = self._imap.list_folders()
        except Exception as e:
            raise ImapConnectionError(f'フォルダ一覧取得エラー: {e}') from e

        folders = []
        for flags, delimiter, remote_name in raw_folders:
            if isinstance(remote_name, bytes):
                remote_name = remote_name.decode('utf-7', errors='replace')
            folder_type = _guess_folder_type(remote_name)
            # 日本語名のマッピング
            display_name = {
                'inbox': '受信トレイ',
                'sent': '送信済み',
                'draft': '下書き',
                'trash': 'ゴミ箱',
                'spam': 'スパム',
            }.get(folder_type, remote_name)
            folders.append({
                'name': display_name,
                'remote_name': remote_name,
                'folder_type': folder_type,
            })
        return folders

    # --------------------------------------------------
    # メール一覧取得
    # --------------------------------------------------

    _FETCH_BATCH_SIZE = 200  # 1回のIMAPフェッチで取得する件数

    def get_folder_uids(self, folder_remote_name: str) -> set[int]:
        """指定フォルダにある全メールのUID一覧を返す"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            return set(self._imap.search(['ALL']))
        except Exception as e:
            raise ImapConnectionError(f'UID一覧取得エラー: {e}') from e

    def fetch_emails_by_uids(
        self, folder_remote_name: str, uids: list[int]
    ) -> list[dict]:
        """
        指定UIDリストのメタ情報を取得する（バッチフェッチ）
        大量UID を一括 fetch するとサーバーが応答しないため、
        _FETCH_BATCH_SIZE 件ずつに分割して取得する。
        戻り値: メタ情報のリスト（本文は含まない）
        """
        self._require_imap()
        if not uids:
            return []

        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
        except Exception as e:
            raise ImapConnectionError(f'フォルダ選択エラー: {e}') from e

        # 新しい順にソート（UID が大きいほど新しい）
        target_uids = sorted(uids, reverse=True)

        # バッチに分割してフェッチ
        raw_fetch: dict = {}
        batch = self._FETCH_BATCH_SIZE
        for i in range(0, len(target_uids), batch):
            chunk = target_uids[i:i + batch]
            try:
                chunk_data = self._imap.fetch(
                    chunk, ['ENVELOPE', 'FLAGS', 'RFC822.SIZE']
                )
                raw_fetch.update(chunk_data)
            except Exception as e:
                raise ImapConnectionError(f'メール取得エラー: {e}') from e

        emails = []
        for uid in target_uids:  # 新しい順を維持
            data = raw_fetch.get(uid)
            if not data:
                continue
            envelope = data.get(b'ENVELOPE')
            flags = data.get(b'FLAGS', [])
            if not envelope:
                continue

            is_read = b'\\Seen' in flags
            is_starred = b'\\Flagged' in flags

            # 件名デコード
            subject = _decode_str(envelope.subject) if envelope.subject else '（件名なし）'

            # 送信者
            from_list = envelope.from_ or []
            from_addr = ''
            if from_list:
                f = from_list[0]
                name = _decode_str(f.name) if f.name else ''
                mailbox = f.mailbox.decode() if f.mailbox else ''
                host = f.host.decode() if f.host else ''
                from_addr = f'{name} <{mailbox}@{host}>' if name else f'{mailbox}@{host}'

            # 宛先
            to_addrs = []
            for t in (envelope.to or []):
                mailbox = t.mailbox.decode() if t.mailbox else ''
                host = t.host.decode() if t.host else ''
                to_addrs.append(f'{mailbox}@{host}')

            # 受信日時
            received_at = None
            if envelope.date:
                received_at = envelope.date.isoformat()

            # Message-ID
            message_id = ''
            if envelope.message_id:
                message_id = envelope.message_id.decode(errors='replace')

            emails.append({
                'uid': uid,
                'message_id': message_id,
                'subject': subject,
                'from_address': from_addr,
                'to_addresses': to_addrs,
                'is_read': is_read,
                'is_starred': is_starred,
                'received_at': received_at,
            })

        return emails

    # --------------------------------------------------
    # メール本文取得
    # --------------------------------------------------

    def fetch_email_body(self, uid: int, folder_remote_name: str) -> dict:
        """
        指定UIDのメール本文を取得する
        戻り値: {body_text, body_html, has_attachments, cc_addresses}
        """
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            fetch_data = self._imap.fetch([uid], ['RFC822'])
        except Exception as e:
            raise ImapConnectionError(f'メール本文取得エラー: {e}') from e

        raw = fetch_data.get(uid, {}).get(b'RFC822')
        if not raw:
            return {'body_text': '', 'body_html': '', 'has_attachments': False, 'cc_addresses': []}

        msg = BytesParser(policy=policy.compat32).parsebytes(raw)
        body_text, body_html, has_attachments, attachments = _parse_body(msg)

        cc_header = msg.get('Cc', '')
        cc_addresses = _parse_addresses(cc_header)

        return {
            'body_text': body_text,
            'body_html': body_html,
            'has_attachments': has_attachments,
            'attachments': attachments,
            'cc_addresses': cc_addresses,
        }

    def fetch_attachment(self, uid: int, folder_remote_name: str, index: int) -> dict:
        """
        指定インデックスの添付ファイルデータを取得する
        戻り値: {'filename': str, 'content_type': str, 'data': bytes}
        """
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            fetch_data = self._imap.fetch([uid], ['RFC822'])
        except Exception as e:
            raise ImapConnectionError(f'添付ファイル取得エラー: {e}') from e

        raw = fetch_data.get(uid, {}).get(b'RFC822')
        if not raw:
            raise ImapConnectionError('メールが見つかりません')

        msg = BytesParser(policy=policy.compat32).parsebytes(raw)
        attach_index = 0
        for part in msg.walk():
            disposition = str(part.get('Content-Disposition', ''))
            if 'attachment' in disposition:
                filename = part.get_filename()
                if filename:
                    if attach_index == index:
                        return {
                            'filename': _decode_str(filename),
                            'content_type': part.get_content_type(),
                            'data': part.get_payload(decode=True) or b'',
                        }
                    attach_index += 1
        raise ImapConnectionError('添付ファイルが見つかりません')

    # --------------------------------------------------
    # 既読/未読
    # --------------------------------------------------

    def mark_as_read(self, uid: int, folder_remote_name: str) -> bool:
        """指定メールを既読にする"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            self._imap.add_flags([uid], [b'\\Seen'])
            return True
        except Exception as e:
            logger.error('既読設定エラー uid=%s: %s', uid, e)
            return False

    def mark_as_unread(self, uid: int, folder_remote_name: str) -> bool:
        """指定メールを未読にする"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            self._imap.remove_flags([uid], [b'\\Seen'])
            return True
        except Exception as e:
            logger.error('未読設定エラー uid=%s: %s', uid, e)
            return False

    def toggle_star(self, uid: int, folder_remote_name: str) -> bool:
        """スター（\\Flagged）をトグルする。現在の状態を取得して切り替える。"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            data = self._imap.fetch([uid], ['FLAGS'])
            flags = data.get(uid, {}).get(b'FLAGS', [])
            if b'\\Flagged' in flags:
                self._imap.remove_flags([uid], [b'\\Flagged'])
                return False
            else:
                self._imap.add_flags([uid], [b'\\Flagged'])
                return True
        except Exception as e:
            logger.error('スタートグルエラー uid=%s: %s', uid, e)
            return False

    # --------------------------------------------------
    # 移動・削除
    # --------------------------------------------------

    def move_email(self, uid: int, from_folder: str, to_folder: str) -> bool:
        """メールを別フォルダへ移動する"""
        self._require_imap()
        try:
            self._imap.select_folder(from_folder)
            self._imap.move([uid], to_folder)
            return True
        except imapclient.IMAPClient.Error:
            # MOVE未対応サーバー向けのフォールバック (COPY + DELETE)
            try:
                self._imap.copy([uid], to_folder)
                self._imap.add_flags([uid], [b'\\Deleted'])
                self._imap.expunge()
                return True
            except Exception as e:
                logger.error('移動エラー uid=%s: %s', uid, e)
                return False
        except Exception as e:
            logger.error('移動エラー uid=%s: %s', uid, e)
            return False

    def delete_email(self, uid: int, folder_remote_name: str) -> bool:
        """メールを完全削除する（IMAPから消去）"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            self._imap.add_flags([uid], [b'\\Deleted'])
            self._imap.expunge()
            return True
        except Exception as e:
            logger.error('削除エラー uid=%s: %s', uid, e)
            return False

    def empty_folder(self, folder_remote_name: str) -> bool:
        """フォルダ内の全メッセージを完全削除する"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            uids = self._imap.search(['ALL'])
            if uids:
                self._imap.add_flags(uids, [b'\\Deleted'])
                self._imap.expunge()
            return True
        except Exception as e:
            logger.error('フォルダ空化エラー folder=%s: %s', folder_remote_name, e)
            return False

    def append_to_folder(self, raw_bytes: bytes, folder_remote_name: str) -> bool:
        """指定フォルダにメッセージを追加する（送信済み保存に使用）"""
        self._require_imap()
        try:
            from datetime import datetime, timezone
            self._imap.append(
                folder_remote_name,
                raw_bytes,
                flags=[b'\\Seen'],
                msg_time=datetime.now(timezone.utc),
            )
            return True
        except Exception as e:
            logger.error('フォルダアペンドエラー folder=%s: %s', folder_remote_name, e)
            return False

    # --------------------------------------------------
    # 送信者・宛先検索（友達機能用）
    # --------------------------------------------------

    def _scan_for_address(self, folder_remote_name: str, email_address: str,
                          field: str = 'from', scan_count: int = 500) -> list[int]:
        """
        最近のメールを手動スキャンしてアドレスが一致するUIDリストを返す。
        IMAP SEARCH が機能しない場合のフォールバック。
        """
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            all_uids = self._imap.search(['ALL'])
        except Exception:
            return []
        if not all_uids:
            return []
        recent = sorted(all_uids, reverse=True)[:scan_count]
        logger.info('_scan_for_address: フォルダ=%s 総件数=%d スキャン=%d 検索アドレス=%s',
                    folder_remote_name, len(all_uids), len(recent), email_address)

        # バッチで ENVELOPE を取得（一度に大量取得するとサーバーが応答しない場合がある）
        raw = {}
        batch = 100
        for i in range(0, len(recent), batch):
            chunk = recent[i:i + batch]
            try:
                raw.update(self._imap.fetch(chunk, ['ENVELOPE']))
            except Exception:
                continue

        email_lower = email_address.lower()
        matching = []
        for uid, data in raw.items():
            envelope = data.get(b'ENVELOPE')
            if not envelope:
                continue
            addrs = (envelope.from_ if field == 'from' else envelope.to) or []
            for addr in addrs:
                mailbox = (addr.mailbox or b'').decode('utf-8', errors='replace').lower()
                host = (addr.host or b'').decode('utf-8', errors='replace').lower()
                full = f'{mailbox}@{host}'
                if email_lower == full:
                    matching.append(uid)
                    break
        logger.info('_scan_for_address: マッチ件数=%d', len(matching))
        return matching

    def search_emails_by_sender(self, folder_remote_name: str, sender_email: str, limit: int = 50) -> list[dict]:
        """送信者アドレスで検索。複数の戦略を順に試す。"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            uids = self._imap.search(['FROM', sender_email])
            logger.info('IMAP FROM検索: folder=%s email=%s → %d件', folder_remote_name, sender_email, len(uids))
        except Exception as e:
            raise ImapConnectionError(f'送信者検索エラー: {e}') from e

        if not uids:
            # フォールバック: ドメイン部分で再検索
            domain = sender_email.split('@')[-1] if '@' in sender_email else ''
            if domain:
                try:
                    uids = self._imap.search(['FROM', domain])
                    # ドメイン一致だと過剰取得になるので、後で正確なアドレスで絞り込む
                    if uids:
                        raw = {}
                        batch = 100
                        for i in range(0, len(uids), batch):
                            try:
                                raw.update(self._imap.fetch(sorted(uids, reverse=True)[i:i+batch], ['ENVELOPE']))
                            except Exception:
                                continue
                        email_lower = sender_email.lower()
                        uids = []
                        for uid, data in raw.items():
                            envelope = data.get(b'ENVELOPE')
                            if not envelope:
                                continue
                            for addr in (envelope.from_ or []):
                                mailbox = (addr.mailbox or b'').decode('utf-8', errors='replace').lower()
                                host = (addr.host or b'').decode('utf-8', errors='replace').lower()
                                if email_lower == f'{mailbox}@{host}':
                                    uids.append(uid)
                                    break
                        logger.info('ドメイン検索後絞り込み: %d件', len(uids))
                except Exception:
                    uids = []

        if not uids:
            # 最終フォールバック: 直近 500 通を手動スキャン
            uids = self._scan_for_address(folder_remote_name, sender_email, field='from')

        if not uids:
            return []
        recent_uids = sorted(uids, reverse=True)[:limit]
        return self.fetch_emails_by_uids(folder_remote_name, recent_uids)

    def search_emails_to_recipient(self, folder_remote_name: str, recipient_email: str, limit: int = 50) -> list[dict]:
        """宛先アドレスで検索。複数の戦略を順に試す。"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            uids = self._imap.search(['TO', recipient_email])
            logger.info('IMAP TO検索: folder=%s email=%s → %d件', folder_remote_name, recipient_email, len(uids))
        except Exception as e:
            raise ImapConnectionError(f'宛先検索エラー: {e}') from e

        if not uids:
            uids = self._scan_for_address(folder_remote_name, recipient_email, field='to')

        if not uids:
            return []
        recent_uids = sorted(uids, reverse=True)[:limit]
        return self.fetch_emails_by_uids(folder_remote_name, recent_uids)

    # --------------------------------------------------
    # SMTP送信
    # --------------------------------------------------

    def _build_smtp(self):
        """SMTPサーバーへ接続してログインしたインスタンスを返す"""
        ssl_context = None
        if not self.account.ssl_verify:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            auth_type = getattr(self.account, 'auth_type', 'password')
            if auth_type == 'oauth2':
                import base64
                access_token = _get_oauth2_access_token(self.account)
                smtp = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
                smtp.starttls(context=ssl_context)
                auth_string = f'user={self.account.email_address}\x01auth=Bearer {access_token}\x01\x01'
                encoded = base64.b64encode(auth_string.encode()).decode()
                smtp.docmd('AUTH', 'XOAUTH2 ' + encoded)
                return smtp
            password = self.account.get_password()
            if self.account.use_ssl:
                smtp = smtplib.SMTP_SSL(
                    self.account.smtp_host,
                    self.account.smtp_port,
                    context=ssl_context,
                    timeout=30,
                )
            else:
                smtp = smtplib.SMTP(
                    self.account.smtp_host,
                    self.account.smtp_port,
                    timeout=30,
                )
                smtp.starttls(context=ssl_context)
            smtp.login(self.account.username, password)
            return smtp
        except smtplib.SMTPException as e:
            raise SmtpConnectionError(f'SMTP接続エラー: {e}') from e
        except Exception as e:
            raise SmtpConnectionError(f'SMTP予期せぬエラー: {e}') from e

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
        """
        メールを送信する
        attachments: [{'filename': str, 'content_type': str, 'data': bytes}]
        bcc: BCCアドレスリスト（ヘッダーには含めず、エンベロープのみに使用）
        save_to_sent: 送信済みフォルダのremote_name（指定時はIMAPにAPPEND）
        """
        from email.mime.base import MIMEBase
        from email import encoders as _encoders

        if attachments:
            msg = MIMEMultipart('mixed')
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(body, 'plain', 'utf-8'))
            if body_html:
                alt.attach(MIMEText(body_html, 'html', 'utf-8'))
            msg.attach(alt)
            for att in attachments:
                maintype, _, subtype = att['content_type'].partition('/')
                part = MIMEBase(maintype, subtype or 'octet-stream')
                part.set_payload(att['data'])
                _encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment', filename=att['filename'])
                msg.attach(part)
        else:
            msg = MIMEMultipart('alternative')
            msg.attach(MIMEText(body, 'plain', 'utf-8'))
            if body_html:
                msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        msg['Subject'] = subject
        msg['From'] = _make_from_header(self.account.display_name or '', self.account.email_address)
        msg['To'] = ', '.join(to)
        if cc:
            msg['Cc'] = ', '.join(cc)
        # BCC: ヘッダーには含めず送信先リストにのみ追加

        all_recipients = to + (cc or []) + (bcc or [])
        raw_bytes = msg.as_bytes()

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, all_recipients, raw_bytes)
            smtp.quit()
        except SmtpConnectionError:
            raise
        except Exception as e:
            logger.error('送信エラー: %s', e)
            raise SmtpConnectionError(f'送信エラー: {e}') from e

        # 送信済みフォルダへ保存（ベストエフォート）
        if save_to_sent:
            try:
                self.connect_imap()
                self.append_to_folder(raw_bytes, save_to_sent)
                self.disconnect_imap()
            except Exception as e:
                logger.warning('送信済みフォルダ保存失敗: %s', e)

        return True

    def get_folder_unread_count(self, folder_remote_name: str) -> int:
        """指定フォルダの未読数をIMAPのSTATUSコマンドで取得する"""
        self._require_imap()
        try:
            status = self._imap.folder_status(folder_remote_name, ['UNSEEN'])
            return int(status.get(b'UNSEEN', 0))
        except Exception as e:
            logger.warning('未読数取得エラー folder=%s: %s', folder_remote_name, e)
            return 0

    def search_emails(self, folder_remote_name: str, query: str) -> list[int]:
        """
        IMAPのSEARCHコマンドでメールを検索し、UIDリストを返す（新しい順）
        """
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            uids = self._imap.search(['OR', ['SUBJECT', query], ['FROM', query]])
            return sorted(list(uids), reverse=True)
        except Exception as e:
            logger.warning('IMAP検索エラー: %s', e)
            return []

    def reply_email(self, original_data: dict, body: str, attachments: list[dict] | None = None, save_to_sent: str | None = None) -> bool:
        """
        メールに返信する
        original_data: {'subject', 'from_address', 'message_id', 'body_text'}
        attachments: [{'filename': str, 'content_type': str, 'data': bytes}]
        save_to_sent: 送信済みフォルダのremote_name（指定時はIMAPにAPPEND）
        """
        from email.mime.base import MIMEBase
        from email import encoders as _encoders

        subject = original_data.get('subject', '')
        from_address = original_data.get('from_address', '')
        message_id = original_data.get('message_id', '')
        body_text = original_data.get('body_text', '')

        quoted = '\n'.join(f'> {line}' for line in body_text.splitlines())
        full_body = f'{body}\n\n{quoted}'

        if attachments:
            msg = MIMEMultipart('mixed')
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(full_body, 'plain', 'utf-8'))
            msg.attach(alt)
            for att in attachments:
                maintype, _, subtype = att['content_type'].partition('/')
                part = MIMEBase(maintype, subtype or 'octet-stream')
                part.set_payload(att['data'])
                _encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment', filename=att['filename'])
                msg.attach(part)
        else:
            msg = MIMEMultipart('alternative')
            msg.attach(MIMEText(full_body, 'plain', 'utf-8'))

        msg['Subject'] = f'Re: {subject}' if not subject.startswith('Re:') else subject
        msg['From'] = _make_from_header(self.account.display_name or '', self.account.email_address)
        msg['To'] = from_address
        if message_id:
            msg['In-Reply-To'] = message_id
            msg['References'] = message_id

        raw_bytes = msg.as_bytes()

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, [from_address], raw_bytes)
            smtp.quit()
        except SmtpConnectionError:
            raise
        except Exception as e:
            raise SmtpConnectionError(f'返信エラー: {e}') from e

        if save_to_sent:
            try:
                self.connect_imap()
                self.append_to_folder(raw_bytes, save_to_sent)
                self.disconnect_imap()
            except Exception as e:
                logger.warning('送信済みフォルダ保存失敗（返信）: %s', e)

        return True

    def forward_email(self, original_data: dict, to: list[str], body: str, attachments: list[dict] | None = None, save_to_sent: str | None = None) -> bool:
        """メールを転送する
        original_data: {'subject', 'from_address', 'received_at', 'to_addresses', 'body_text'}
        attachments: [{'filename': str, 'content_type': str, 'data': bytes}]
        save_to_sent: 送信済みフォルダのremote_name（指定時はIMAPにAPPEND）
        """
        from email.mime.base import MIMEBase
        from email import encoders as _encoders

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

        if attachments:
            msg = MIMEMultipart('mixed')
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(full_body, 'plain', 'utf-8'))
            msg.attach(alt)
            for att in attachments:
                maintype, _, subtype = att['content_type'].partition('/')
                part = MIMEBase(maintype, subtype or 'octet-stream')
                part.set_payload(att['data'])
                _encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment', filename=att['filename'])
                msg.attach(part)
        else:
            msg = MIMEMultipart('alternative')
            msg.attach(MIMEText(full_body, 'plain', 'utf-8'))

        msg['Subject'] = f'Fwd: {subject}'
        msg['From'] = _make_from_header(self.account.display_name or '', self.account.email_address)
        msg['To'] = ', '.join(to)

        raw_bytes = msg.as_bytes()

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, to, raw_bytes)
            smtp.quit()
        except SmtpConnectionError:
            raise
        except Exception as e:
            raise SmtpConnectionError(f'転送エラー: {e}') from e

        if save_to_sent:
            try:
                self.connect_imap()
                self.append_to_folder(raw_bytes, save_to_sent)
                self.disconnect_imap()
            except Exception as e:
                logger.warning('送信済みフォルダ保存失敗（転送）: %s', e)

        return True


# =============================
# 接続テスト（アカウント未登録状態でも呼べる）
# =============================

def test_connection(
    imap_host: str, imap_port: int, smtp_host: str, smtp_port: int,
    username: str, password: str, use_ssl: bool, ssl_verify: bool = True,
) -> list[dict]:
    """
    IMAP/SMTP接続テストを実行して各ステップの結果を返す
    ssl_verify=False: さくら/Xserverなど共用ホスティングのSSL証明書ホスト名不一致を回避
    戻り値: [{'step': '...', 'ok': True/False, 'error': '...'}, ...]
    """
    results = []

    # SSL証明書検証スキップ用コンテキスト
    ssl_ctx = None
    if not ssl_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    # --- IMAP接続 ---
    imap_client = None
    try:
        imap_client = imapclient.IMAPClient(
            host=imap_host,
            port=imap_port,
            ssl=use_ssl,
            ssl_context=ssl_ctx,
            timeout=15,
        )
        results.append({'step': 'IMAP接続', 'ok': True})
    except Exception as e:
        results.append({'step': 'IMAP接続', 'ok': False, 'error': str(e)})
        results.extend([
            {'step': 'IMAP認証', 'ok': False, 'error': '接続に失敗したためスキップ'},
            {'step': 'フォルダ取得', 'ok': False, 'error': '接続に失敗したためスキップ'},
        ])
        imap_client = None

    # --- IMAP認証 ---
    if imap_client:
        try:
            imap_client.login(username, password)
            results.append({'step': 'IMAP認証', 'ok': True})
        except Exception as e:
            results.append({'step': 'IMAP認証', 'ok': False, 'error': str(e)})
            imap_client = None
            results.append({'step': 'フォルダ取得', 'ok': False, 'error': '認証失敗のためスキップ'})

    # --- フォルダ取得 ---
    if imap_client:
        try:
            folders = imap_client.list_folders()
            results.append({'step': 'フォルダ取得', 'ok': True, 'count': len(folders)})
            imap_client.logout()
        except Exception as e:
            results.append({'step': 'フォルダ取得', 'ok': False, 'error': str(e)})

    # --- SMTP接続 ---
    smtp = None
    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl_ctx, timeout=15)
        else:
            smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            smtp.starttls(context=ssl_ctx)
        results.append({'step': 'SMTP接続', 'ok': True})
    except Exception as e:
        results.append({'step': 'SMTP接続', 'ok': False, 'error': str(e)})
        results.append({'step': 'SMTP認証', 'ok': False, 'error': '接続に失敗したためスキップ'})
        smtp = None

    # --- SMTP認証 ---
    if smtp:
        try:
            smtp.login(username, password)
            results.append({'step': 'SMTP認証', 'ok': True})
            smtp.quit()
        except Exception as e:
            results.append({'step': 'SMTP認証', 'ok': False, 'error': str(e)})

    return results
