function required(name: string, value: string | undefined): string {
    if (!value) {
        throw new Error(`Missing required environment variable: ${name}`);
    }
    return value;
}

export const config = {
    port: Number(process.env.PORT || 4001),
    // Must match Django's QR_PROVIDER_API_KEY / QR_PROVIDER_WEBHOOK_SECRET --
    // this bridge and the backend authenticate to each other with the same
    // two shared secrets (see backend/apps/whatsapp/providers/qr_bridge.py).
    apiKey: required("QR_PROVIDER_API_KEY", process.env.QR_PROVIDER_API_KEY),
    webhookSecret: required("QR_PROVIDER_WEBHOOK_SECRET", process.env.QR_PROVIDER_WEBHOOK_SECRET),
    dataDir: process.env.QR_BRIDGE_DATA_DIR || "./data",
    qrTtlSeconds: Number(process.env.WHATSAPP_QR_SESSION_TTL_SECONDS || 120),
    logLevel: process.env.QR_BRIDGE_LOG_LEVEL || "info",
};
