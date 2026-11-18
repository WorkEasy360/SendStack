# syntax=docker/dockerfile:1

# --- builder: the Python environment, including the private sign-in connector ---
# git and an SSH client exist only in this stage: the runtime stage below copies
# /opt/venv from here and nothing else, so neither ships.
FROM python:3.13-slim AS builder

# PIP_NO_CACHE_DIR as an environment variable, not --no-cache-dir: it also
# reaches the pip that installs the connector's build backend, so no pip cache
# is written anywhere in this stage.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements/ requirements/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements/production.txt

# The WorkEasy360 sign-in connector comes from its PRIVATE repository, pinned to
# a commit (requirements/workeasy360.txt names it by its SSH URL;
# docs/workeasy360-sso.md, "Installing the connector"). This one RUN gets read
# access to it, one of two ways, and no other step does:
#  - a short-lived GitHub App installation token (Contents read-only on
#    workeasy360-sso alone, an hour at most) as the BuildKit secret
#    workeasy360_sso_token:
#      docker build --secret id=workeasy360_sso_token,env=WORKEASY360_SSO_TOKEN .
#    git's environment configuration (GIT_CONFIG_*, this RUN's environment only:
#    no file) rewrites the connector's SSH URL, and no other, to https with the
#    token, and empties credential.helper so git stores it nowhere;
#  - a developer's own SSH key, through their agent:
#      docker build --ssh default .
#    trusting only github.com's published host key (https://api.github.com/meta).
# Never a deploy key, and never a --build-arg (build arguments are recorded in
# the image). BuildKit mounts the secret (a tmpfs file) and the agent's socket
# into this RUN alone, so no token, key or .git reaches any layer: pip clones
# into a temporary directory and deletes it. With neither, the build stops
# here, saying what to pass. `pip check` proves the resolved tree consistent.
RUN --mount=type=secret,id=workeasy360_sso_token \
    --mount=type=ssh \
    set -eu; \
    if [ -s /run/secrets/workeasy360_sso_token ]; then \
        export GIT_CONFIG_COUNT=2 \
            GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
            GIT_CONFIG_KEY_1="url.https://x-access-token:$(cat /run/secrets/workeasy360_sso_token)@github.com/WorkEasy360/workeasy360-sso.git.insteadOf" \
            GIT_CONFIG_VALUE_1=ssh://git@github.com/WorkEasy360/workeasy360-sso.git; \
    elif [ -S "${SSH_AUTH_SOCK:-}" ]; then \
        echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /tmp/github_known_hosts; \
        export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/github_known_hosts -o GlobalKnownHostsFile=/dev/null"; \
    else \
        echo "No access to the private WorkEasy360 sign-in connector (WorkEasy360/workeasy360-sso). Build with a token that can read it, 'docker build --secret id=workeasy360_sso_token,env=WORKEASY360_SSO_TOKEN .' (docker compose: export WORKEASY360_SSO_TOKEN), or with your own SSH key, 'docker build --ssh default .'. See docs/workeasy360-sso.md, 'Installing the connector'." >&2; \
        exit 1; \
    fi; \
    GIT_TERMINAL_PROMPT=0 /opt/venv/bin/pip install -r requirements/workeasy360.txt; \
    rm -f /tmp/github_known_hosts; \
    /opt/venv/bin/pip check

# --- runtime ---
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

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
