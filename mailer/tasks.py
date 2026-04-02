"""
mailer/tasks.py
Celeryタスク: メール定期同期・AI仕分けスケジュール
"""
import logging

from celery import shared_task
from django.utils import timezone

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


@shared_task(bind=True, max_retries=2)
def classify_for_user_task(self, user_id: int):
    """
    指定ユーザーのメールをAIで分類する。
    check_classify_schedules_task から非同期で呼ばれる。
    """
    try:
        from .views import _classify_emails_for_user
        result = _classify_emails_for_user(user_id)
        from .models import ClassifySchedule
        ClassifySchedule.objects.filter(user_id=user_id).update(last_run_at=timezone.now())
        logger.info('自動仕分け完了: user_id=%s, 分類数=%s', user_id, result.get('classified', 0))
        return result
    except Exception as exc:
        logger.error('自動仕分けエラー: user_id=%s, error=%s', user_id, exc)
        raise self.retry(exc=exc, countdown=120)


@shared_task
def check_classify_schedules_task():
    """
    有効なスケジュールをチェックし、実行時刻を迎えたものをキューに追加する。
    Celery Beatから1分おきに呼ばれる。
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from .models import ClassifySchedule

    tz = ZoneInfo('Asia/Tokyo')
    now = datetime.now(tz)
    dispatched = 0

    schedules = ClassifySchedule.objects.filter(is_enabled=True).select_related('user')
    for schedule in schedules:
        if schedule.hour != now.hour or schedule.minute != now.minute:
            continue
        if schedule.weekdays and now.weekday() not in schedule.weekdays:
            continue
        # 当日既に実行済みならスキップ
        if schedule.last_run_at:
            last_run_jst = schedule.last_run_at.astimezone(tz)
            if last_run_jst.date() == now.date():
                continue
        classify_for_user_task.delay(schedule.user_id)
        dispatched += 1
        logger.info('スケジュール仕分けをキューに追加: user_id=%s', schedule.user_id)

    return {'dispatched': dispatched}
