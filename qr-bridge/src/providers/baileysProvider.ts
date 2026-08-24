import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { Boom } from "@hapi/boom";
import makeWASocket, {
    fetchLatestBaileysVersion,
    generateWAMessageFromContent,
    proto,
    DisconnectReason,
    type WASocket,
} from "@whiskeysockets/baileys";
import pino from "pino";

import { config } from "../config.js";
import { renderQrDataUrl } from "../qr.js";
import { postConnectionWebhook, postMessageWebhook } from "../webhooks.js";
import { SessionStore, type SessionEntry } from "../sessionStore.js";
import { isDeadHandshake, sessionAddressesFor, useSharedAuthState, forgetAuthState } from "../authState.js";
import type {
    CreateSessionInput,
    InteractiveButton,
    OutgoingMessage,
    SendResult,
    SessionSnapshot,
    WhatsAppBridgeProvider,
} from "./base.js";

const logger = pino({ level: config.logLevel, name: "qr-bridge" });

interface SessionMeta {
    accountId: number;
    connectionWebhookUrl: string;
    messageWebhookUrl: string;
}

const MEDIA_TYPES = ["image", "video", "audio", "document", "sticker"] as const;

/** JID suffix that identifies a real phone-number sender (vs. a LID, group, or broadcast). */
const PHONE_JID_SUFFIX = "@s.whatsapp.net";

/**
 * Reconnect backoff. Reconnecting the instant a socket drops turns a
 * server-side problem into a tight loop of half-open sockets, which is exactly
 * the condition that lets two of them overlap on one auth folder.
 */
const BASE_RECONNECT_MS = 2_000;
const MAX_RECONNECT_MS = 60_000;
const MAX_RECONNECT_ATTEMPTS = 12;

/**
 * Resolve the sender's phone number from a message key.
 *
 * WhatsApp now addresses many chats by LID (`<opaque-id>@lid`), so remoteJid on
 * its own is often not a phone number at all -- forwarding it produced contacts
 * with unusable 15-digit "numbers" that nothing could reply to. When the chat is
 * LID-addressed, Baileys puts the real phone JID in senderPn/participantPn.
 * Group (`@g.us`) and broadcast JIDs have no single phone sender, so they yield
 * "" here and the caller drops them.
 */
function senderPhone(key: any): string {
    for (const jid of [key?.senderPn, key?.participantPn, key?.remoteJid]) {
        if (typeof jid === "string" && jid.endsWith(PHONE_JID_SUFFIX)) {
            // A JID may carry a device suffix ("4477...:12@s.whatsapp.net").
            return jid.split("@")[0].split(":")[0];
        }
    }
    return "";
}

function metaPathFor(store: SessionStore, sessionId: string): string {
    return path.join(store.authDirFor(sessionId), "bridge-meta.json");
}

/**
 * The only module in this service that imports Baileys. Everything else
 * (routes, webhooks, index) talks to the WhatsAppBridgeProvider interface
 * in base.ts -- same boundary Django keeps against this whole service, kept
 * one layer further in so a future non-Baileys engine only means writing a
 * new file here.
 */
export class BaileysProvider implements WhatsAppBridgeProvider {
    constructor(private readonly store: SessionStore) {}

    /** Reconnects every session whose auth state survived a service restart. */
    async resumePersistedSessions(): Promise<void> {
        for (const sessionId of this.store.persistedSessionIds()) {
            const metaPath = metaPathFor(this.store, sessionId);
            if (!fs.existsSync(metaPath)) continue;
            const meta: SessionMeta = JSON.parse(fs.readFileSync(metaPath, "utf-8"));
            logger.info({ sessionId }, "Resuming persisted WhatsApp session");
            const snapshot: SessionSnapshot = {
                session_id: sessionId,
                status: "CONNECTING",
                qr: null,
                expires_at: null,
                phone_number: null,
                display_name: null,
                provider_account_id: null,
                error: null,
            };
            this.store.set(sessionId, {
                snapshot,
                accountId: meta.accountId,
                connectionWebhookUrl: meta.connectionWebhookUrl,
                messageWebhookUrl: meta.messageWebhookUrl,
                // Fresh TTL window from the moment we resume it, not from
                // whenever it was originally created.
                deadlineAt: Date.now() + config.qrTtlSeconds * 1000,
            });
            await this.openSocket(sessionId);
        }
    }

