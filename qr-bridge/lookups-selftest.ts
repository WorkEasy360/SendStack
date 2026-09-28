/**
 * lookups-selftest -- the number filter and the group grabber, checked
 * without a phone or a real socket.
 *
 * Usage (from qr-bridge/):  npx tsx lookups-selftest.ts
 *
 * What this proves, all offline:
 *
 *   1. checkNumbers asks by bare digits, once per distinct number, in batches
 *      of NUMBER_LOOKUP_BATCH, and answers every input in the order it was
 *      given -- including the same number written two ways;
 *   2. "absent from onWhatsApp()'s answer" is read as not registered, and a
 *      registered number carries its JID back;
 *   3. listGroups returns every group the socket reports, sorted by subject,
 *      with the member count and creation time in the documented shape;
 *   4. getGroupMembers returns phone digits for phone-addressed members,
 *      counts LID-only members as hidden rather than guessing, leaves the
 *      linked number itself out, and reads at most GROUP_MEMBERS_MAX_GROUPS;
 *   5. every lookup on a session with no live socket throws
 *      SessionNotConnected, which routes.ts answers with 409.
 *
 * It uses the real BaileysProvider over a stub socket. No credentials, no
 * session, nothing sent.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// config.ts refuses to load without the two shared secrets; nothing here
// ever uses them.
process.env.QR_PROVIDER_API_KEY ??= "selftest";
process.env.QR_PROVIDER_WEBHOOK_SECRET ??= "selftest";
process.env.QR_BRIDGE_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "qr-lookups-"));
process.env.QR_BRIDGE_LOG_LEVEL = "warn";

const { BaileysProvider } = await import("./src/providers/baileysProvider.js");
const { SessionStore } = await import("./src/sessionStore.js");
const { GROUP_MEMBERS_MAX_GROUPS, SessionNotConnected } = await import("./src/providers/base.js");

const store = new SessionStore(process.env.QR_BRIDGE_DATA_DIR);
const provider = new BaileysProvider(store);

const queries: string[][] = [];
const metadataAsked: string[] = [];

const registered = new Set(["15550000001", "919876543210"]);

/** The three Baileys calls the lookups use, and nothing else. */
const sock = {
    user: { id: "15550009999:12@s.whatsapp.net" },
    onWhatsApp: async (...jids: string[]) => {
        queries.push(jids);
        return jids
            .filter((digits) => registered.has(digits))
            .map((digits) => ({ jid: `${digits}@s.whatsapp.net`, exists: true, lid: `${digits}@lid` }));
    },
    groupFetchAllParticipating: async () => ({
        "2@g.us": { id: "2@g.us", subject: "Zebra traders", size: 40, participants: [], creation: 1735689600, announce: true },
        "1@g.us": { id: "1@g.us", subject: "Apple buyers", participants: [{ id: "a@lid" }, { id: "b@lid" }], isCommunity: true },
    }),
    groupMetadata: async (jid: string) => {
        metadataAsked.push(jid);
        return {
            id: jid,
            subject: `Group ${jid}`,
            participants: [
                // Phone-addressed, with a name and admin rights.
                { id: "15550000001@s.whatsapp.net", jid: "15550000001@s.whatsapp.net", admin: "superadmin", notify: "Asha" },
                // LID-addressed but WhatsApp gave the phone alongside it.
                { id: "77@lid", jid: "15550000002@s.whatsapp.net", lid: "77@lid", admin: null },
                // LID only: real, unimportable, counted.
                { id: "88@lid", jid: "", lid: "88@lid", admin: null },
                // The linked number itself.
                { id: "15550009999@s.whatsapp.net", jid: "15550009999@s.whatsapp.net", admin: "admin" },
            ],
        };
    },
};

function connect(sessionId: string, liveSock: unknown, status: "CONNECTED" | "QR_READY" = "CONNECTED") {
    store.set(sessionId, {
        accountId: 1,
        connectionWebhookUrl: "http://django.invalid/c",
        messageWebhookUrl: "http://django.invalid/m",
        sock: liveSock,
        snapshot: {
            session_id: sessionId,
            status,
            qr: null,
            expires_at: null,
            phone_number: "15550009999",
            display_name: "Self test",
            provider_account_id: null,
            error: null,
        },
    });
}

