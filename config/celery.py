import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')

# 登録されたDjangoアプリからタスクを自動的に検出する
app.autodiscover_tasks()