    async createSession(input: CreateSessionInput): Promise<SessionSnapshot> {
        const sessionId = crypto.randomUUID();
        const authDir = this.store.authDirFor(sessionId);
        fs.mkdirSync(authDir, { recursive: true });
        fs.writeFileSync(
            metaPathFor(this.store, sessionId),
            JSON.stringify({
                accountId: input.accountId,
                connectionWebhookUrl: input.connectionWebhookUrl,
                messageWebhookUrl: input.messageWebhookUrl,
            } satisfies SessionMeta),
        );

        const snapshot: SessionSnapshot = {
            session_id: sessionId,
            status: "CONNECTING",
            qr: null,
            expires_at: null,
            phone_number: null,
            display_name: null,
            provider_account_id: null,
            error: null,
        };
        this.store.set(sessionId, {
            snapshot,
            accountId: input.accountId,
            connectionWebhookUrl: input.connectionWebhookUrl,
            messageWebhookUrl: input.messageWebhookUrl,
            deadlineAt: Date.now() + config.qrTtlSeconds * 1000,
        });

        await this.openSocket(sessionId);

        // The caller (routes.ts) needs the QR, which arrives asynchronously
        // over the socket -- wait briefly for it (or for an immediate
        // failure) rather than returning "CONNECTING" with nothing to show.
        return this.awaitFirstQrOrOutcome(sessionId);
    }

    private awaitFirstQrOrOutcome(sessionId: string, timeoutMs = 20_000): Promise<SessionSnapshot> {
        return new Promise((resolve) => {
            const deadline = Date.now() + timeoutMs;
            const poll = () => {
                const entry = this.store.get(sessionId);
                if (!entry) return resolve(this.errorSnapshot(sessionId, "Session was removed."));
                if (entry.snapshot.status !== "CONNECTING" || Date.now() > deadline) {
                    return resolve(entry.snapshot);
                }
                setTimeout(poll, 250);
            };
            poll();
        });
    }

    private errorSnapshot(sessionId: string, error: string): SessionSnapshot {
        return {
            session_id: sessionId,
            status: "ERROR",
            qr: null,
            expires_at: null,
            phone_number: null,
            display_name: null,
            provider_account_id: null,
            error,
        };
    }

    /**
     * Open this session's socket, guaranteeing it has exactly one.
     *
     * The guard matters more than it looks. Every extra socket brings its own
     * view of the session's auth folder, and two of them encrypting to the
     * same contact advance that contact's Signal ratchet independently. After
     * that the contact cannot decrypt anything we send: their WhatsApp shows
     * "Waiting for this message. This may take a while." and never resolves,
     * because we go on re-announcing a one-time pre-key they already used.
     *
     * Reconnects used to be able to overlap -- a close handled while another
     * open was still in flight, or a socket we had already replaced reporting
     * its own close. That is how this bridge accumulated sockets (16 opens
     * against 11 closes over two days on one live session) and left 87 of that
     * session's 183 Signal sessions permanently unreadable.
     */
    private openSocket(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        // Deliberately expired/disconnected in the gap between the reconnect
        // being scheduled and this running (see the `closing` flag's
        // doc-comment in sessionStore.ts) -- don't resurrect it.
        if (!entry || entry.closing) return Promise.resolve();
        if (entry.opening) return entry.opening;

        // A plain `let` reads as "used before assigned" to TS here, since it
        // cannot see that the closure only runs the comparison after the
        // assignment below completes -- a boxed property sidesteps that.
        const box: { run?: Promise<void> } = {};
        box.run = (async () => {
            try {
                await this.openSocketOnce(sessionId);
            } catch (err) {
                logger.error({ sessionId, err }, "Failed to open WhatsApp socket");
                this.scheduleReconnect(sessionId);
            } finally {
                const current = this.store.get(sessionId);
                if (current && current.opening === box.run) current.opening = undefined;
            }
        })();

        entry.opening = box.run;
        return box.run;
    }

