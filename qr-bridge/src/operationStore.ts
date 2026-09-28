import fs from "node:fs";
import path from "node:path";

/**
 * Idempotency for outbound sends, keyed by the `send_operation_id` Django
 * puts on each send attempt.
 *
 * The problem it solves: Django POSTs a message, Baileys accepts it, and the
 * HTTP response is lost (worker OOM, container restart, network blip). Django
 * cannot tell whether the message went out. When its recovery sweep retries,
 * the same `send_operation_id` arrives again -- and without this store the
 * bridge would send the message a second time.
 *
 * What is remembered, and what is not:
 *
 *  - a **successful** send  -> remembered; a repeat returns the stored
 *    provider_message_id and does NOT re-send.
 *  - an **ambiguous** send (Baileys never settled) -> remembered as ambiguous;
 *    a repeat returns ambiguous again, still without re-sending, because we
 *    genuinely do not know if the first one landed.
 *  - a **clean failure** (rejected before anything went on the wire) -> NOT
 *    remembered, so a legitimate retry proceeds normally.
 *
 * Records expire after `TTL_MS` (a send that is still unresolved a day later
 * is a manual-review case, not something a retry should act on), and the whole
 * store is capped so a long-running bridge cannot grow it without bound.
 */

const TTL_MS = 24 * 60 * 60 * 1000;
const MAX_RECORDS = 50_000;

export interface OperationRecord {
    operationId: string;
    /** "sent" once WhatsApp acknowledged; "ambiguous" when Baileys never settled. */
    outcome: "sent" | "ambiguous";
    providerMessageId: string | null;
    /** Epoch ms. */
    at: number;
}

export class OperationStore {
    private records = new Map<string, OperationRecord>();
    /** provider_message_id -> operationId, for echoing on delivery receipts. */
    private byMessageId = new Map<string, string>();
    private readonly file: string;
    private writeQueued = false;

    constructor(dataDir: string) {
        this.file = path.join(dataDir, "operations.json");
        this.load();
    }

    private index(rec: OperationRecord): void {
        if (rec.providerMessageId) this.byMessageId.set(rec.providerMessageId, rec.operationId);
    }

    private load(): void {
        try {
            const raw = fs.readFileSync(this.file, "utf8");
            const parsed = JSON.parse(raw) as OperationRecord[];
            const cutoff = Date.now() - TTL_MS;
            for (const rec of parsed) {
                if (rec && typeof rec.operationId === "string" && rec.at > cutoff) {
                    this.records.set(rec.operationId, rec);
                    this.index(rec);
                }
            }
        } catch {
            // No file yet, or an unreadable one -- start empty. A lost dedup
            // store degrades to "might re-send on the rare recovery retry",
            // never to a crash.
        }
    }

    private persist(): void {
        // Coalesce bursts of writes into one flush on the next tick.
        if (this.writeQueued) return;
        this.writeQueued = true;
        setImmediate(() => {
            this.writeQueued = false;
            try {
                const tmp = `${this.file}.tmp`;
                fs.writeFileSync(tmp, JSON.stringify([...this.records.values()]), { mode: 0o600 });
                fs.renameSync(tmp, this.file);
            } catch {
                // Best effort. The in-memory map still protects against a
                // same-process retry, which is the common recovery case.
            }
        });
    }

    private drop(key: string): void {
        const rec = this.records.get(key);
        this.records.delete(key);
        if (rec?.providerMessageId && this.byMessageId.get(rec.providerMessageId) === key) {
            this.byMessageId.delete(rec.providerMessageId);
        }
    }

    private prune(): void {
        const cutoff = Date.now() - TTL_MS;
        for (const [key, rec] of this.records) {
            if (rec.at <= cutoff) this.drop(key);
        }
        if (this.records.size > MAX_RECORDS) {
            // Oldest-first; Map preserves insertion order.
            const excess = this.records.size - MAX_RECORDS;
            let i = 0;
            for (const key of [...this.records.keys()]) {
                if (i++ >= excess) break;
                this.drop(key);
            }
        }
    }

    lookup(operationId: string): OperationRecord | undefined {
        if (!operationId) return undefined;
        const rec = this.records.get(operationId);
        if (!rec) return undefined;
        if (rec.at <= Date.now() - TTL_MS) {
            this.drop(operationId);
            return undefined;
        }
        return rec;
    }

    remember(operationId: string, outcome: "sent" | "ambiguous", providerMessageId: string | null): void {
        if (!operationId) return;
        const rec: OperationRecord = { operationId, outcome, providerMessageId, at: Date.now() };
        this.records.set(operationId, rec);
        this.index(rec);
        this.prune();
        this.persist();
    }

    /** The send_operation_id a given provider message id went out under, if known. */
    operationForMessageId(providerMessageId: string): string | null {
        return this.byMessageId.get(providerMessageId) ?? null;
    }
}
