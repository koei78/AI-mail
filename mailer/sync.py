"""
mailer/sync.py
フォルダ一覧と未読数をIMAPサーバーから同期するロジック

メール本文はIMAPサーバーに保存されているため、DBには同期しない。
DBに保存するのはフォルダ一覧と未読数のみ。
"""
import logging

from django.utils import timezone as django_timezone

from django.utils.dateparse import parse_datetime

from .imap_client import ImapConnectionError, MailClient
from .models import EmailCache, MailAccount, MailFolder


def _get_client_for_account(account):
    if getattr(account, 'auth_type', None) == 'microsoft_oauth2':
        from .graph_api_client import GraphMailClient, GraphConnectionError
        return GraphMailClient(account), GraphConnectionError
    return MailClient(account), ImapConnectionError

logger = logging.getLogger(__name__)

_SYNC_HIDE_FOLDERS = {
    '[gmail]', '[gmail]/all mail', '[gmail]/すべてのメール',
    '[gmail]/important', '[gmail]/重要',
    '[gmail]/starred', '[gmail]/スター付き',
    '[gmail]/chats', '[gmail]/チャット',
}


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
            folder, _ = MailFolder.objects.update_or_create(
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

        stale_ids = [
            f.pk for f in MailFolder.objects.filter(account=account)
            if f.remote_name.lower() in _SYNC_HIDE_FOLDERS
        ]
        if stale_ids:
            MailFolder.objects.filter(pk__in=stale_ids).delete()

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


def sync_emails_cache(folder: MailFolder, max_new: int = 500) -> dict:
    """
    フォルダのメールヘッダーをDBにキャッシュする。
    - 新着UIDをIMAPから取得してDB保存
    - サーバーから消えたUIDをDBから削除
    - 直近200件の既読/スターフラグを更新
    戻り値: {"added": N, "updated": N, "removed": N, "errors": [...]}
    """
    account = folder.account
    client, ConnectionError = _get_client_for_account(account)
    result = {'added': 0, 'updated': 0, 'removed': 0, 'errors': []}

    try:
        client.connect_imap()
        server_uids = set(client.get_folder_uids(folder.remote_name))
        cached_uids = set(EmailCache.objects.filter(folder=folder).values_list('uid', flat=True))

        # 新規UIDをDBに追加
        new_uids = server_uids - cached_uids
        if new_uids:
            fetch_uids = sorted(new_uids, reverse=True)[:max_new]
            emails_data = client.fetch_emails_by_uids(folder.remote_name, fetch_uids)
            objs = []
            for e in emails_data:
                received_at = e.get('received_at')
                if isinstance(received_at, str):
                    received_at = parse_datetime(received_at)
                objs.append(EmailCache(
                    account=account,
                    folder=folder,
                    uid=e['uid'],
                    message_id=e.get('message_id', ''),
                    subject=e.get('subject', ''),
                    from_address=e.get('from_address', ''),
                    to_addresses=e.get('to_addresses', []),
                    received_at=received_at,
                    is_read=e.get('is_read', False),
                    is_starred=e.get('is_starred', False),
                    has_attachments=e.get('has_attachments', False),
                    size=e.get('size', 0),
                    body_text='',
                    body_html='',
                    body_cached=False,
                ))
            EmailCache.objects.bulk_create(objs, ignore_conflicts=True)
            result['added'] = len(objs)

        # サーバーから削除されたメールをキャッシュからも削除
        deleted_uids = cached_uids - server_uids
        if deleted_uids:
            EmailCache.objects.filter(folder=folder, uid__in=deleted_uids).delete()
            result['removed'] = len(deleted_uids)

        # 直近200件の既読/スターフラグを更新
        existing_uids = sorted(cached_uids & server_uids, reverse=True)[:200]
        if existing_uids:
            flags_data = client.fetch_emails_by_uids(folder.remote_name, existing_uids)
            for e in flags_data:
                EmailCache.objects.filter(folder=folder, uid=e['uid']).update(
                    is_read=e.get('is_read', False),
                    is_starred=e.get('is_starred', False),
                )
            result['updated'] = len(flags_data)

    except ConnectionError as e:
        result['errors'].append(str(e))
        logger.error('メールキャッシュ同期エラー folder=%s: %s', folder.id, e)
    except Exception as e:
        result['errors'].append(str(e))
        logger.error('メールキャッシュ予期しないエラー folder=%s: %s', folder.id, e)
    finally:
        client.disconnect_imap()

    return result