    private async openSocketOnce(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry || entry.closing) return;

        // Retire whatever is already here before building its replacement.
        this.teardownSocket(entry);

        const generation = (entry.generation ?? 0) + 1;
        entry.generation = generation;

        const authDir = this.store.authDirFor(sessionId);
        const { state, saveCreds } = await useSharedAuthState(authDir, logger);
        const { version } = await fetchLatestBaileysVersion();

        // Both awaits above yield, so re-check: the session may have been torn
        // down, or a newer open may have overtaken this one.
        const current = this.store.get(sessionId);
        if (!current || current.closing || current.generation !== generation) return;

        const sock: WASocket = makeWASocket({
            version,
            auth: state,
            logger: logger.child({ sessionId }) as any,
            syncFullHistory: false,
        });
        current.sock = sock;

        // A socket we have replaced still emits its own close, and acting on
        // that is what used to start yet another one. Every handler below
        // stands down once this generation is no longer the session's current.
        const isCurrent = () => this.store.get(sessionId)?.generation === generation;

        sock.ev.on("creds.update", () => {
            if (isCurrent()) void saveCreds();
        });

        sock.ev.on("connection.update", async (update) => {
            if (!isCurrent()) return;
            const { connection, lastDisconnect, qr } = update;
            const live = this.store.get(sessionId);
            if (!live) return;

            if (qr) {
                await this.handleNewQr(sessionId, qr);
            }

            if (connection === "open") {
                await this.handleConnected(sessionId, sock);
            }

            if (connection === "close") {
                const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
                if (live.snapshot.status === "EXPIRED" || live.snapshot.status === "DISCONNECTED" || live.closing) {
                    // We tore this socket down ourselves (TTL expiry or an
                    // explicit disconnect) -- never reconnect it, regardless
                    // of what Baileys reports as the close reason.
                    return;
                }

                if (statusCode === DisconnectReason.loggedOut) {
                    await this.handleLoggedOut(sessionId);
                    return;
                }

                if (statusCode === DisconnectReason.connectionReplaced) {
                    // Another socket has taken this account's device slot.
                    // Racing it back just swaps the two forever, and every
                    // swap is one more chance to corrupt a Signal session, so
                    // stand down and let the winner keep the connection.
                    await this.handleReplaced(sessionId);
                    return;
                }

                logger.warn({ sessionId, statusCode }, "WhatsApp socket closed, reconnecting");
                this.scheduleReconnect(sessionId);
            }
        });

        sock.ev.on("messages.upsert", ({ messages, type }) => {
            if (type !== "notify" || !isCurrent()) return;
            void this.forwardInboundMessages(sessionId, messages);
        });

