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
    /**
     * What this session has actually done with the messages sent to it, since
     * the process started. Present on a status read, absent everywhere a
     * snapshot is built to describe a pairing attempt.
     *
     * `status` alone cannot answer "is this number working": a session whose
     * stored Signal state has gone bad stays CONNECTED and receives everything
     * while delivering none of it. `undecryptable` climbing while `forwarded`
     * stands still is that failure, and it is the only place it is visible.
     */
    inbound?: InboundHealth;
}

export interface InboundHealth {
    forwarded: number;
    dropped: number;
    undecryptable: number;
    /** ISO 8601, or null if this session has never forwarded anything. */
    last_forwarded_at: string | null;
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

/**
 * A poll, in the shape Django's core/interactive.py normalises to.
 *
 * `selectable_count` carries the multi-answer switch: 1 for one choice, the
 * option count for any number. Django computes it because the poll's own shape
 * is what determines it — see qr_bridge.py's send_poll_message.
 */
export interface OutgoingPoll {
    question: string;
    options: string[];
    selectable_count: number;
}

/**
 * Which wire shape a native button message is relayed in.
 *
 * WhatsApp has carried tappable buttons in several different envelopes over
 * the years and has retired them at different times for different sender
 * types, so which one a recipient's client actually *renders* is an empirical
 * question rather than a documented one. Keeping the choice a parameter means
 * it can be answered by measurement against a real device instead of by
 * guesswork, and the answer changed without a code change if Meta moves again.
 *
 *  - `native_flow`        interactiveMessage/nativeFlowMessage inside a
 *                         viewOnceMessage wrapper. The shape most current
 *                         guidance describes.
 *  - `native_flow_v1`     the same, plus the `messageParamsJson` and
 *                         `messageVersion` fields the proto defines and the
 *                         wrapper-only form omits.
 *  - `interactive_direct` interactiveMessage with no viewOnceMessage wrapper.
 *  - `buttons_legacy`     the original `buttonsMessage`.
 *  - `template`           templateMessage with hydrated quick-reply buttons.
 *  - `native_flow_helper`  the same interactiveMessage, but built and relayed by
 *                         the `baileys_helper` engine, which also derives the
 *                         `biz`/`interactive`/`native_flow`/`bot` stanza nodes the
 *                         official client emits alongside it. The five above are
 *                         hand-built payloads and all five have been measured
 *                         failing; this one differs by declaring the stanza
 *                         rather than by reshaping the payload. See nativeFlow.ts.
 */
export type InteractiveFormat =
    | "native_flow"
    | "native_flow_v1"
    | "interactive_direct"
    | "buttons_legacy"
    | "template"
    | "native_flow_helper";

export const INTERACTIVE_FORMATS: InteractiveFormat[] = [
    "native_flow",
    "native_flow_v1",
    "interactive_direct",
    "buttons_legacy",
    "template",
    "native_flow_helper",
];

/**
 * Body text plus the buttons under it.
 *
 * This is the whole vocabulary Django uses to ask for a button message: a
 * body, an optional footer, ordered buttons carrying their own ids, and
 * optionally the wire format to use. No Baileys or protobuf type crosses this
 * boundary in either direction -- translating this into whatever WhatsApp
 * wants this month is baileysProvider.ts's job alone.
 */
export interface OutgoingInteractive {
    body: string;
    footer?: string;
    buttons: InteractiveButton[];
    /** Omitted means "use the bridge's configured default". */
    format?: InteractiveFormat;
}

export interface OutgoingMessage {
    to: string;
    type: string;
    text?: { body: string };
    media?: { link: string; caption?: string; filename?: string };
    /** Set when type === "interactive": body text plus the buttons under it. */
    interactive?: OutgoingInteractive;
    /** Set when type === "poll". */
    poll?: OutgoingPoll;
    /**
     * Django's per-attempt idempotency key (see apps/messaging/leasing.py). If
     * a send with this id has already succeeded or is already in doubt, the
     * bridge returns the earlier result and does NOT send again -- this is what
     * stops a recovery retry after a lost HTTP response becoming a duplicate.
     */
    send_operation_id?: string;
}

/** What became of one send, by its send_operation_id. See OperationStore. */
export interface OperationStatus {
    known: boolean;
    sent: boolean;
    provider_message_id: string | null;
    ambiguous: boolean;
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
 * One phone number's WhatsApp registration, as the number-filter tool
 * reports it.
 *
 * `number` is echoed back exactly as the caller sent it, so Django can match
 * results to its own rows without re-deriving whatever normalisation the
 * bridge applied on the way out.
 */
export interface NumberLookupResult {
    number: string;
    exists: boolean;
    /** The registered JID when `exists`, else null. */
    jid: string | null;
}

/** Most numbers one `checkNumbers` call may carry. Django batches above this. */
export const NUMBER_LOOKUP_MAX_PER_REQUEST = 500;

/** One group the linked number is a member of, without its member list. */
export interface GroupSummary {
    id: string;
    subject: string;
    member_count: number;
    /** Only admins may post ("announcement" groups). */
    announce: boolean;
    is_community: boolean;
    created_at: string | null;
}

export interface GroupMember {
    /** Digits only, no "+" -- the same shape sends use. */
    phone: string;
    jid: string;
    name: string;
    is_admin: boolean;
}

export interface GroupMembers {
    id: string;
    subject: string;
    members: GroupMember[];
    /**
     * Participants WhatsApp exposed only by an anonymous LID, with no phone
     * number to import. Counted rather than dropped silently, so the screen
     * can say "412 members, 9 hidden" instead of quietly importing 403.
     */
    hidden_count: number;
}

/** Most groups one `getGroupMembers` call may name. */
export const GROUP_MEMBERS_MAX_GROUPS = 25;

/**
 * Thrown by the lookup methods when the session has no live socket. Its own
 * class so routes.ts can answer 409 without knowing anything about Baileys.
 */
export class SessionNotConnected extends Error {
    constructor(message = "This session is not connected.") {
        super(message);
        this.name = "SessionNotConnected";
    }
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
    /**
     * What became of a send, by its send_operation_id -- for Django's recovery
     * sweep after a lost response. `known: false` means the bridge never saw
     * that operation (safe for Django to re-send).
     */
    getOperationStatus(operationId: string): OperationStatus;

    /**
     * Which of these numbers are registered on WhatsApp (the number filter).
     * Throws SessionNotConnected when there is no live socket to ask.
     */
    checkNumbers(sessionId: string, numbers: string[]): Promise<NumberLookupResult[]>;

    /** Every group the linked number belongs to (the group grabber's first step). */
    listGroups(sessionId: string): Promise<GroupSummary[]>;

    /** The members of the named groups, with phone numbers where WhatsApp exposes them. */
    getGroupMembers(sessionId: string, groupIds: string[]): Promise<GroupMembers[]>;
}
