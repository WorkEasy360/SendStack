import fs from "node:fs";
import path from "node:path";
import type { SessionSnapshot } from "./providers/base.js";

export interface SessionEntry {
    snapshot: SessionSnapshot;
    accountId: number;
    connectionWebhookUrl: string;
    messageWebhookUrl: string;
    expiryTimer?: NodeJS.Timeout;
    // Absolute epoch-ms deadline for this session's overall QR/pairing
    // window, set once (session creation or resume) and never pushed back.
    // Baileys regenerates the QR internally every ~60s and closes with
    // statusCode 408 ("QR refs attempts ended") once its own retry budget
    // is exhausted; that close is handled as a normal reconnect (see
    // baileysProvider's connection.update handler) so the user keeps
    // seeing a live QR. Without a fixed deadline, each of those internal
    // refreshes used to re-arm a fresh WHATSAPP_QR_SESSION_TTL_SECONDS
    // timer, so a session nobody ever scanned would reconnect forever
    // instead of expiring.
    deadlineAt?: number;
    // Set by baileysProvider once the socket exists; typed loosely here so
    // this module never needs to import Baileys types.
    sock?: unknown;
    // True only while we ourselves are tearing this socket down (TTL
    // expiry or an explicit disconnect). Baileys' own 'connection.update'
    // close handler must check this before deciding to reconnect --
    // otherwise a deliberate close (or Baileys' own QR-refresh-attempts-
    // exhausted timeout, which looks identical to a network drop) gets
    // treated as "reconnect", silently reviving a session we just expired
    // and looping forever against WhatsApp's real servers.
    closing?: boolean;
    // Bumped every time this session opens a socket. Each socket's handlers
    // capture the value they were registered under and go quiet once it moves
    // on, so a socket we have already replaced can never act on its own close.
    generation?: number;
    // The in-flight openSocket() call, so two closes arriving together cannot
    // each build a socket against this session's auth folder.
    opening?: Promise<void>;
    // Consecutive failed reconnects, reset on a successful connection, and the
    // timer holding the next attempt.
    reconnectAttempts?: number;
    reconnectTimer?: NodeJS.Timeout;

    // Inbound accounting for this session, kept because "connected" and
    // "delivering" are not the same thing and only these tell them apart. A
    // session whose Signal state has gone bad reports CONNECTED for as long as
    // the socket holds, receives everything sent to it, and forwards none of
    // it -- see DropReason in providers/baileysProvider.ts. In memory only:
    // this is a live health signal, not a record, and a restart is exactly
    // when it should start counting again.
    inboundForwarded?: number;
    inboundDropped?: number;
    inboundUndecryptable?: number;
    inboundLastForwardedAt?: number;
}

/** In-memory session registry, keyed by our own session_id (not Baileys' internal state). */
export class SessionStore {
    private sessions = new Map<string, SessionEntry>();

    constructor(private readonly dataDir: string) {
        // 0700. This directory holds Baileys' Signal identity keys and device
        // credentials for every connected WhatsApp number -- whoever can read
        // it can impersonate those numbers. The container already runs as the
        // unprivileged `app` user and the volume is not published anywhere, so
        // this is the third lock rather than the first; it costs nothing and it
        // is what stops a future sidecar or a bind-mounted host directory from
        // quietly making the files world-readable. See SECURITY.md.
        fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
        try {
            // mkdirSync's mode is ignored when the directory already exists,
            // which is every restart after the first.
            fs.chmodSync(dataDir, 0o700);
        } catch {
            // A volume driver that does not support chmod (some Windows and
            // NFS mounts) is not a reason to refuse to start.
        }
    }

    authDirFor(sessionId: string): string {
        return path.join(this.dataDir, sessionId);
    }

    set(sessionId: string, entry: SessionEntry): void {
        this.sessions.set(sessionId, entry);
    }

    get(sessionId: string): SessionEntry | undefined {
        return this.sessions.get(sessionId);
    }

    delete(sessionId: string): void {
        const entry = this.sessions.get(sessionId);
        if (entry?.expiryTimer) clearTimeout(entry.expiryTimer);
        if (entry?.reconnectTimer) clearTimeout(entry.reconnectTimer);
        this.sessions.delete(sessionId);
    }

    /** Auth-state directories left on disk from a previous run (see index.ts's boot-time resume). */
    persistedSessionIds(): string[] {
        return fs
            .readdirSync(this.dataDir, { withFileTypes: true })
            .filter((entry) => entry.isDirectory())
            .map((entry) => entry.name);
    }

    removePersisted(sessionId: string): void {
        fs.rmSync(this.authDirFor(sessionId), { recursive: true, force: true });
    }
}
