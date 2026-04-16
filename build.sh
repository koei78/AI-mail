#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input --clear
python manage.py migrate --run-syncdb
sed -i 's/\r$//' start.sh
chmod +x start.sh
