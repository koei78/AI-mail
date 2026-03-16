"""
mailer/tasks.py
Celeryタスク: メール定期同期
"""
import logging

from celery import shared_task

from .models import MailAccount
from .sync import sync_account

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def sync_account_task(self, account_id: int):
    """
    1アカウントを同期する。
    失敗時は60秒後に最大3回リトライする。
    """
    try:
        result = sync_account(account_id)
        logger.info(
            'アカウント %s の同期完了: 新規=%s, 更新=%s, エラー=%s件',
            account_id,
            result['new'],
            result['updated'],
            len(result['errors']),
        )
        return result
    except Exception as exc:
        logger.error('アカウント %s の同期でエラー発生: %s', account_id, exc)
        # 60秒後にリトライ（最大3回）
        raise self.retry(exc=exc, countdown=60)


@shared_task
def sync_all_accounts_task():
    """
    全ユーザーのアクティブなメールアカウントを同期する。
    Celery Beatから15分おきに呼ばれる。
    """
    active_accounts = MailAccount.objects.filter(is_active=True).values_list('id', flat=True)
    dispatched = 0

    for account_id in active_accounts:
        # 各アカウントを個別タスクとして非同期実行
        sync_account_task.delay(account_id)
        dispatched += 1

    logger.info('全アカウント同期: %s アカウントをキューに追加しました', dispatched)
    return {'dispatched': dispatched}
