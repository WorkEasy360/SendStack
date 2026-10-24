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
import { OperationStore } from "../operationStore.js";
import { isDeadHandshake, sessionAddressesFor, useSharedAuthState, forgetAuthState } from "../authState.js";
import { runButtonLab, type ButtonLabInput, type ButtonLabReport } from "../buttonLab.js";
import { captureHelperConsole, nativeFlowEngineAvailable, sendNativeFlowButtons } from "../nativeFlow.js";
import { extractButtonReply } from "../buttonReply.js";
import {
    GROUP_MEMBERS_MAX_GROUPS,
    SessionNotConnected,
    type CreateSessionInput,
    type GroupMember,
    type GroupMembers,
    type GroupSummary,
    type InteractiveButton,
    type NumberLookupResult,
    type OperationStatus,
    type OutgoingInteractive,
    type OutgoingMessage,
    type SendResult,
    type SessionSnapshot,
    type WhatsAppBridgeProvider,
} from "./base.js";

const logger = pino({ level: config.logLevel, name: "qr-bridge" });

// The native-flow engine reports through `console`; route that into pino before
// anything can send. See captureHelperConsole().
captureHelperConsole(logger);

// Answered at startup rather than mid-send: see nativeFlowEngineAvailable().
// Logged, never thrown -- text and media never touch the engine, and the
// button format it serves is off unless an operator turns it on.
const nativeFlowEngine = nativeFlowEngineAvailable();
if (!nativeFlowEngine.ok) {
    logger.error(
        { reason: nativeFlowEngine.reason },
        "Native-flow button engine unusable; QR_BRIDGE_INTERACTIVE_FORMAT=native_flow_helper will fail. Text and media are unaffected.",
    );
} else if (config.interactiveFormat === "native_flow_helper") {
    logger.warn("Native-flow buttons are ENABLED as the default interactive format.");
}

interface SessionMeta {
    accountId: number;
    connectionWebhookUrl: string;
    messageWebhookUrl: string;
}

const MEDIA_TYPES = ["image", "video", "audio", "document", "sticker"] as const;

/** JID suffix that identifies a real phone-number sender (vs. a LID, group, or broadcast). */
const PHONE_JID_SUFFIX = "@s.whatsapp.net";

/**
 * Chat JIDs that are not one person's private thread, whatever else the key
 * carries. A group message names a *member* in `participantPn`, and reading
 * that as the correspondent turned every group into a private conversation
 * with whoever last spoke -- including replies, which would then have gone to
 * that member instead of back to the group.
 */
const NON_DIRECT_JID_SUFFIXES = ["@g.us", "@broadcast", "@newsletter"] as const;

/**
 * Why an inbound message was received but not delivered to the inbox.
 *
 * Every one of these used to be a silent `.filter()`, and that is the most
 * expensive thing this file has ever done. A WhatsApp account whose stored
 * Signal state has gone bad still pairs, still reports CONNECTED and still
 * receives stanzas -- it simply cannot read any of them. With nothing logged,
 * the only symptom anywhere in the product was an inbox that stayed empty,
 * which looks exactly like nobody having written in. Naming and counting each
 * drop is what separates "no messages" from "no *readable* messages".
 */
type DropReason =
    | "own_message"
    | "undecryptable"
    | "no_message_id"
    | "not_a_direct_chat"
    | "no_sender_number";

/**
 * Reconnect backoff. Reconnecting the instant a socket drops turns a
 * server-side problem into a tight loop of half-open sockets, which is exactly
 * the condition that lets two of them overlap on one auth folder.
 */
const BASE_RECONNECT_MS = 2_000;
const MAX_RECONNECT_MS = 60_000;
const MAX_RECONNECT_ATTEMPTS = 12;

/**
 * Number-filter pacing.
 *
 * A registration lookup is one "usync" query carrying several users. The
 * server answers a batch of this size in one round trip; the pause between
 * batches is there because a linked device that fires thousands of contact
 * queries back to back looks like exactly the scraping WhatsApp bans numbers
 * for. Django batches above the per-request cap and paces its own calls too
 * (LEADTOOLS_NUMBER_CHECK_* in backend settings), so this is the inner loop
 * of a two-level throttle rather than the whole of it.
 */
const NUMBER_LOOKUP_BATCH = 50;
const NUMBER_LOOKUP_PAUSE_MS = 300;

/** How long one registration or group query may take before it is given up on. */
const LOOKUP_TIMEOUT_MS = 30_000;