        sock.ev.on("messages.update", (updates) => {
            if (!isCurrent()) return;
            void this.forwardStatusUpdates(sessionId, updates);
        });
    }

    /**
     * Retire a socket for good.
     *
     * Dropping the listeners first is the important half: a socket on its way
     * out still emits `connection.update`, and while its handler was attached
     * the close of a socket we had already replaced would start another one.
     */
    private teardownSocket(entry: SessionEntry): void {
        const sock = entry.sock as WASocket | undefined;
        if (!sock) return;
        entry.sock = undefined;

        for (const event of ["connection.update", "creds.update", "messages.upsert", "messages.update"] as const) {
            try {
                sock.ev.removeAllListeners(event);
            } catch {
                // Nothing here is worth failing a teardown over.
            }
        }
        try {
            sock.end(undefined);
        } catch {
            // Already dead.
        }
    }

    /** Queue a reconnect, backing off so a bad spell cannot become a loop. */
    private scheduleReconnect(sessionId: string): void {
        const entry = this.store.get(sessionId);
        if (!entry || entry.closing) return;

        this.teardownSocket(entry);

        const attempt = (entry.reconnectAttempts ?? 0) + 1;
        entry.reconnectAttempts = attempt;

        if (attempt > MAX_RECONNECT_ATTEMPTS) {
            logger.error({ sessionId, attempt }, "Giving up reconnecting this WhatsApp session");
            entry.snapshot = {
                ...entry.snapshot,
                status: "ERROR",
                qr: null,
                error: "Lost the connection to WhatsApp and could not get it back. Reconnect this number.",
            };
            void postConnectionWebhook(entry.connectionWebhookUrl, { session_id: sessionId, status: "ERROR" }, logger);
            return;
        }

        const backoff = Math.min(BASE_RECONNECT_MS * 2 ** (attempt - 1), MAX_RECONNECT_MS);
        // Half fixed, half jittered, so two sessions dropped by one network
        // blip do not come back in lockstep and collide.
        const delay = backoff / 2 + Math.random() * (backoff / 2);

        if (entry.reconnectTimer) clearTimeout(entry.reconnectTimer);
        entry.reconnectTimer = setTimeout(() => void this.openSocket(sessionId), delay);
    }

    /** 440: something else owns this account's slot now, so stay down. */
    private async handleReplaced(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        logger.error({ sessionId }, "Connection replaced by another socket; standing down");
        this.teardownSocket(entry);
        // Silences the retired socket's handlers without marking the session
        // closing, so an explicit reconnect can still revive it later.
        entry.generation = (entry.generation ?? 0) + 1;
        entry.snapshot = {
            ...entry.snapshot,
            status: "ERROR",
            qr: null,
            error: "This number was connected somewhere else, so this connection was dropped.",
        };
        await postConnectionWebhook(entry.connectionWebhookUrl, { session_id: sessionId, status: "ERROR" }, logger);
    }

    private async handleNewQr(sessionId: string, rawQr: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        // deadlineAt is fixed once at session creation/resume (see
        // sessionStore.ts) -- it must NOT be pushed back every time Baileys
        // internally regenerates the QR (roughly every ~60s until it gives
        // up with "QR refs attempts ended" / statusCode 408 and the
        // connection.update close handler reconnects). Resetting it here
        // used to mean a session nobody ever scanned kept re-arming its own
        // TTL forever instead of expiring.
        if (!entry.deadlineAt) {
            entry.deadlineAt = Date.now() + config.qrTtlSeconds * 1000;
        }
        const remainingMs = entry.deadlineAt - Date.now();
        if (remainingMs <= 0) {
            // Overall session TTL is already up -- don't hand out another
            // QR; let the pending expiry timer (or the next close event,
            // once it fires) tear this session down instead.
            return;
        }

        entry.snapshot = {
            ...entry.snapshot,
            status: "QR_READY",
            qr: await renderQrDataUrl(rawQr),
            expires_at: new Date(entry.deadlineAt).toISOString(),
            error: null,
        };

        if (!entry.expiryTimer) {
            entry.expiryTimer = setTimeout(() => void this.expireSession(sessionId), remainingMs);
        }
    }

    private async expireSession(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry || entry.snapshot.status !== "QR_READY") return;

        entry.closing = true;
        entry.snapshot = { ...entry.snapshot, status: "EXPIRED", qr: null };
        this.teardownSocket(entry);
        this.store.removePersisted(sessionId);
        forgetAuthState(this.store.authDirFor(sessionId));
        await postConnectionWebhook(entry.connectionWebhookUrl, { session_id: sessionId, status: "EXPIRED" }, logger);
    }

    private async handleConnected(sessionId: string, sock: WASocket): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        const jid = sock.user?.id ?? "";
        const phoneNumber = jid.split(":")[0]?.split("@")[0] ?? null;

        entry.snapshot = {
            ...entry.snapshot,
            status: "CONNECTED",
            qr: null,
            expires_at: null,
            phone_number: phoneNumber ? `+${phoneNumber}` : null,
            display_name: sock.user?.name ?? null,
            provider_account_id: jid || null,
            error: null,
        };
        if (entry.expiryTimer) clearTimeout(entry.expiryTimer);
        if (entry.reconnectTimer) clearTimeout(entry.reconnectTimer);
        entry.reconnectTimer = undefined;
        entry.reconnectAttempts = 0;

        logger.info({ sessionId, phoneNumber }, "WhatsApp session connected");
        await postConnectionWebhook(
            entry.connectionWebhookUrl,
            {
                session_id: sessionId,
                status: "CONNECTED",
                phone_number: entry.snapshot.phone_number,
                display_name: entry.snapshot.display_name,
                provider_account_id: entry.snapshot.provider_account_id,
            },
            logger,
        );
    }

    private async handleLoggedOut(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        entry.closing = true;
        this.teardownSocket(entry);
        entry.snapshot = { ...entry.snapshot, status: "DISCONNECTED", qr: null, error: "Logged out from WhatsApp." };
        await postConnectionWebhook(entry.connectionWebhookUrl, { session_id: sessionId, status: "DISCONNECTED" }, logger);
        this.store.removePersisted(sessionId);
        forgetAuthState(this.store.authDirFor(sessionId));
    }

    private async forwardInboundMessages(sessionId: string, messages: any[]): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        const mapped = messages
            .filter((m) => !m.key?.fromMe && m.message)
            .map((m) => {
                const text = m.message?.conversation ?? m.message?.extendedTextMessage?.text ?? null;
                // Must test the field's VALUE, not `in`: Baileys message objects
                // are protobuf instances whose prototype carries every optional
                // field, so `"imageMessage" in m.message` is true for a plain
                // text message and typed every inbound message as an image.
                const mediaType = MEDIA_TYPES.find((t) => (m.message as any)?.[`${t}Message`]);
                const caption = mediaType ? (m.message as any)?.[`${mediaType}Message`]?.caption ?? null : null;
                // A tapped button carries no conversation text, so it is
                // reported as its own type with the id that was tapped --
                // that id is what automation rules match on.
                const buttonReply = extractButtonReply(m.message);
                return {
                    id: m.key?.id,
                    from: senderPhone(m.key),
                    type: buttonReply ? "interactive" : mediaType ?? (text ? "text" : "unknown"),
                    body: buttonReply ? buttonReply.title : text ?? caption,
                    button_id: buttonReply?.id ?? null,
                    push_name: m.pushName ?? null,
                };
            })
            .filter((m) => m.id && m.from);

        if (mapped.length === 0) return;
        await postMessageWebhook(entry.messageWebhookUrl, { messages: mapped }, logger);
    }

    private async forwardStatusUpdates(sessionId: string, updates: any[]): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;

        const STATUS_MAP: Record<number, string> = { 2: "sent", 3: "delivered", 4: "read" };
        const mapped = updates
            .map((u) => ({ id: u.key?.id, status: STATUS_MAP[u.update?.status as number] }))
            .filter((s) => s.id && s.status);

        if (mapped.length === 0) return;
        await postMessageWebhook(entry.messageWebhookUrl, { statuses: mapped }, logger);
    }

    getSession(sessionId: string): SessionSnapshot | undefined {
        return this.store.get(sessionId)?.snapshot;
    }

    getAccountInfo(sessionId: string): SessionSnapshot | undefined {
        return this.getSession(sessionId);
    }

    async disconnectSession(sessionId: string): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) return;
        entry.closing = true;
        try {
            await (entry.sock as WASocket | undefined)?.logout();
        } catch (err) {
            logger.warn({ sessionId, err }, "Error logging out; removing session state anyway");
        }
        this.teardownSocket(entry);
        this.store.delete(sessionId);
        this.store.removePersisted(sessionId);
        forgetAuthState(this.store.authDirFor(sessionId));
    }

    async sendMessage(sessionId: string, message: OutgoingMessage): Promise<SendResult> {
        const entry = this.store.get(sessionId);
        const sock = entry?.sock as WASocket | undefined;
        if (!entry || !sock || entry.snapshot.status !== "CONNECTED") {
            return { success: false, provider_message_id: null, error: "This session is not connected." };
        }

        const jid = toJid(message.to);
        if (!jid) {
            return { success: false, provider_message_id: null, error: `Invalid recipient '${message.to}'.` };
        }

        // Clear out any Signal session this contact has never been able to
        // read before we encrypt into it again -- see isDeadHandshake().
        await this.repairDeadHandshakes(sessionId, jid);

        try {
            let sent;
            if (message.type === "interactive" && message.interactive) {
                sent = sendInteractive(sock, jid, message.interactive);
            } else if (message.type === "text" && message.text) {
                sent = sock.sendMessage(jid, { text: message.text.body });
            } else if (message.media) {
                sent = sock.sendMessage(jid, buildMediaContent(message.type, message.media));
            } else {
                return { success: false, provider_message_id: null, error: `Unsupported message type '${message.type}'.` };
            }
            const result = await withTimeout(sent, SEND_TIMEOUT_MS);
            return { success: true, provider_message_id: result?.key?.id ?? null, error: null };
        } catch (err) {
            if (err instanceof SendTimeout) {
                // Baileys never settled. It may still deliver, so this is
                // reported as ambiguous rather than a plain failure -- but we
                // answer now instead of letting the caller hang.
                logger.error({ sessionId, jid }, "Send timed out inside Baileys; delivery unknown");
                return {
                    success: false,
                    provider_message_id: null,
                    error: `WhatsApp did not confirm the send within ${SEND_TIMEOUT_MS / 1000}s; delivery is unknown.`,
                    ambiguous: true,
                };
            }
            return { success: false, provider_message_id: null, error: (err as Error).message };
        }
    }

    /**
     * Reset Signal sessions with this recipient that they have never
     * acknowledged, so the send opens a clean one instead of re-announcing a
     * pre-key they already consumed. isDeadHandshake() explains why such a
     * session never recovers on its own.
     *
     * The reset goes through the key store rather than the files, because the
     * cacheable store in authState.ts would otherwise keep serving the record
     * it read before the delete.
     */
    private async repairDeadHandshakes(sessionId: string, jid: string): Promise<void> {
        const user = jid.split("@")[0]?.split(":")[0];
        if (!user) return;

        try {
            const authDir = this.store.authDirFor(sessionId);
            const addresses = sessionAddressesFor(authDir, `${user}.`);
            if (addresses.length === 0) return;

            const { state } = await useSharedAuthState(authDir, logger);
            const stored = await state.keys.get("session", addresses);

            const dead = addresses.filter((address) => isDeadHandshake(stored[address]));
            if (dead.length === 0) return;

            await state.keys.set({ session: Object.fromEntries(dead.map((address) => [address, null])) as any });
            logger.warn(
                { sessionId, jid, addresses: dead },
                "Reset Signal sessions this contact never acknowledged; the next send re-handshakes",
            );
        } catch (err) {
            // Session hygiene must never be the reason a message fails to go.
            logger.warn({ sessionId, jid, err }, "Could not check this contact's Signal sessions");
        }
    }
}

