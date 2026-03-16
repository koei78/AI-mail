"""
mailer/imap_client.py
IMAP/SMTPの接続・操作をまとめたクライアントクラス
"""
import smtplib
import logging
import ssl
from email import policy
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser

import imapclient

from .models import Email, MailAccount

logger = logging.getLogger(__name__)


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


def _parse_body(msg) -> tuple[str, str, bool]:
    """
    MIMEメッセージから本文（text/plain, text/html）と
    添付ファイルの有無を取得する
    戻り値: (body_text, body_html, has_attachments)
    """
    body_text = ''
    body_html = ''
    has_attachments = False

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get('Content-Disposition', ''))

            # 添付ファイルはスキップして has_attachments フラグを立てる
            if 'attachment' in disposition:
                has_attachments = True
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

    return body_text, body_html, has_attachments


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

    def fetch_emails(
        self, folder_remote_name: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """
        指定フォルダのメール一覧を取得する（新しい順）
        戻り値: メタ情報のリスト（本文は含まない）
        """
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name, readonly=True)
            # 全メッセージのUIDを取得
            uids = self._imap.search(['ALL'])
        except Exception as e:
            raise ImapConnectionError(f'フォルダ選択エラー: {e}') from e

        # 新しい順にソートしてページング
        uids = sorted(uids, reverse=True)
        page_uids = uids[offset: offset + limit]

        if not page_uids:
            return []

        try:
            fetch_data = self._imap.fetch(
                page_uids,
                ['ENVELOPE', 'FLAGS', 'RFC822.SIZE'],
            )
        except Exception as e:
            raise ImapConnectionError(f'メール取得エラー: {e}') from e

        emails = []
        for uid, data in fetch_data.items():
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
        body_text, body_html, has_attachments = _parse_body(msg)

        cc_header = msg.get('Cc', '')
        cc_addresses = _parse_addresses(cc_header)

        return {
            'body_text': body_text,
            'body_html': body_html,
            'has_attachments': has_attachments,
            'cc_addresses': cc_addresses,
        }

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
        """メールを削除（ゴミ箱へ移動または完全削除）する"""
        self._require_imap()
        try:
            self._imap.select_folder(folder_remote_name)
            self._imap.add_flags([uid], [b'\\Deleted'])
            self._imap.expunge()
            return True
        except Exception as e:
            logger.error('削除エラー uid=%s: %s', uid, e)
            return False

    # --------------------------------------------------
    # SMTP送信
    # --------------------------------------------------

    def _build_smtp(self):
        """SMTPサーバーへ接続してログインしたインスタンスを返す"""
        password = self.account.get_password()
        ssl_context = None
        if not self.account.ssl_verify:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        try:
            if self.account.use_ssl:
                # ポート465 SSL
                smtp = smtplib.SMTP_SSL(
                    self.account.smtp_host,
                    self.account.smtp_port,
                    context=ssl_context,
                    timeout=30,
                )
            else:
                # ポート587 STARTTLS
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
    ) -> bool:
        """
        メールを送信する
        body_html が指定された場合は multipart/alternative で送信
        """
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'{self.account.display_name} <{self.account.email_address}>'
        msg['To'] = ', '.join(to)
        if cc:
            msg['Cc'] = ', '.join(cc)

        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        all_recipients = to + (cc or [])

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, all_recipients, msg.as_bytes())
            smtp.quit()
            return True
        except SmtpConnectionError:
            raise
        except Exception as e:
            logger.error('送信エラー: %s', e)
            raise SmtpConnectionError(f'送信エラー: {e}') from e

    def reply_email(self, original: Email, body: str) -> bool:
        """
        メールに返信する
        In-Reply-To / References ヘッダーを設定してスレッドを継続する
        """
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'Re: {original.subject}' if not original.subject.startswith('Re:') else original.subject
        msg['From'] = f'{self.account.display_name} <{self.account.email_address}>'
        msg['To'] = original.from_address
        msg['In-Reply-To'] = original.message_id
        msg['References'] = original.message_id

        # 引用文を追加
        quoted = '\n'.join(f'> {line}' for line in original.body_text.splitlines())
        full_body = f'{body}\n\n{quoted}'
        msg.attach(MIMEText(full_body, 'plain', 'utf-8'))

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, [original.from_address], msg.as_bytes())
            smtp.quit()
            return True
        except SmtpConnectionError:
            raise
        except Exception as e:
            raise SmtpConnectionError(f'返信エラー: {e}') from e

    def forward_email(self, original: Email, to: list[str], body: str) -> bool:
        """メールを転送する"""
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'Fwd: {original.subject}'
        msg['From'] = f'{self.account.display_name} <{self.account.email_address}>'
        msg['To'] = ', '.join(to)

        # 転送ヘッダー付きの本文
        header = (
            f'\n\n---------- 転送メッセージ ----------\n'
            f'差出人: {original.from_address}\n'
            f'日時: {original.received_at}\n'
            f'件名: {original.subject}\n'
            f'宛先: {", ".join(original.to_addresses)}\n\n'
        )
        full_body = f'{body}{header}{original.body_text}'
        msg.attach(MIMEText(full_body, 'plain', 'utf-8'))

        try:
            smtp = self._build_smtp()
            smtp.sendmail(self.account.email_address, to, msg.as_bytes())
            smtp.quit()
            return True
        except SmtpConnectionError:
            raise
        except Exception as e:
            raise SmtpConnectionError(f'転送エラー: {e}') from e


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