/** Whether this chat is one person's private thread rather than a group or broadcast. */
function isDirectChat(remoteJid: unknown): boolean {
    return typeof remoteJid === "string" && !NON_DIRECT_JID_SUFFIXES.some((suffix) => remoteJid.endsWith(suffix));
}

/** The phone number in a JID, or "" if that JID does not carry one. */
function phoneFromJid(jid: unknown): string {
    if (typeof jid !== "string" || !jid.endsWith(PHONE_JID_SUFFIX)) return "";
    // A JID may carry a device suffix ("4477...:12@s.whatsapp.net").
    return jid.split("@")[0].split(":")[0];
}

/**
 * Resolve the sender's phone number from a message key.
 *
 * WhatsApp now addresses many chats by LID (`<opaque-id>@lid`), so remoteJid on
 * its own is often not a phone number at all -- forwarding it produced contacts
 * with unusable 15-digit "numbers" that nothing could reply to. When the chat is
 * LID-addressed, Baileys puts the real phone JID in `senderPn`.
 *
 * The group check comes first and is not a formality. `participantPn` used to
 * be consulted here alongside `senderPn`, and it is populated on every group
 * message -- so a group thread arrived looking exactly like a private chat with
 * whichever member happened to speak, and an agent answering it would have
 * replied to that member rather than to the group.
 */
