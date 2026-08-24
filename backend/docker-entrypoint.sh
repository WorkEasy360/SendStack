#!/bin/sh
set -e

# Only the web process runs migrations/collectstatic -- celery worker/beat
# containers use this same image but skip straight to exec so they don't
# race the web container over migrations on startup.
if [ "$1" = "gunicorn" ]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput

    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

exec "$@"