/**
 * Send body text with tappable buttons under it.
 *
 * Buttons are sent as a *native flow* interactive message, which is the format
 * current WhatsApp clients actually render. The older `buttonsMessage` shape
 * Baileys still accepts is no longer rendered by most clients -- it arrives as
 * plain text with the buttons missing, which is precisely the bug this fixes.
 *
 * The `viewOnceMessage` wrapper is not about disappearing messages: an
 * interactive message has to be relayed inside one of the wrapper types the
 * client unpacks before it will render the buttons, so it is built by hand with
 * generateWAMessageFromContent + relayMessage rather than sock.sendMessage.
 *
 * Returns the generated message, whose `key.id` is the id delivery receipts for
 * this send arrive against -- the same shape sendMessage() returns.
 */
async function sendInteractive(
    sock: WASocket,
    jid: string,
    interactive: { body: string; buttons: InteractiveButton[] },
) {
    const message = generateWAMessageFromContent(
        jid,
        {
            viewOnceMessage: {
                message: {
                    messageContextInfo: {
                        deviceListMetadata: {},
                        deviceListMetadataVersion: 2,
                    },
                    interactiveMessage: proto.Message.InteractiveMessage.create({
                        body: proto.Message.InteractiveMessage.Body.create({ text: interactive.body }),
                        nativeFlowMessage: proto.Message.InteractiveMessage.NativeFlowMessage.create({
                            buttons: interactive.buttons.map(toNativeFlowButton),
                        }),
                    }),
                },
            },
        },
        { userJid: sock.user?.id ?? "" },
    );

    await sock.relayMessage(jid, message.message!, { messageId: message.key.id! });
    return message;
}