function senderPhone(key: any): string {
    if (!isDirectChat(key?.remoteJid)) return "";
    return phoneFromJid(key?.senderPn) || phoneFromJid(key?.remoteJid);
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
    /** Idempotency for outbound sends, keyed by Django's send_operation_id. */
    private readonly operations = new OperationStore(config.dataDir);

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
            // Baileys' own logger, deliberately NOT this service's. At debug
            // and trace it prints the Signal payloads it is handling, which
            // include pre-keys and session state -- the material that lets
            // somebody impersonate a connected number. Wiring it to
            // QR_BRIDGE_LOG_LEVEL meant that turning this service's logging up
            // to debug an unrelated problem would write those keys to the
            // container's stdout, where they end up in whatever collects logs.
            // It gets its own level (QR_BRIDGE_BAILEYS_LOG_LEVEL, "warn" by
            // default) so raising ours never cascades into that.
            logger: logger.child({ sessionId }, { level: config.baileysLogLevel }) as any,
            syncFullHistory: false,
        });
        current.sock = sock;
        // A resumed session arrives already linked; a fresh one becomes linked
        // when the scan lands (the creds.update below).
        if (state.creds.me) current.paired = true;

        // A socket we have replaced still emits its own close, and acting on
        // that is what used to start yet another one. Every handler below
        // stands down once this generation is no longer the session's current.
        const isCurrent = () => this.store.get(sessionId)?.generation === generation;

        sock.ev.on("creds.update", (update) => {
            if (!isCurrent()) return;
            // `me` is written by WhatsApp's pair-success. The session turns
            // from a QR attempt into a connected number at this moment, which
            // is up to a few seconds before the socket reopens and reports
            // CONNECTED -- a window the QR deadline must not be allowed to
            // fire inside.
            if (update?.me) {
                const live = this.store.get(sessionId);
                if (live) live.paired = true;
            }
            void saveCreds();
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
            if (!isCurrent()) return;
            // "notify" is a message arriving while we are connected; "append"
            // is one WhatsApp had queued because we were not. Only the first
            // used to be accepted, which quietly cost this session every
            // message sent during a reconnect -- and a session that drops and
            // comes back all day drops most of its inbox that way. Both are
            // messages nobody here has seen, so both are ingested; Django
            // stores webhook events under (account, provider_event_id), so a
            // redelivery on the next reconnect is a no-op rather than a
            // duplicate. History sync does not come through here at all (it
            // arrives on "messaging-history.set"), so this cannot drag old
            // conversations into the inbox.
            if (type !== "notify" && type !== "append") return;
            void this.forwardInboundMessages(sessionId, messages);
        });

        sock.ev.on("messages.update", (updates) => {
            // A replaced socket keeps emitting; its updates are stale by
            // definition. Logged rather than dropped in silence, because a
            // session wedged on an old generation looks exactly like a
            // WhatsApp that has stopped sending receipts.
            if (!isCurrent()) {
                logger.warn(
                    { sessionId, generation, count: updates.length },
                    "Dropped delivery statuses from a superseded socket",
                );
                return;
            }
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

        if (attempt > MAX_RECONNECT_ATTEMPTS && entry.paired) {
            // A linked number is not a pairing attempt, and a long outage
            // (the host's network down for ten minutes, WhatsApp unreachable)
            // is not a reason to unlink it. Its credentials are still good,
            // so keep knocking at the slow, capped interval until WhatsApp
            // answers; giving up here used to turn every extended blip into a
            // dead device that someone had to re-scan. Logged once, not per
            // attempt, so a day-long outage is one line rather than a
            // thousand.
            if (attempt === MAX_RECONNECT_ATTEMPTS + 1) {
                logger.error(
                    { sessionId, attempt },
                    "WhatsApp is not answering this linked session; will keep retrying every minute until it does",
                );
            }
        } else if (attempt > MAX_RECONNECT_ATTEMPTS) {
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

        // The scan was accepted but the socket has not reopened yet, so the
        // snapshot still says QR_READY. Expiring now would delete the auth
        // folder of a number that is linked on the user's phone: the handset
        // would keep listing the device while this service had nothing to
        // reconnect it with, and nobody could tell it from a connection that
        // "expired". The reopen reports CONNECTED within seconds; let it.
        if (entry.paired) {
            logger.info({ sessionId }, "QR window elapsed after the scan was accepted; keeping the session");
            entry.expiryTimer = undefined;
            return;
        }

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

        const mapped: Array<Record<string, unknown>> = [];
        const dropped: Array<{ reason: DropReason; key: unknown }> = [];

        for (const m of messages) {
            if (m.key?.fromMe) {
                dropped.push({ reason: "own_message", key: m.key });
                continue;
            }
            if (!m.message) {
                // No plaintext. Almost always this stanza failed to decrypt:
                // Baileys marks it CIPHERTEXT, asks the sender to resend, and
                // hands us a message with nothing in it. There is genuinely
                // nothing to deliver -- but see DropReason for why this must
                // never pass unremarked.
                dropped.push({ reason: "undecryptable", key: m.key });
                continue;
            }
            if (!m.key?.id) {
                dropped.push({ reason: "no_message_id", key: m.key });
                continue;
            }
            if (!isDirectChat(m.key?.remoteJid)) {
                dropped.push({ reason: "not_a_direct_chat", key: m.key });
                continue;
            }
            const from = senderPhone(m.key);
            if (!from) {
                // A LID-addressed chat whose stanza carried no `sender_pn`.
                // Forwarding the LID instead would create a contact with an
                // unusable 15-digit "number" no reply could ever reach, so the
                // message is dropped -- loudly, because a sender we cannot
                // name is a customer nobody is going to answer.
                dropped.push({ reason: "no_sender_number", key: m.key });
                continue;
            }

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
            mapped.push({
                id: m.key.id,
                from,
                type: buttonReply ? "interactive" : mediaType ?? (text ? "text" : "unknown"),
                body: buttonReply ? buttonReply.title : text ?? caption,
                button_id: buttonReply?.id ?? null,
                push_name: m.pushName ?? null,
            });
        }

        if (dropped.length > 0) this.recordDroppedInbound(sessionId, dropped);

        if (mapped.length === 0) return;
        entry.inboundForwarded = (entry.inboundForwarded ?? 0) + mapped.length;
        entry.inboundLastForwardedAt = Date.now();
        await postMessageWebhook(entry.messageWebhookUrl, { messages: mapped }, logger);
    }

    /**
     * Say out loud what was thrown away, and remember how much of it there was.
     *
     * `own_message` is ordinary and stays at debug. Everything else is somebody
     * whose message this platform received and did not deliver, so it is logged
     * where it will be seen, with the keys attached -- a LID carrying no phone
     * number and a group leaking into the inbox are different faults, and the
     * key is what tells them apart.
     */
    private recordDroppedInbound(sessionId: string, dropped: Array<{ reason: DropReason; key: unknown }>): void {
        const entry = this.store.get(sessionId);
        const counts = {} as Record<DropReason, number>;
        for (const { reason } of dropped) counts[reason] = (counts[reason] ?? 0) + 1;

        const undecryptable = counts.undecryptable ?? 0;
        const notable = dropped.filter((d) => d.reason !== "own_message");
        if (entry) {
            entry.inboundUndecryptable = (entry.inboundUndecryptable ?? 0) + undecryptable;
            entry.inboundDropped = (entry.inboundDropped ?? 0) + notable.length;
        }

        if (notable.length === 0) {
            logger.debug({ sessionId, counts }, "Skipped this session's own messages");
            return;
        }

        logger.warn(
            {
                sessionId,
                counts,
                // Enough to identify the chats without turning one bad minute
                // into thousands of log lines.
                keys: notable.slice(0, 5).map((d) => d.key),
                totalDropped: entry?.inboundDropped ?? notable.length,
            },
            "Inbound WhatsApp messages arrived but were not delivered to the inbox",
        );

        if (undecryptable > 0) {
            logger.error(
                { sessionId, undecryptable, totalUndecryptable: entry?.inboundUndecryptable ?? undecryptable },
                "This session cannot read the messages being sent to it -- its stored Signal state is unusable. " +
                    "The number stays CONNECTED and the inbox simply stays empty, so nothing else reports this. " +
                    "Reconnect the number if it does not clear on its own.",
            );
        }
    }

    private async forwardStatusUpdates(sessionId: string, updates: any[]): Promise<void> {
        const entry = this.store.get(sessionId);
        if (!entry) {
            logger.warn({ sessionId }, "Delivery statuses arrived for a session no longer in the store");
            return;
        }

        const STATUS_MAP: Record<number, string> = { 2: "sent", 3: "delivered", 4: "read" };
        const mapped = updates
            .map((u) => {
                const id = u.key?.id as string | undefined;
                const status = STATUS_MAP[u.update?.status as number];
                // Echo the send_operation_id this message went out with, when we
                // still have it, so Django can tie the receipt to the right
                // message even if it never persisted our provider_message_id
                // (worker died before the save). See receivers.py.
                const op = id ? this.operations.operationForMessageId(id) : null;
                return op ? { id, status, send_operation_id: op } : { id, status };
            })
            .filter((s) => s.id && s.status);

        // A `messages.update` batch carries edits and revocations as well as
        // receipts, so "none of these were statuses" is normal and not a
        // fault. It is logged all the same: a message stuck at SENT is
        // indistinguishable from one whose receipt never arrived, and without
        // this line there is no way to tell which of the two happened.
        if (mapped.length === 0) {
            logger.debug(
                { sessionId, received: updates.length, raw: updates.map((u) => u.update?.status) },
                "messages.update carried no delivery statuses",
            );
            return;
        }

        logger.info({ sessionId, statuses: mapped }, "Forwarding delivery statuses");
        await postMessageWebhook(entry.messageWebhookUrl, { statuses: mapped }, logger);
    }

    getSession(sessionId: string): SessionSnapshot | undefined {
        const entry = this.store.get(sessionId);
        if (!entry) return undefined;
        return {
            ...entry.snapshot,
            inbound: {
                forwarded: entry.inboundForwarded ?? 0,
                dropped: entry.inboundDropped ?? 0,
                undecryptable: entry.inboundUndecryptable ?? 0,
                last_forwarded_at: entry.inboundLastForwardedAt
                    ? new Date(entry.inboundLastForwardedAt).toISOString()
                    : null,
            },
        };
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
        const operationId = message.send_operation_id ?? "";

        // Idempotency: if Django already sent this exact operation and we
        // recorded it (a success, or an ambiguous send we cannot vouch for),
        // replay that outcome instead of putting the message on the wire again.
        // A clean failure was never recorded, so it falls through and retries.
        if (operationId) {
            const prior = this.operations.lookup(operationId);
            if (prior) {
                logger.warn(
                    { sessionId, operationId, outcome: prior.outcome },
                    "Duplicate send_operation_id; replaying the earlier result without re-sending",
                );
                return prior.outcome === "sent"
                    ? { success: true, provider_message_id: prior.providerMessageId, error: null }
                    : {
                          success: false,
                          provider_message_id: prior.providerMessageId,
                          error: "A previous attempt for this operation was already in flight; not re-sending.",
                          ambiguous: true,
                      };
            }
        }

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
                sent = sendInteractiveButtons(sock, jid, message.interactive);
            } else if (message.type === "poll" && message.poll) {
                // A poll needs no wrapper or hand-built relay: unlike an
                // interactive message it is a first-class content type that
                // linked devices are meant to send, which is why this is a
                // plain sendMessage while sendInteractive() is not.
                sent = sock.sendMessage(jid, {
                    poll: {
                        name: message.poll.question,
                        values: message.poll.options,
                        selectableCount: message.poll.selectable_count,
                    },
                });
            } else if (message.type === "text" && message.text) {
                sent = sock.sendMessage(jid, { text: message.text.body });
            } else if (message.media) {
                sent = sock.sendMessage(jid, buildMediaContent(message.type, message.media));
            } else {
                return { success: false, provider_message_id: null, error: `Unsupported message type '${message.type}'.` };
            }
            const result = await withTimeout(sent, SEND_TIMEOUT_MS);
            const providerMessageId = result?.key?.id ?? null;
            if (operationId) this.operations.remember(operationId, "sent", providerMessageId);
            return { success: true, provider_message_id: providerMessageId, error: null };
        } catch (err) {
            if (err instanceof SendTimeout) {
                // Baileys never settled. It may still deliver, so this is
                // reported as ambiguous rather than a plain failure -- but we
                // answer now instead of letting the caller hang. Recorded so a
                // recovery retry with the same operation id does NOT re-send:
                // we cannot prove the first attempt failed.
                logger.error({ sessionId, jid }, "Send timed out inside Baileys; delivery unknown");
                if (operationId) this.operations.remember(operationId, "ambiguous", null);
                return {
                    success: false,
                    provider_message_id: null,
                    error: `WhatsApp did not confirm the send within ${SEND_TIMEOUT_MS / 1000}s; delivery is unknown.`,
                    ambiguous: true,
                };
            }
            // A clean rejection before anything went on the wire is NOT
            // recorded -- a legitimate retry should proceed.
            return { success: false, provider_message_id: null, error: (err as Error).message };
        }
    }

    /**
     * The live socket for a session, or a SessionNotConnected the routes turn
     * into a 409. The send path returns a SendResult instead because a send
     * has a recorded outcome; a lookup has nothing to record, so it throws.
     */
    private connectedSocket(sessionId: string): WASocket {
        const entry = this.store.get(sessionId);
        const sock = entry?.sock as WASocket | undefined;
        if (!entry || !sock || entry.snapshot.status !== "CONNECTED") {
            throw new SessionNotConnected();
        }
        return sock;
    }

    async checkNumbers(sessionId: string, numbers: string[]): Promise<NumberLookupResult[]> {
        const sock = this.connectedSocket(sessionId);

        // Asked by digits, answered by digits. The same number pasted twice
        // ("+91 98..." and "9198...") is one query and two rows in the answer,
        // because Django reports on every line it was given.
        const unique: string[] = [];
        const seen = new Set<string>();
        for (const raw of numbers) {
            const digits = String(raw ?? "").replace(/\D/g, "");
            if (digits && !seen.has(digits)) {
                seen.add(digits);
                unique.push(digits);
            }
        }

        // onWhatsApp() returns only the numbers that ARE registered, so absence
        // from the answer is the "not on WhatsApp" signal -- there is no
        // negative row to read.
        const registered = new Map<string, string>();
        for (let start = 0; start < unique.length; start += NUMBER_LOOKUP_BATCH) {
            const batch = unique.slice(start, start + NUMBER_LOOKUP_BATCH);
            const found = await withTimeout(sock.onWhatsApp(...batch), LOOKUP_TIMEOUT_MS, () => new LookupTimeout());
            for (const row of found ?? []) {
                if (!row?.exists || typeof row.jid !== "string") continue;
                registered.set(row.jid.split("@")[0].split(":")[0], row.jid);
            }
            if (start + NUMBER_LOOKUP_BATCH < unique.length) await sleep(NUMBER_LOOKUP_PAUSE_MS);
        }

        logger.info({ sessionId, asked: unique.length, registered: registered.size }, "Number lookup completed");
        return numbers.map((raw) => {
            const digits = String(raw ?? "").replace(/\D/g, "");
            const jid = digits ? registered.get(digits) ?? null : null;
            return { number: String(raw), exists: jid !== null, jid };
        });
    }

    async listGroups(sessionId: string): Promise<GroupSummary[]> {
        const sock = this.connectedSocket(sessionId);
        const all = await withTimeout(sock.groupFetchAllParticipating(), LOOKUP_TIMEOUT_MS, () => new LookupTimeout());
        return Object.values(all)
            .map((group) => ({
                id: group.id,
                subject: group.subject ?? "",
                member_count: group.size ?? group.participants?.length ?? 0,
                announce: Boolean(group.announce),
                is_community: Boolean(group.isCommunity),
                created_at: group.creation ? new Date(group.creation * 1000).toISOString() : null,
            }))
            .sort((a, b) => a.subject.localeCompare(b.subject));
    }

    async getGroupMembers(sessionId: string, groupIds: string[]): Promise<GroupMembers[]> {
        const sock = this.connectedSocket(sessionId);
        // The linked number is a member of every group it can see; importing
        // it into its own contact list would be a row nobody asked for. Its
        // own id carries a device suffix ("9198...:12@s.whatsapp.net"), which
        // phoneFromJid already strips.
        const ownPhone = phoneFromJid(sock.user?.id);

        const out: GroupMembers[] = [];
        for (const id of groupIds.slice(0, GROUP_MEMBERS_MAX_GROUPS)) {
            const meta = await withTimeout(sock.groupMetadata(id), LOOKUP_TIMEOUT_MS, () => new LookupTimeout());
            const members: GroupMember[] = [];
            let hidden = 0;
            for (const participant of meta.participants ?? []) {
                // A member is addressed by phone JID, by LID, or by both. Only
                // the phone form carries a number; a LID-only member is real
                // but unimportable, and is counted so the screen can say so.
                const jid = phoneFromJid(participant.jid) ? String(participant.jid) : phoneFromJid(participant.id) ? participant.id : "";
                const phone = phoneFromJid(jid);
                if (!phone) {
                    hidden += 1;
                    continue;
                }
                if (phone === ownPhone) continue;
                members.push({
                    phone,
                    jid,
                    name: participant.name ?? participant.notify ?? "",
                    is_admin: Boolean(participant.admin),
                });
            }
            out.push({ id: meta.id, subject: meta.subject ?? "", members, hidden_count: hidden });
        }
        logger.info(
            { sessionId, groups: out.length, members: out.reduce((n, g) => n + g.members.length, 0) },
            "Group member fetch completed",
        );
        return out;
    }

    getOperationStatus(operationId: string): OperationStatus {
        const rec = this.operations.lookup(operationId);
        if (!rec) {
            return { known: false, sent: false, provider_message_id: null, ambiguous: false };
        }
        return {
            known: true,
            sent: rec.outcome === "sent",
            provider_message_id: rec.providerMessageId,
            ambiguous: rec.outcome === "ambiguous",
        };
    }

    /**
     * Run the native-flow measurement matrix against a real recipient.
     *
     * Diagnostics only: nothing in the campaign path calls this, and it is
     * reachable solely through the API-key-guarded /diagnostics route. It runs
     * on the session's EXISTING socket on purpose -- a second socket over one
     * auth directory is what corrupted this project's Signal sessions before.
     */
    async runButtonDiagnostic(sessionId: string, input: ButtonLabInput): Promise<ButtonLabReport> {
        const entry = this.store.get(sessionId);
        const sock = entry?.sock as WASocket | undefined;
        if (!entry || !sock || entry.snapshot.status !== "CONNECTED") {
            throw new Error("This session is not connected.");
        }
        const jid = toJid(input.to);
        if (!jid) throw new Error(`Invalid recipient '${input.to}'.`);

        // The same hygiene the real send path runs. Without it a stale ratchet
        // would look exactly like "the server dropped my interactive message".
        //
        // Skipped in observe-only mode, which sends nothing: clearing a Signal
        // session there would tear down a working ratchet with the exact
        // contact whose tap we are waiting for, and a listen must not mutate
        // the thing it is listening to.
        if (!input.observeOnly) {
            await this.repairDeadHandshakes(sessionId, jid);
        }
        logger.warn({ sessionId, jid, observeOnly: !!input.observeOnly }, "Running native-flow button diagnostic");
        return runButtonLab(sock, jid, input);
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
 * This is the only entry point for a button message, and its argument is the
 * engine-agnostic `OutgoingInteractive` -- a body, an optional footer, and
 * ordered buttons carrying their own ids. Nothing above this function knows
 * that Baileys or protobuf exist.
 *
 * WHY THERE IS MORE THAN ONE WIRE SHAPE
 *
 * WhatsApp has carried tappable buttons in several envelopes and has retired
 * them at different times for different kinds of sender. A linked device can
 * put any of them on the wire, get a message id back and reach DELIVERED while
 * the recipient's phone renders only the body text -- there is no error at any
 * layer to distinguish "rendered" from "silently flattened". So which envelope
 * works is a question only a real handset can answer, and `format` exists so
 * that it can be answered by measurement rather than assumption, and re-answered
 * without a code change when Meta moves again.
 *
 * Returns the generated message, whose `key.id` is the id delivery receipts for
 * this send arrive against -- the same shape sendMessage() returns.
 */
async function sendInteractiveButtons(
    sock: WASocket,
    jid: string,
    interactive: OutgoingInteractive,
) {
    const format = interactive.format ?? config.interactiveFormat;
    const { body, footer, buttons } = interactive;

    // The only shape that declares itself to the server. Delegated whole to
    // nativeFlow.ts, which owns every Baileys-adjacent detail of it; a failure
    // here is returned to Django as a failed send rather than quietly degraded to
    // one of the shapes below, because a body with no readable buttons asks the
    // customer a question they cannot answer. The safe path is Django declining to
    // send interactive content at all (supports_interactive_buttons), which is
    // still where the numbered-text fallback comes from and is untouched by this.
    if (format === "native_flow_helper") {
        return sendNativeFlowButtons(sock, jid, { body, footer, buttons });
    }

    // The two shapes Baileys sends through its own sendMessage() path: they
    // are ordinary content types rather than hand-relayed protobuf, so they
    // need no wrapper and no generateWAMessageFromContent.
    if (format === "buttons_legacy") {
        return sock.sendMessage(jid, {
            text: body,
            ...(footer ? { footer } : {}),
            buttons: buttons.map((button) => ({
                buttonId: button.id,
                buttonText: { displayText: button.title },
                type: 1,
            })),
            headerType: 1,
        } as any);
    }

    if (format === "template") {
        return sock.sendMessage(jid, {
            text: body,
            ...(footer ? { footer } : {}),
            templateButtons: buttons.map((button, index) =>
                button.type === "URL"
                    ? { index: index + 1, urlButton: { displayText: button.title, url: button.url } }
                    : { index: index + 1, quickReplyButton: { displayText: button.title, id: button.id } },
            ),
        } as any);
    }

    // The remaining three are all interactiveMessage, hand-built and relayed.
    const interactiveMessage = proto.Message.InteractiveMessage.create({
        body: proto.Message.InteractiveMessage.Body.create({ text: body }),
        ...(footer
            ? { footer: proto.Message.InteractiveMessage.Footer.create({ text: footer }) }
            : {}),
        nativeFlowMessage: proto.Message.InteractiveMessage.NativeFlowMessage.create({
            buttons: buttons.map(toNativeFlowButton),
            // Present only in native_flow_v1. The proto defines both fields and
            // the wrapper-only shape omits them; whether a client requires them
            // is exactly the kind of thing only a handset can tell us.
            ...(format === "native_flow_v1"
                ? { messageParamsJson: "{}", messageVersion: 1 }
                : {}),
        }),
    });

    // `interactive_direct` skips the viewOnceMessage wrapper. The wrapper is
    // not about disappearing messages -- an interactive message has
    // historically had to arrive inside a wrapper the client unpacks before it
    // will render buttons -- but that requirement is itself a moving target,
    // so sending without it is one of the shapes worth measuring.
    const content =
        format === "interactive_direct"
            ? { interactiveMessage }
            : {
                  viewOnceMessage: {
                      message: {
                          messageContextInfo: {
                              deviceListMetadata: {},
                              deviceListMetadataVersion: 2,
                          },
                          interactiveMessage,
                      },
                  },
              };

    const message = generateWAMessageFromContent(jid, content as any, {
        userJid: sock.user?.id ?? "",
    });

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

/** Baileys hangs forever on a malformed JID, so no send may run unbounded. */
const SEND_TIMEOUT_MS = 30_000;

class SendTimeout extends Error {}

/** A registration or group query WhatsApp never answered. Not a delivery question, so never "ambiguous". */
class LookupTimeout extends Error {
    constructor() {
        super(`WhatsApp did not answer the lookup within ${LOOKUP_TIMEOUT_MS / 1000}s.`);
        this.name = "LookupTimeout";
    }
}

function withTimeout<T>(promise: Promise<T>, ms: number, error: () => Error = () => new SendTimeout()): Promise<T> {
    let timer: NodeJS.Timeout;
    return Promise.race([
        promise.finally(() => clearTimeout(timer)),
        new Promise<never>((_, reject) => {
            timer = setTimeout(() => reject(error()), ms);
        }),
    ]);
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
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
