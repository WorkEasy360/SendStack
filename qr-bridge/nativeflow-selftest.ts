/**
 * nativeflow-selftest -- everything about the native-flow engine that can be
 * checked without a phone.
 *
 * Usage (from qr-bridge/):  npx tsx nativeflow-selftest.ts
 *
 * WHAT THIS IS FOR, AND WHAT IT IS NOT FOR
 *
 * It does NOT prove buttons render. Nothing short of a handset does, which is
 * why qr-button-test.ts exists and why HTTP 200, a message id and a websocket
 * ack are all explicitly not success. What this proves is the half of the
 * problem that IS decidable offline, so a failed handset test can be read as
 * "WhatsApp refused it" rather than "maybe we built it wrong":
 *
 *   1. the engine loads at all against OUR installed Baileys 6.7.24 -- it
 *      resolves Baileys with `require`, and Baileys 6.7.24 is ESM, so this
 *      silently depends on Node >= 20.19 having require(esm). On an older Node
 *      the engine throws "Missing baileys internals" only at send time, on a
 *      live customer message;
 *   2. our button ids survive into `nativeFlowMessage.buttons[].buttonParamsJson`
 *      byte for byte -- an engine that renamed or regenerated them would give
 *      the customer working buttons that match nothing in Django;
 *   3. the stanza nodes injected are the ones this whole change is about
 *      (biz > interactive > native_flow v=9, plus <bot biz_bot="1"/> in a
 *      private chat);
 *   4. a tap flattens back to the SAME id, through the same function the
 *      production webhook path uses.
 *
 * It runs against a stub socket. No credentials are read, no session is
 * touched, nothing is sent.
 */
import { normalizeMessageContent, isJidGroup } from "@whiskeysockets/baileys";

import { sendNativeFlowButtons, nativeFlowStanzaNodes, toHelperButtons } from "./src/nativeFlow.js";
import { extractButtonReply } from "./src/buttonReply.js";
import type { InteractiveButton } from "./src/providers/base.js";

/** The exact input from the brief. */
const BODY = "Are you interested in image ??";
const BUTTONS: InteractiveButton[] = [
    { type: "REPLY", id: "interested", title: "Interested", url: "" },
    { type: "REPLY", id: "not_interested", title: "Not Interested", url: "" },
];

const JID = "919999999999@s.whatsapp.net";
const ME = "918888888888:1@s.whatsapp.net";

let failures = 0;

function check(label: string, condition: boolean, detail?: unknown) {
    console.log(`${condition ? "PASS" : "FAIL"}  ${label}`);
    if (!condition) {
        failures++;
        if (detail !== undefined) console.log(`      got: ${JSON.stringify(detail)}`);
    }
}

/**
 * A socket that records instead of sending.
 *
 * Only the surface the engine actually touches is implemented, so anything it
 * reaches for that is missing throws here rather than in production.
 */
function stubSocket() {
    const relayed: { jid: string; message: any; options: any }[] = [];
    const sock = {
        user: { id: ME },
        authState: { creds: { me: { id: ME } } },
        logger: { level: "silent", child: () => ({ level: "silent" }) },
        config: { emitOwnEvents: false },
        relayMessage: async (jid: string, message: any, options: any) => {
            relayed.push({ jid, message, options });
        },
    } as any;
    return { relayed, sock };
}

