"""
mailer/sync.py
IMAPサーバーからDBへのメール同期ロジック
"""
import logging
from datetime import timezone

from django.utils import timezone as django_timezone

from .imap_client import ImapConnectionError, MailClient
from .models import Email, MailAccount, MailFolder

logger = logging.getLogger(__name__)


def sync_account(account_id: int) -> dict:
    """
    1アカウントの全フォルダを同期する。
    1. fetch_folders() でフォルダ一覧を取得してDBに保存
    2. 各フォルダで fetch_emails() を呼んでDBにupsert
    3. message_id が既存なら skip、なければ INSERT
    戻り値: {"new": N, "updated": N, "errors": [...]}
    """
    try:
        account = MailAccount.objects.get(id=account_id, is_active=True)
    except MailAccount.DoesNotExist:
        return {'new': 0, 'updated': 0, 'errors': [f'アカウントID {account_id} が見つかりません']}

    client = MailClient(account)
    result = {'new': 0, 'updated': 0, 'errors': []}

    try:
        client.connect_imap()

        # フォルダ一覧を取得してDBに保存
        remote_folders = client.fetch_folders()
        db_folders = {}

        for folder_data in remote_folders:
            folder, _ = MailFolder.objects.get_or_create(
                account=account,
                remote_name=folder_data['remote_name'],
                defaults={
                    'name': folder_data['name'],
                    'folder_type': folder_data['folder_type'],
                },
            )
            db_folders[folder_data['remote_name']] = folder

        # 各フォルダのメールを同期
        for remote_name, db_folder in db_folders.items():
            folder_result = _sync_single_folder(client, account, db_folder, remote_name)
            result['new'] += folder_result['new']
            result['updated'] += folder_result['updated']
            result['errors'].extend(folder_result['errors'])

        # 最終同期日時を更新
        account.last_synced_at = django_timezone.now()
        account.save(update_fields=['last_synced_at'])

    except ImapConnectionError as e:
        result['errors'].append(str(e))
        logger.error('アカウント %s の同期エラー: %s', account_id, e)
    finally:
        client.disconnect_imap()

    return result


def sync_folder(account_id: int, folder_id: int) -> dict:
    """特定フォルダだけ同期する"""
    try:
        account = MailAccount.objects.get(id=account_id, is_active=True)
        db_folder = MailFolder.objects.get(id=folder_id, account=account)
    except MailAccount.DoesNotExist:
        return {'new': 0, 'updated': 0, 'errors': [f'アカウントID {account_id} が見つかりません']}
    except MailFolder.DoesNotExist:
        return {'new': 0, 'updated': 0, 'errors': [f'フォルダID {folder_id} が見つかりません']}

    client = MailClient(account)
    result = {'new': 0, 'updated': 0, 'errors': []}

    try:
        client.connect_imap()
        result = _sync_single_folder(client, account, db_folder, db_folder.remote_name)
    except ImapConnectionError as e:
        result['errors'].append(str(e))
        logger.error('フォルダ %s の同期エラー: %s', folder_id, e)
    finally:
        client.disconnect_imap()

    return result


def _sync_single_folder(
    client: MailClient,
    account: MailAccount,
    db_folder: MailFolder,
    remote_name: str,
) -> dict:
    """1フォルダのメールをDBにupsertする"""
    result = {'new': 0, 'updated': 0, 'errors': []}

    try:
        # 最大200件取得（必要に応じてオフセットでページング可能）
        emails_data = client.fetch_emails(remote_name, limit=200, offset=0)
    except ImapConnectionError as e:
        result['errors'].append(f'{remote_name}: {e}')
        return result

    unread_count = 0

    for data in emails_data:
        message_id = data.get('message_id', '').strip()
        if not message_id:
            # Message-ID がない場合は uid+folder で代替キーを生成
            message_id = f'uid-{data["uid"]}-folder-{db_folder.id}'

        try:
            existing = Email.objects.filter(message_id=message_id).first()

            if existing:
                # 既読状態など変化した可能性のあるフィールドを更新
                updated = False
                if existing.is_read != data.get('is_read', False):
                    existing.is_read = data.get('is_read', False)
                    updated = True
                if existing.is_starred != data.get('is_starred', False):
                    existing.is_starred = data.get('is_starred', False)
                    updated = True
                if updated:
                    existing.save(update_fields=['is_read', 'is_starred'])
                    result['updated'] += 1
            else:
                # 新規メールを INSERT
                import dateutil.parser as dp
                received_at = None
                if data.get('received_at'):
                    try:
                        received_at = dp.parse(data['received_at'])
                        if received_at.tzinfo is None:
                            received_at = received_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        received_at = django_timezone.now()
                else:
                    received_at = django_timezone.now()

                Email.objects.create(
                    account=account,
                    folder=db_folder,
                    uid=data['uid'],
                    message_id=message_id,
                    subject=data.get('subject', ''),
                    from_address=data.get('from_address', ''),
                    to_addresses=data.get('to_addresses', []),
                    cc_addresses=[],
                    is_read=data.get('is_read', False),
                    is_starred=data.get('is_starred', False),
                    received_at=received_at,
                )
                result['new'] += 1

            if not data.get('is_read', False):
                unread_count += 1

        except Exception as e:
            logger.error('メール保存エラー uid=%s: %s', data.get('uid'), e)
            result['errors'].append(f'uid={data.get("uid")}: {e}')

    # フォルダの未読数を更新
    db_folder.unread_count = unread_count
    db_folder.save(update_fields=['unread_count'])

    return result
