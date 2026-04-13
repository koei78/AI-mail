#!/usr/bin/env bash
set -o errexit

# Celery worker + Beat をバックグラウンドで起動
celery -A config worker --beat --loglevel=info --concurrency=2 &

# gunicorn をフォアグラウンドで起動（Render はこのプロセスを監視する）
exec gunicorn config.wsgi:application --timeout 300 --workers 2
