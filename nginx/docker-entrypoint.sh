#!/bin/sh
# Chooses the server layout at container start, then runs nginx.
#
# WHY THIS EXISTS
#
# nginx will not start if `ssl_certificate` names a file that does not exist.
# On a brand-new host it never does: the certificate can only be issued after
# Let's Encrypt has fetched a token over port 80, which requires nginx to
# already be serving. A config file that hard-codes TLS is therefore a
# deadlock on exactly the deploy where you most need the site to come up.
#
# So the layout is decided here, from what is actually on disk:
#
#   certificate present  ->  server-tls.conf   (:80 redirect + :443 app)
#   certificate absent   ->  server-http.conf  (:80 app, ACME reachable)
#
# The transition is a container restart, nothing more. deploy.sh and
# docs/DEPLOY.md both rely on that: issue the certificate, restart nginx, and
# the same image serves https.
set -e

APP_DIR=/etc/nginx/app
CONF_D=/etc/nginx/conf.d

# The stock image ships a default.conf that would otherwise also bind :80 and
# race ours for server_name _.
rm -f "$CONF_D"/default.conf

cp "$APP_DIR/head.conf" "$CONF_D/00-head.conf"

DOMAIN="${DOMAIN:-}"
CERT_PATH="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"

if [ -z "$DOMAIN" ]; then
    echo "[nginx] DOMAIN is unset -- serving plain HTTP on :80."
    echo "[nginx] Set DOMAIN and CERTBOT_EMAIL in .env, then see docs/DEPLOY.md to issue a certificate."
    cp "$APP_DIR/server-http.conf" "$CONF_D/10-app.conf"
elif [ ! -s "$CERT_PATH" ]; then
    echo "[nginx] DOMAIN=$DOMAIN but no certificate at $CERT_PATH -- serving plain HTTP on :80."
    echo "[nginx] This is the expected state before first issuance. The ACME challenge path is being served;"
    echo "[nginx] run the certbot command in docs/DEPLOY.md, then 'docker compose restart nginx'."
    cp "$APP_DIR/server-http.conf" "$CONF_D/10-app.conf"
else
    echo "[nginx] Certificate found for $DOMAIN -- serving HTTPS on :443, redirecting :80."
    # Restricted to DOMAIN on purpose: an unrestricted envsubst would replace
    # nginx's own $host, $scheme, $request_uri and $hsts_header with empty
    # strings, producing a config that starts and silently misbehaves.
    envsubst '${DOMAIN}' < "$APP_DIR/server-tls.conf" > "$CONF_D/10-app.conf"

    # --- Picking up renewals ------------------------------------------
    #
    # certbot renews in its own container and writes to the shared volume.
    # nginx reads the certificate once, at startup, and would go on serving
    # the expired one until something reloaded it -- so this reloads on a
    # timer. Cheap (a reload is a signal, not a restart, and drops no
    # connection) and far more robust than trying to have the certbot
    # container reach back into this one.
    #
    # Twice a day against a 60-day renewal window: the certificate is
    # replaced with 30 days still to run, so there are ~60 chances to notice.
    while :; do
        sleep 12h &
        wait $!
        echo "[nginx] periodic reload (picks up any renewed certificate)"
        nginx -s reload || true
    done &
fi

# nginx -t here rather than letting a bad config surface as a crash loop: the
# error text is the useful part and this way it is the last thing in the log.
nginx -t

exec nginx -g 'daemon off;'
