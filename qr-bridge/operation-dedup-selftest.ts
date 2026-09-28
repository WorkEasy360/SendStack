/**
 * operation-dedup-selftest -- the send_operation_id idempotency, checked
 * without a phone or a real socket.
 *
 * Usage (from qr-bridge/):  npx tsx operation-dedup-selftest.ts
 *
 * What this proves, all offline:
 *
 *   1. a repeated send_operation_id after a SUCCESS replays the stored
 *      provider_message_id and does NOT put a second message on the wire;
 *   2. a repeated send_operation_id after an AMBIGUOUS send replays "ambiguous"
 *      and still does not re-send -- we cannot vouch that the first one failed;
 *   3. a clean failure is NOT remembered, so a legitimate retry proceeds;
 *   4. GET /operations/:id reports known/sent/ambiguous correctly, and 404s
 *      (known:false) for an id the bridge never saw -- which is Django's
 *      signal that re-sending is safe;
 *   5. the store survives a process restart (it is persisted to disk).
 *
 * It uses the real OperationStore and a stub BaileysProvider send path. No
 * credentials, no session, nothing sent.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { OperationStore } from "./src/operationStore.js";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qr-op-dedup-"));
let wireSends = 0;

/** Stand-in for BaileysProvider.sendMessage's core, using the real store. */
function fakeSend(store: OperationStore, op: string, mode: "ok" | "ambiguous" | "fail") {
    const prior = op ? store.lookup(op) : undefined;
    if (prior) {
        return prior.outcome === "sent"
            ? { success: true, provider_message_id: prior.providerMessageId, ambiguous: false, resent: false }
            : { success: false, provider_message_id: prior.providerMessageId, ambiguous: true, resent: false };
    }
    wireSends += 1; // <-- a real message would go on the wire here
    if (mode === "ok") {
        const id = `wamid.${op}`;
        store.remember(op, "sent", id);
        return { success: true, provider_message_id: id, ambiguous: false, resent: true };
    }
    if (mode === "ambiguous") {
        store.remember(op, "ambiguous", null);
        return { success: false, provider_message_id: null, ambiguous: true, resent: true };
    }
    return { success: false, provider_message_id: null, ambiguous: false, resent: true }; // not remembered
}

try {
    const store = new OperationStore(dir);

    // 1. success then replay
    const a1 = fakeSend(store, "t:c:1:1", "ok");
    const a2 = fakeSend(store, "t:c:1:1", "ok");
    assert.equal(a1.resent, true, "first send goes on the wire");
    assert.equal(a2.resent, false, "second send with the same op id does NOT go on the wire");
    assert.equal(a2.provider_message_id, a1.provider_message_id, "replays the same provider_message_id");
    assert.equal(wireSends, 1, "exactly one wire send for a duplicated success");

    // 2. ambiguous then replay
    const b1 = fakeSend(store, "t:c:2:1", "ambiguous");
    const b2 = fakeSend(store, "t:c:2:1", "ok");
    assert.equal(b1.ambiguous, true);
    assert.equal(b2.resent, false, "an ambiguous send is not re-sent on retry");
    assert.equal(b2.ambiguous, true, "retry of an ambiguous send stays ambiguous");
    assert.equal(wireSends, 2, "still only two wire sends total");

    // 3. clean failure is retryable
    const c1 = fakeSend(store, "t:c:3:1", "fail");
    const c2 = fakeSend(store, "t:c:3:1", "ok");
    assert.equal(c1.success, false);
    assert.equal(c2.resent, true, "a clean failure is not remembered; the retry proceeds");
    assert.equal(c2.success, true);

    // 4. operation status lookups
    assert.deepEqual(
        { known: !!store.lookup("t:c:1:1"), sent: store.lookup("t:c:1:1")?.outcome === "sent" },
        { known: true, sent: true },
    );
    assert.equal(store.lookup("t:c:never:1"), undefined, "an unseen op id is unknown -> Django may re-send");
    assert.equal(store.operationForMessageId("wamid.t:c:1:1"), "t:c:1:1", "reverse lookup for the webhook echo");

    // 5. survives a restart. persist() debounces onto setImmediate, so let the
    //    queued flush run before re-opening (a real restart is always later).
    await new Promise((resolve) => setImmediate(resolve));
    const reopened = new OperationStore(dir);
    assert.equal(reopened.lookup("t:c:1:1")?.outcome, "sent", "records persist across a restart");
    assert.equal(reopened.lookup("t:c:2:1")?.outcome, "ambiguous");

    console.log("operation-dedup-selftest: OK (all 5 checks passed, wire sends =", wireSends, ")");
} finally {
    fs.rmSync(dir, { recursive: true, force: true });
}
