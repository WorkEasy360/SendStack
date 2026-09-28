# Build context is the repo root (see docker-compose.yml) so this stage can
# reach frontend/ to build the SPA before baking it into the nginx image.
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html

# The config is no longer a single default.conf. docker-entrypoint.sh assembles
# conf.d at container start from these parts, because which server layout is
# correct depends on whether a certificate exists yet -- see that script and
# nginx/server-tls.conf for why this cannot be decided at build time.
COPY nginx/head.conf         /etc/nginx/app/head.conf
COPY nginx/locations.conf    /etc/nginx/app/locations.conf
COPY nginx/server-http.conf  /etc/nginx/app/server-http.conf
COPY nginx/server-tls.conf   /etc/nginx/app/server-tls.conf
COPY nginx/docker-entrypoint.sh /entrypoint.sh

# The ACME webroot. Created here so the plain-HTTP layout can serve the
# challenge location even on a host that has never run certbot -- an nginx
# whose `root` points at a missing directory answers 404, which during
# issuance reads as a mysterious DNS or firewall fault.
RUN mkdir -p /var/www/certbot \
    && chmod +x /entrypoint.sh

EXPOSE 80 443

# Answered by nginx itself rather than proxied to the backend: this reports on
# whether nginx is serving, which is what the container's health means. It is
# also the one target that behaves identically under both layouts -- probing
# `/` would follow a 301 to https once TLS is on, fail certificate
# verification against 127.0.0.1, and mark a perfectly healthy proxy as down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O- http://127.0.0.1/nginx-health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
