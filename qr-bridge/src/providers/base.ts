export type SessionStatus = "CONNECTING" | "QR_READY" | "CONNECTED" | "ERROR" | "EXPIRED" | "DISCONNECTED";

/** Mirrors the JSON shape documented in backend/apps/whatsapp/providers/qr_bridge.py. */
export interface SessionSnapshot {
    session_id: string;
    status: SessionStatus;
    qr: string | null;
    expires_at: string | null;
    phone_number: string | null;
    display_name: string | null;
    provider_account_id: string | null;
    error: string | null;
}

export interface CreateSessionInput {
    accountId: number;
    connectionWebhookUrl: string;
    messageWebhookUrl: string;
}

/** One tappable button, in the shape Django's core/buttons.py normalises to. */
export interface InteractiveButton {
    type: "REPLY" | "URL";
    id: string;
    title: string;
    url: string;
}

export interface OutgoingMessage {
    to: string;
    type: string;
    text?: { body: string };
    media?: { link: string; caption?: string; filename?: string };
    /** Set when type === "interactive": body text plus the buttons under it. */
    interactive?: { body: string; buttons: InteractiveButton[] };
}

export interface SendResult {
    success: boolean;
    provider_message_id: string | null;
    error: string | null;
    /**
     * True when we gave up on a send that had already been handed to
     * WhatsApp, so we cannot say whether it went out. Django must not retry
     * these (it would risk a duplicate) -- routes.ts answers them with 504.
     */
    ambiguous?: boolean;
}

/**
 * What routes.ts depends on. Only baileysProvider.ts (the sole
 * implementation) is allowed to know anything about Baileys or the
 * WhatsApp multi-device protocol -- routes/index/webhooks stay
 * engine-agnostic, mirroring the same boundary Django keeps against this
 * whole service.
 */
export interface WhatsAppBridgeProvider {
    createSession(input: CreateSessionInput): Promise<SessionSnapshot>;
    getSession(sessionId: string): SessionSnapshot | undefined;
    disconnectSession(sessionId: string): Promise<void>;
    sendMessage(sessionId: string, message: OutgoingMessage): Promise<SendResult>;
    getAccountInfo(sessionId: string): SessionSnapshot | undefined;
}