try {
    connect("live", sock);

    // 1 + 2. Number filter.
    const inputs = ["+1 555 000 0001", "15550000001", "+91 98765 43210", "15550000003", "", "not-a-number"];
    const results = await provider.checkNumbers("live", inputs);
    assert.deepEqual(
        results.map((r) => r.number),
        inputs,
        "one answer per input, in input order",
    );
    assert.deepEqual(
        results.map((r) => r.exists),
        [true, true, true, false, false, false],
        "registered numbers are found however they were written; the rest are not",
    );
    assert.equal(results[0].jid, "15550000001@s.whatsapp.net", "a registered number carries its JID");
    assert.equal(results[3].jid, null, "an unregistered number carries no JID");
    assert.equal(queries.length, 1, "distinct digits fit one batch, so one query");
    assert.deepEqual(queries[0], ["15550000001", "919876543210", "15550000003"], "asked by bare digits, deduplicated");

    queries.length = 0;
    const many = Array.from({ length: 120 }, (_, i) => `1555100${String(i).padStart(4, "0")}`);
    const manyResults = await provider.checkNumbers("live", many);
    assert.equal(manyResults.length, 120, "every number answered");
    assert.deepEqual(queries.map((q) => q.length), [50, 50, 20], "batched at NUMBER_LOOKUP_BATCH");

    // 3. Group listing.
    const groups = await provider.listGroups("live");
    assert.deepEqual(
        groups.map((g) => g.subject),
        ["Apple buyers", "Zebra traders"],
        "sorted by subject",
    );
    assert.equal(groups[1].member_count, 40, "size wins when WhatsApp reports it");
    assert.equal(groups[0].member_count, 2, "otherwise the participant list is counted");
    assert.equal(groups[1].created_at, "2025-01-01T00:00:00.000Z", "creation is epoch seconds, reported as ISO 8601");
    assert.equal(groups[1].announce, true);
    assert.equal(groups[0].is_community, true);

    // 4. Group members.
    const [members] = await provider.getGroupMembers("live", ["1@g.us"]);
    assert.equal(members.subject, "Group 1@g.us");
    assert.deepEqual(
        members.members.map((m) => [m.phone, m.name, m.is_admin]),
        [
            ["15550000001", "Asha", true],
            ["15550000002", "", false],
        ],
        "phone-addressed members come back as digits; the linked number is left out",
    );
    assert.equal(members.hidden_count, 1, "a LID-only member is counted, not guessed at");

    metadataAsked.length = 0;
    const tooMany = Array.from({ length: GROUP_MEMBERS_MAX_GROUPS + 5 }, (_, i) => `${i}@g.us`);
    const capped = await provider.getGroupMembers("live", tooMany);
    assert.equal(capped.length, GROUP_MEMBERS_MAX_GROUPS, "at most GROUP_MEMBERS_MAX_GROUPS groups are read");
    assert.equal(metadataAsked.length, GROUP_MEMBERS_MAX_GROUPS, "and no more are asked for");

    // 5. Not connected.
    connect("pairing", sock, "QR_READY");
    connect("dead", undefined);
    for (const [sessionId, why] of [
        ["pairing", "a session still showing a QR"],
        ["dead", "a session with no socket"],
        ["unknown", "a session the bridge never saw"],
    ] as const) {
        await assert.rejects(provider.checkNumbers(sessionId, ["15550000001"]), SessionNotConnected, `${why}: checkNumbers`);
        await assert.rejects(provider.listGroups(sessionId), SessionNotConnected, `${why}: listGroups`);
        await assert.rejects(provider.getGroupMembers(sessionId, ["1@g.us"]), SessionNotConnected, `${why}: getGroupMembers`);
    }

    // process.stdout rather than console: baileysProvider routes console.*
    // into pino at import time (see captureHelperConsole), and the level set
    // above keeps that quiet.
    process.stdout.write("lookups-selftest: all checks passed\n");
} finally {
    fs.rmSync(process.env.QR_BRIDGE_DATA_DIR!, { recursive: true, force: true });
}
