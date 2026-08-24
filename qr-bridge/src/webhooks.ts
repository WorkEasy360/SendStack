import crypto from "node:crypto";
import { config } from "./config.js";

function sign(rawBody: string): string {
    return "sha256=" + crypto.createHmac("sha256", config.webhookSecret).update(rawBody).digest("hex");
}

async function post(url: string, body: unknown, signatureHeaderName: string, logger: { warn: (msg: string) => void }): Promise<void> {
    const rawBody = JSON.stringify(body);
    try {
        const response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                [signatureHeaderName]: sign(rawBody),
            },
            body: rawBody,
        });
        if (!response.ok) {
            logger.warn(`Webhook POST to ${url} failed with HTTP ${response.status}`);
        }
    } catch (err) {
        logger.warn(`Webhook POST to ${url} failed: ${(err as Error).message}`);
    }
}

export interface ConnectionWebhookEvent {
    session_id: string;
    status: "CONNECTED" | "ERROR" | "EXPIRED" | "DISCONNECTED";
    phone_number?: string | null;
    display_name?: string | null;
    provider_account_id?: string | null;
    error?: string | null;
}

/** Django's WhatsAppConnectionWebhookView reads this header (see qr_bridge.py). */
export function postConnectionWebhook(url: string, event: ConnectionWebhookEvent, logger: { warn: (msg: string) => void }) {
    return post(url, event, "X-Bridge-Signature", logger);
}

export interface MessageWebhookPayload {
    messages?: Array<Record<string, unknown>>;
    statuses?: Array<Record<string, unknown>>;
}

/**
 * Django's shared WhatsAppWebhookView (used by both Cloud API and this
 * bridge) hardcodes Meta's header name for the message webhook -- see
 * qr_bridge.py module docstring for why the two webhook kinds differ.
 */
export function postMessageWebhook(url: string, payload: MessageWebhookPayload, logger: { warn: (msg: string) => void }) {
    return post(url, payload, "X-Hub-Signature-256", logger);
}
