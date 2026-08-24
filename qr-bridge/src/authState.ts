import fs from "node:fs";
import {
    makeCacheableSignalKeyStore,
    useMultiFileAuthState,
    type AuthenticationState,
} from "@whiskeysockets/baileys";
import type { Logger } from "pino";

export interface SharedAuthState {
    state: AuthenticationState;
    saveCreds: () => Promise<void>;
}

const shared = new Map<string, Promise<SharedAuthState>>();

/**
 * One auth state per session directory, shared by every socket that session
 * ever opens.
 *
 * useMultiFileAuthState() builds a *fresh* in-memory `creds` object on every
 * call, and its file mutex only guards one file operation at a time -- it does
 * not make libsignal's load -> mutate -> store sequence atomic. So whenever a
 * reconnect left a second socket alive on the same folder, the two held
 * divergent creds (including the pre-key counters) and raced each other's
 * Signal session writes. The peer then received messages encrypted against a
 * ratchet it had never seen, and showed "Waiting for this message" forever.
 *
 * Handing every socket the same state removes that divergence. openSocket()
 * keeping exactly one socket alive per session is the other half of the fix;
 * this is what makes an overlap survivable if one ever slips through.
 */
export function useSharedAuthState(dir: string, logger: Logger): Promise<SharedAuthState> {
    let handle = shared.get(dir);
    if (!handle) {
        handle = build(dir, logger).catch((err) => {
            // A failed build must not stay cached, or this session could never
            // open again for the lifetime of the process.
            shared.delete(dir);
            throw err;
        });
        shared.set(dir, handle);
    }
    return handle;
}

async function build(dir: string, logger: Logger): Promise<SharedAuthState> {
    const { state, saveCreds } = await useMultiFileAuthState(dir);
    return {
        state: {
            creds: state.creds,
            // Keeps repeated reads of one Signal address consistent within the
            // process rather than re-reading the file for every operation.
            // Everything that mutates sessions must go through this store --
            // writing the files directly would leave the cache serving what it
            // read before the change.
            keys: makeCacheableSignalKeyStore(state.keys, logger as any),
        },
        saveCreds,
    };
}

/** Drops the cached state for a session that has been disconnected/removed. */
export function forgetAuthState(dir: string): void {
    shared.delete(dir);
}

/** How long a handshake may go unanswered before we treat it as dead. */
const STALE_HANDSHAKE_MS = 10 * 60 * 1000;
/** ...and how many messages we must have sent into it unanswered. */
const STALE_HANDSHAKE_SENDS = 3;

/**
 * Signal addresses this session holds a stored session for, read from the
 * folder's filenames. Used only to narrow which addresses are worth loading
 * through the key store; the records themselves are always read from the store
 * so the cache stays authoritative.
 */
export function sessionAddressesFor(dir: string, userPrefix: string): string[] {
    let entries: string[];
    try {
        entries = fs.readdirSync(dir);
    } catch {
        return [];
    }

    return entries
        .filter((name) => name.startsWith("session-") && name.endsWith(".json"))
        .map((name) => name.slice("session-".length, -".json".length))
        // Records are addressed "<user>.<device>", so this covers every device
        // the recipient has linked.
        .filter((address) => address.startsWith(userPrefix));
}

/**
 * Whether the peer has never acknowledged this Signal session.
 *
 * libsignal clears `pendingPreKey` the moment the other side answers inside the
 * session. While it is still set, every message we send re-announces the
 * session and quotes the *one-time* pre-key we opened it with -- and the peer
 * consumed that key the first time it saw it. So each later message references
 * a pre-key the recipient no longer holds, cannot be decrypted, and shows up as
 * "Waiting for this message. This may take a while."
 *
 * Nothing recovers from that on its own: we only stop re-announcing once the
 * peer replies inside the session, which they cannot do while they cannot read
 * us. Resetting the record breaks the deadlock -- the next send fetches a fresh
 * pre-key bundle and opens a clean session the peer can actually read.
 *
 * The age and send-count floors keep a healthy new conversation out of this: a
 * session legitimately carries `pendingPreKey` until the contact's first
 * in-session reply.
 */
export function isDeadHandshake(record: any, now = Date.now()): boolean {
    const records = Object.values(record?._sessions ?? {}) as any[];
    if (records.length === 0) return false;

    // Only worth resetting when *every* stored record is unusable: one
    // completed session among them means the peer can still read us.
    if (!records.every((entry) => !!entry?.pendingPreKey)) return false;

    const oldest = Math.min(...records.map((entry) => Number(entry?.indexInfo?.created) || now));
    const sends = Math.max(
        0,
        ...records.map((entry) => {
            const chains = Object.values(entry?._chains ?? {}) as any[];
            return chains.reduce((max, chain) => Math.max(max, Number(chain?.chainKey?.counter) || 0), 0);
        }),
    );

    return now - oldest >= STALE_HANDSHAKE_MS && sends >= STALE_HANDSHAKE_SENDS;
}
