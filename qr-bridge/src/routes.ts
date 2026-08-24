import { Router } from "express";
import type { WhatsAppBridgeProvider } from "./providers/base.js";

/** Implements the HTTP contract documented in backend/apps/whatsapp/providers/qr_bridge.py. */
export function buildRoutes(provider: WhatsAppBridgeProvider): Router {
    const router = Router();

    router.post("/sessions", async (req, res) => {
        const { account_id, connection_webhook_url, message_webhook_url } = req.body ?? {};
        if (!account_id || !connection_webhook_url || !message_webhook_url) {
            res.status(400).json({ error: "account_id, connection_webhook_url, and message_webhook_url are required." });
            return;
        }
        const snapshot = await provider.createSession({
            accountId: account_id,
            connectionWebhookUrl: connection_webhook_url,
            messageWebhookUrl: message_webhook_url,
        });
        res.status(snapshot.status === "ERROR" ? 502 : 201).json(snapshot);
    });

    router.get("/sessions/:sessionId", (req, res) => {
        const snapshot = provider.getSession(req.params.sessionId);
        if (!snapshot) {
            res.status(404).json({ error: "Unknown session." });
            return;
        }
        res.json(snapshot);
    });

    router.delete("/sessions/:sessionId", async (req, res) => {
        await provider.disconnectSession(req.params.sessionId);
        res.status(204).send();
    });

    router.post("/sessions/:sessionId/messages", async (req, res) => {
        const result = await provider.sendMessage(req.params.sessionId, req.body ?? {});
        // 504 distinguishes "we don't know if this was sent" from a definite
        // rejection, so the caller can decline to retry it into a duplicate.
        const status = result.success ? 200 : result.ambiguous ? 504 : 422;
        res.status(status).json(result);
    });

    return router;
}