async function main() {
    // The engine reports through console; keep the report readable.
    const noise = console.log;
    console.log = () => {};
    const { sock, relayed } = stubSocket();
    let sent: any;
    let sendError: Error | null = null;
    try {
        sent = await sendNativeFlowButtons(sock, JID, { body: BODY, buttons: BUTTONS });
    } catch (err) {
        sendError = err as Error;
    }
    console.log = noise;

    console.log("nativeflow-selftest -- offline checks on the native-flow engine\n");

    check(
        "engine loaded and relayed against installed Baileys",
        sendError === null && relayed.length === 1,
        sendError?.message,
    );
    if (sendError) {
        console.log(`\n      ${sendError.message}`);
        console.log("      If this says 'Missing baileys internals', the Node running this");
        console.log(`      (${process.version}) cannot require() ESM. Needs >= 20.19.`);
        process.exit(1);
    }

    const relay = relayed[0];
    const content: any = normalizeMessageContent(relay.message);
    const flow = content?.interactiveMessage?.nativeFlowMessage;

    check("relayed content is an interactiveMessage/nativeFlowMessage", !!flow, Object.keys(content ?? {}));
    check("no viewOnceMessage wrapper", !relay.message?.viewOnceMessage);
    check(
        "body text is carried verbatim",
        content?.interactiveMessage?.body?.text === BODY,
        content?.interactiveMessage?.body?.text,
    );

    // 2. Button ids survive the engine's normalisation.
    const params = (flow?.buttons ?? []).map((b: any) => ({
        name: b.name,
        ...JSON.parse(b.buttonParamsJson),
    }));
    check(
        "two quick_reply buttons",
        params.length === 2 && params.every((p: any) => p.name === "quick_reply"),
        params,
    );
    check('button[0].id === "interested"', params[0]?.id === "interested", params[0]);
    check('button[0] label === "Interested"', params[0]?.display_text === "Interested", params[0]);
    check('button[1].id === "not_interested"', params[1]?.id === "not_interested", params[1]);
    check('button[1] label === "Not Interested"', params[1]?.display_text === "Not Interested", params[1]);

    // 3. The stanza nodes -- the actual point of the change.
    const nodes = relay.options?.additionalNodes ?? [];
    const biz = nodes.find((n: any) => n.tag === "biz");
    const interactive = biz?.content?.find((n: any) => n.tag === "interactive");
    const nativeFlow = interactive?.content?.find((n: any) => n.tag === "native_flow");
    const bot = nodes.find((n: any) => n.tag === "bot");

    check("stanza declares <biz>", !!biz, nodes.map((n: any) => n.tag));
    check(
        'stanza declares <interactive type="native_flow">',
        interactive?.attrs?.type === "native_flow",
        interactive?.attrs,
    );
    check(
        'stanza declares <native_flow v="9" name="mixed">',
        nativeFlow?.attrs?.v === "9" && nativeFlow?.attrs?.name === "mixed",
        nativeFlow?.attrs,
    );
    check('private chat carries <bot biz_bot="1"/>', bot?.attrs?.biz_bot === "1", bot?.attrs);
    check("relayed under the id the caller is handed back", !!sent?.key?.id && relay.options?.messageId === sent.key.id, {
        returned: sent?.key?.id,
        relayed: relay.options?.messageId,
    });

    // The lab re-derives these nodes from the relayed message; if that drifts
    // from what actually went out, the lab report lies about its own run.
    const rederived = nativeFlowStanzaNodes(content, !isJidGroup(JID));
    check(
        "lab re-derivation matches the nodes actually injected",
        JSON.stringify(rederived) === JSON.stringify(nodes),
        { rederived, injected: nodes },
    );

    // 4. The tap comes back as the same id, through the production flattener.
    for (const id of ["interested", "not_interested"]) {
        const label = id === "interested" ? "Interested" : "Not Interested";
        const inbound = {
            interactiveResponseMessage: {
                body: { text: label },
                nativeFlowResponseMessage: {
                    name: "quick_reply",
                    paramsJson: JSON.stringify({ id }),
                    version: 2,
                },
            },
        };
        check(`tap on "${label}" flattens to button_id = ${id}`, extractButtonReply(inbound)?.id === id, extractButtonReply(inbound));

        // The same tap, wrapped the way WhatsApp wraps a reply that came from
        // one of the sender's other devices.
        const wrapped = { deviceSentMessage: { destinationJid: JID, message: inbound } };
        check("  ...and still does inside deviceSentMessage", extractButtonReply(wrapped)?.id === id);
    }

    // A URL button has no reply id; it must translate without inventing one.
    const url = toHelperButtons([{ type: "URL", id: "", title: "Open", url: "https://example.com" }]);
    check("URL button becomes cta_url", (url[0] as any).name === "cta_url", url[0]);

    console.log(
        failures === 0
            ? "\nAll offline checks passed. This does NOT mean buttons render.\nNext: npx tsx qr-button-test.ts <recipient> 09_helper_engine"
            : `\n${failures} check(s) failed -- do not run the handset test until these pass.`,
    );
    process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