/**
 * One button in native-flow form.
 *
 * The parameters travel as a JSON *string*, not a nested object -- that is the
 * wire format, not a convenience. A reply button's `id` is what comes back when
 * it is tapped, so it must be the id Django stored (automation matches on it).
 */
function toNativeFlowButton(button: InteractiveButton) {
    if (button.type === "URL") {
        return {
            name: "cta_url",
            buttonParamsJson: JSON.stringify({
                display_text: button.title,
                url: button.url,
                merchant_url: button.url,
            }),
        };
    }
    return {
        name: "quick_reply",
        buttonParamsJson: JSON.stringify({ display_text: button.title, id: button.id }),
    };
}

/**
 * The button a contact tapped, whichever shape WhatsApp reported it in.
 *
 * Four shapes mean the same thing -- a native-flow reply (what our own
 * interactive messages come back as), the older buttons/template replies, and a
 * list selection. Flattened here so Django only ever sees "this id was tapped".
 *
 * Each check reads a nested VALUE rather than testing `in`: Baileys message
 * objects are protobuf instances whose prototype carries every optional field,
 * so `"buttonsResponseMessage" in message` is true for every message.
 */
function extractButtonReply(message: any): { id: string; title: string } | null {
    const buttons = message?.buttonsResponseMessage;
    if (buttons?.selectedButtonId) {
        return { id: buttons.selectedButtonId, title: buttons.selectedDisplayText ?? buttons.selectedButtonId };
    }

    const template = message?.templateButtonReplyMessage;
    if (template?.selectedId) {
        return { id: template.selectedId, title: template.selectedDisplayText ?? template.selectedId };
    }

    const listRowId = message?.listResponseMessage?.singleSelectReply?.selectedRowId;
    if (listRowId) {
        return { id: listRowId, title: message.listResponseMessage.title ?? listRowId };
    }

    const nativeFlow = message?.interactiveResponseMessage?.nativeFlowResponseMessage;
    if (nativeFlow?.paramsJson) {
        try {
            const params = JSON.parse(nativeFlow.paramsJson);
            if (params?.id) {
                const label = message.interactiveResponseMessage?.body?.text;
                return { id: String(params.id), title: label ?? String(params.id) };
            }
        } catch {
            // A params blob we can't read is not a reason to drop the message;
            // it falls through and is forwarded as ordinary text.
        }
    }

    return null;
}

