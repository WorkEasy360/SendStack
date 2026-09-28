FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/production.txt

COPY . .

# Which commit this image is. Passed at build time
# (--build-arg APP_BUILD=$(git rev-parse --short HEAD)) rather than read from a
# .git directory, which is not in the build context and must not be: shipping
# the repository history inside the image would ship every secret ever
# committed to it. Reported by /health/live/, /health/ready/ and the support
# diagnostics view, so a running container can be tied to a commit during an
# incident.
ARG APP_BUILD=""
ARG APP_VERSION="1.0.0"
ENV APP_BUILD=${APP_BUILD} \
    APP_VERSION=${APP_VERSION}

RUN chmod +x docker-entrypoint.sh \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health/live/ || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
