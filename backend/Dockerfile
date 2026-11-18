# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/production.txt

# The WorkEasy360 sign-in connector lives in a PRIVATE repository and is
# installed from it by commit (docs/workeasy360-sso.md). The read-only deploy
# key is forwarded by BuildKit for this one step and never stored in a layer:
#
#     docker build --ssh default ...           (ssh-agent holding the deploy key)
#     docker compose build                     (docker-compose.yml forwards it)
#
# GitHub's host key is pinned from https://api.github.com/meta rather than
# trusted on first use.
RUN --mount=type=ssh     mkdir -p -m 0700 /root/.ssh     && echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /root/.ssh/known_hosts     && pip install --no-cache-dir --no-deps -r requirements/workeasy360.txt     && rm -rf /root/.ssh

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
