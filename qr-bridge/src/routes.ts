import { Router, type Response } from "express";
import {
    GROUP_MEMBERS_MAX_GROUPS,
    NUMBER_LOOKUP_MAX_PER_REQUEST,
    SessionNotConnected,
    type WhatsAppBridgeProvider,
} from "./providers/base.js";

/**
 * One answer for every lookup that failed. 409 for "no live socket", which
 * Django reports as the number needing to be reconnected; 502 for anything
 * WhatsApp itself refused or never answered, which Django reports verbatim.
 */
function respondLookupError(res: Response, err: unknown): void {
    if (err instanceof SessionNotConnected) {
        res.status(409).json({ error: err.message, code: "not_connected" });
        return;
    }
    res.status(502).json({ error: (err as Error)?.message || "The lookup failed.", code: "lookup_failed" });
}

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
        // `send_operation_id` (when Django sends one) rides through on req.body
        // -- see OutgoingMessage.send_operation_id. The provider dedups on it.
        const result = await provider.sendMessage(req.params.sessionId, req.body ?? {});
        // 504 distinguishes "we don't know if this was sent" from a definite
        // rejection, so the caller can decline to retry it into a duplicate.
        const status = result.success ? 200 : result.ambiguous ? 504 : 422;
        res.status(status).json(result);
    });

    /**
     * What became of one send, by its send_operation_id. Django's recovery
     * sweep calls this after a lost response to decide whether a stranded
     * message was delivered, never went out, or is genuinely unknown.
     *
     *   200 {"known": true,  "sent": true,  "provider_message_id": "...", "ambiguous": false}
     *   200 {"known": true,  "sent": false, "provider_message_id": null,  "ambiguous": true}
     *   404 {"known": false} -- the bridge never saw this operation id
     */
    router.get("/sessions/:sessionId/operations/:operationId", (req, res) => {
        const status = provider.getOperationStatus(req.params.operationId);
        res.status(status.known ? 200 : 404).json(status);
    });

    /**
     * Number filter: which of these numbers are registered on WhatsApp.
     *
     *   POST {"numbers": ["919876543210", ...]}
     *   -> 200 {"results": [{"number": "...", "exists": true, "jid": "...@s.whatsapp.net"}, ...]}
     *   -> 400 malformed or over the per-request cap
     *   -> 409 session not connected     -> 502 WhatsApp refused or timed out
     *
     * Django owns the batching above the cap and the pause between calls;
     * see backend/apps/leadtools/tasks.py.
     */
    router.post("/sessions/:sessionId/numbers/check", async (req, res) => {
        const numbers = req.body?.numbers;
        if (!Array.isArray(numbers) || numbers.length === 0) {
            res.status(400).json({ error: "numbers must be a non-empty array." });
            return;
        }
        if (numbers.length > NUMBER_LOOKUP_MAX_PER_REQUEST) {
            res.status(400).json({ error: `At most ${NUMBER_LOOKUP_MAX_PER_REQUEST} numbers per request.` });
            return;
        }
        try {
            const results = await provider.checkNumbers(req.params.sessionId, numbers.map(String));
            res.json({ results });
        } catch (err) {
            respondLookupError(res, err);
        }
    });

    /**
     * Group grabber, step one: every group the linked number is in.
     *   -> 200 {"groups": [{id, subject, member_count, announce, is_community, created_at}]}
     */
    router.get("/sessions/:sessionId/groups", async (req, res) => {
        try {
            const groups = await provider.listGroups(req.params.sessionId);
            res.json({ groups });
        } catch (err) {
            respondLookupError(res, err);
        }
    });

    /**
     * Group grabber, step two: the members of the named groups.
     *   POST {"group_ids": ["1203...@g.us", ...]}
     *   -> 200 {"groups": [{id, subject, members: [{phone, jid, name, is_admin}], hidden_count}]}
     */
    router.post("/sessions/:sessionId/groups/members", async (req, res) => {
        const groupIds = req.body?.group_ids;
        if (!Array.isArray(groupIds) || groupIds.length === 0 || !groupIds.every((id) => typeof id === "string")) {
            res.status(400).json({ error: "group_ids must be a non-empty array of group ids." });
            return;
        }
        if (groupIds.length > GROUP_MEMBERS_MAX_GROUPS) {
            res.status(400).json({ error: `At most ${GROUP_MEMBERS_MAX_GROUPS} groups per request.` });
            return;
        }
        try {
            const groups = await provider.getGroupMembers(req.params.sessionId, groupIds);
            res.json({ groups });
        } catch (err) {
            respondLookupError(res, err);
        }
    });

    /**
     * Diagnostics: relay the native-flow matrix to one real recipient and
     * report what the wire actually did (see buttonLab.ts).
     *
     * Not part of the contract qr_bridge.py speaks -- Django never calls this.
     * It is here rather than in a standalone script because a standalone script
     * would have to open its own socket over the same auth directory, which is
     * the exact condition that corrupted this project's Signal sessions.
     */
    router.post("/diagnostics/button-lab/:sessionId", async (req, res) => {
        const run = (provider as { runButtonDiagnostic?: Function }).runButtonDiagnostic;
        if (typeof run !== "function") {
            res.status(501).json({ error: "This provider has no button diagnostic." });
            return;
        }
        const { to, body, footer, buttons, variants, observe_ms, include_control_text, observe_only } =
            req.body ?? {};
        // Observe-only relays nothing, so it needs a recipient to resolve and
        // nothing else; demanding a body and buttons it will never send would
        // just make the caller invent them.
        if (!to || (!observe_only && (!body || !Array.isArray(buttons) || buttons.length === 0))) {
            res.status(400).json({
                error: observe_only
                    ? "to is required."
                    : "to, body, and a non-empty buttons array are required.",
            });
            return;
        }
        try {
            const report = await run.call(provider, req.params.sessionId, {
                to,
                body: body ?? "",
                footer,
                buttons: buttons ?? [],
                variants,
                observeMs: observe_ms,
                includeControlText: include_control_text,
                observeOnly: observe_only,
            });
            res.json(report);
        } catch (err) {
            res.status(422).json({ error: (err as Error).message });
        }
    });

    return router;
}
