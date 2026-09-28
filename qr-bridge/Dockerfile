# 20.19 is a floor, not a preference. The native-flow button engine reaches
# Baileys through require(), Baileys 6.7.24 is ESM, and require(esm) landed in
# Node 20.19. On an older 20.x the bridge starts, sends text and media fine, and
# fails only when a button message is sent. See src/nativeFlow.ts.
FROM node:20.19-slim AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:20.19-slim

ENV NODE_ENV=production \
    QR_BRIDGE_DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY --from=build /app/dist ./dist

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 4001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:4001/health || exit 1

CMD ["node", "dist/index.js"]