/** Baileys hangs forever on a malformed JID, so no send may run unbounded. */
const SEND_TIMEOUT_MS = 30_000;

class SendTimeout extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    let timer: NodeJS.Timeout;
    return Promise.race([
        promise.finally(() => clearTimeout(timer)),
        new Promise<never>((_, reject) => {
            timer = setTimeout(() => reject(new SendTimeout()), ms);
        }),
    ]);
}

/**
 * Build the WhatsApp JID for a recipient, or null if there is nothing usable.
 *
 * Everything but the digits has to go. WhatsApp's device-list lookup for a
 * JID like "+918429577706@s.whatsapp.net" never resolves and never errors, so
 * sendMessage() simply never settles -- which is exactly how a stray "+" from
 * an E.164-formatted number used to turn every campaign send into a hang.
 * Django strips it too (see _to_msisdn in qr_bridge.py); this is the second
 * line of defence, because a hang here is far worse than a rejection.
 */
function toJid(to: string): string | null {
    if (to?.includes("@")) return to;
    const digits = (to ?? "").replace(/\D/g, "");
    return digits ? `${digits}@s.whatsapp.net` : null;
}

function buildMediaContent(type: string, media: { link: string; caption?: string; filename?: string }): any {
    const url = { url: media.link };
    switch (type) {
        case "image":
            return { image: url, caption: media.caption };
        case "video":
            return { video: url, caption: media.caption };
        case "audio":
            return { audio: url, mimetype: "audio/mp4" };
        case "document":
            return { document: url, fileName: media.filename, caption: media.caption };
        default:
            return { document: url, fileName: media.filename };
    }
}
