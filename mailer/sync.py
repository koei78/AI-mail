"""
mailer/sync.py
フォルダ一覧と未読数をIMAPサーバーから同期するロジック

メール本文はIMAPサーバーに保存されているため、DBには同期しない。
DBに保存するのはフォルダ一覧と未読数のみ。
"""
import logging

from django.utils import timezone as django_timezone

from .imap_client import ImapConnectionError, MailClient
from .models import MailAccount, MailFolder


def _get_client_for_account(account):
    if getattr(account, 'auth_type', None) == 'microsoft_oauth2':
        from .graph_api_client import GraphMailClient, GraphConnectionError
        return GraphMailClient(account), GraphConnectionError
    return MailClient(account), ImapConnectionError

logger = logging.getLogger(__name__)


def sync_account(account_id: int) -> dict:
    """
    1アカウントのフォルダ一覧と未読数を同期する。
    戻り値: {"updated": N, "errors": [...]}
    """
    try:
        account = MailAccount.objects.get(id=account_id, is_active=True)
    except MailAccount.DoesNotExist:
        return {'updated': 0, 'errors': [f'アカウントID {account_id} が見つかりません']}

    client, ConnectionError = _get_client_for_account(account)
    result = {'updated': 0, 'errors': []}

    try:
        client.connect_imap()

        remote_folders = client.fetch_folders()
        for folder_data in remote_folders:
            folder, _ = MailFolder.objects.get_or_create(
                account=account,
                remote_name=folder_data['remote_name'],
                defaults={
                    'name': folder_data['name'],
                    'folder_type': folder_data['folder_type'],
                },
            )
            try:
                unread = client.get_folder_unread_count(folder_data['remote_name'])
                if folder.unread_count != unread:
                    folder.unread_count = unread
                    folder.save(update_fields=['unread_count'])
                    result['updated'] += 1
            except Exception as e:
                logger.warning('未読数取得失敗 folder=%s: %s', folder_data['remote_name'], e)

        account.last_synced_at = django_timezone.now()
        account.save(update_fields=['last_synced_at'])

    except ConnectionError as e:
        result['errors'].append(str(e))
        logger.error('アカウント %s の同期エラー: %s', account_id, e)
    finally:
        client.disconnect_imap()

    return result


def sync_folder(account_id: int, folder_id: int) -> dict:
    """特定フォルダの未読数をIMAPから更新する"""
    try:
        account = MailAccount.objects.get(id=account_id, is_active=True)
        db_folder = MailFolder.objects.get(id=folder_id, account=account)
    except MailAccount.DoesNotExist:
        return {'updated': 0, 'errors': [f'アカウントID {account_id} が見つかりません']}
    except MailFolder.DoesNotExist:
        return {'updated': 0, 'errors': [f'フォルダID {folder_id} が見つかりません']}

    client, ConnectionError = _get_client_for_account(account)
    result = {'updated': 0, 'errors': []}

    try:
        client.connect_imap()
        unread = client.get_folder_unread_count(db_folder.remote_name)
        if db_folder.unread_count != unread:
            db_folder.unread_count = unread
            db_folder.save(update_fields=['unread_count'])
            result['updated'] = 1
    except ConnectionError as e:
        result['errors'].append(str(e))
        logger.error('フォルダ %s の同期エラー: %s', folder_id, e)
    finally:
        client.disconnect_imap()

    return result
