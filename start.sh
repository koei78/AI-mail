#!/usr/bin/env bash
set -o errexit

# Celery worker + Beat をバックグラウンドで起動（stderr を stdout にリダイレクト）
echo "=== Starting Celery ==="
celery -A config worker --beat --loglevel=info --concurrency=2 --without-gossip --without-mingle --without-heartbeat 2>&1 &
CELERY_PID=$!
sleep 3
kill -0 $CELERY_PID 2>&1 && echo "=== Celery running (PID: $CELERY_PID) ===" || echo "=== Celery crashed ==="

# gunicorn をフォアグラウンドで起動（Render はこのプロセスを監視する）
echo "=== Starting Gunicorn ==="
exec gunicorn config.wsgi:application --timeout 300 --workers 2
