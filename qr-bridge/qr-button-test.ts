/**
 * qr-button-test -- does a QR-linked WhatsApp session render native buttons?
 *
 * Usage (from qr-bridge/):
 *   npx tsx qr-button-test.ts <recipient> 10_buttons_msg_rich_biz  # THE TEST: one message
 *   npx tsx qr-button-test.ts <recipient> --replies                # then watch for taps
 *   npx tsx qr-button-test.ts <recipient> --all                    # whole matrix (14 sends!)
 *
 * Run ONE row. The bare form now refuses rather than relaying every variant,
 * because that puts fourteen messages -- most of them already measured as
 * unrenderable -- into a real customer's chat to answer a question one row
 * answers.
 *
 * ROW ORDER MATTERS. Rows 01-09 are settled (config.ts holds the ledger): the
 * server refuses a declared `native_flow name="quick_reply"` with 405, and no
 * consumer client renders an undeclared interactiveMessage. Rows 10-14 are the
 * open ones -- they send the biz node as a 2026 client emits it, with the three
 * attributes and the quality_control child that every 2025-era helper omits,
 * and they vary the payload underneath it. Start at 10.
 *
 * `--replies` sends NOTHING. It attaches to the live socket and reports button
 * taps as they arrive, so the tap half of the test can be repeated as often as
 * needed without re-sending the message being tapped.
 *
 * WHY THIS DRIVES THE RUNNING BRIDGE INSTEAD OF CONNECTING ITSELF
 *
 * The obvious shape for this script is "open a socket with the existing creds
 * and send". That is exactly the thing that must not happen here. Two sockets
 * over one auth directory each hold a divergent view of a contact's Signal
 * ratchet and race their writes to it; this project has already lost a session
 * that way (permanent "Waiting for this message" for the affected contact, 420x
 * Bad MAC in 48h, unrecoverable without deleting the session record). The live
 * bridge already holds the only socket that may exist for this session, so the
 * script asks it to run the matrix and prints what came back.
 *
 * WHAT COUNTS AS SUCCESS
 *
 * Not a message id, and not a server ack. Both are returned for messages that
 * never reach the handset -- that is the whole reason this rig exists. Success
 * is a human looking at the recipient's phone and seeing two tappable buttons,
 * then tapping one and this script reporting the id that came back.
 */
const API = process.env.QR_BRIDGE_URL ?? "http://127.0.0.1:4001";
const KEY = process.env.QR_PROVIDER_API_KEY ?? "";
const SESSION = process.env.QR_BRIDGE_SESSION_ID ?? "";

const BODY = "Are you interested in image ??";
const BUTTONS = [
    { id: "interested", title: "Interested" },
    { id: "not_interested", title: "Not Interested" },
];

async function call(path: string, init?: RequestInit) {
    const res = await fetch(`${API}${path}`, {
        ...init,
        headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${KEY}`,
            ...(init?.headers ?? {}),
        },
    });
    const text = await res.text();
    let json: any;
    try {
        json = JSON.parse(text);
    } catch {
        json = { raw: text };
    }
    return { status: res.status, json };
}

function line(char = "-") {
    console.log(char.repeat(78));
}

/** One row of the matrix, rendered so the wire facts are readable at a glance. */
function renderResult(r: any) {
    line();
    console.log(`VARIANT   ${r.variant}`);
    console.log(`WHY       ${r.hypothesis}`);
    console.log(`MSG ID    ${r.messageId ?? "(none -- relay threw)"}`);
    if (r.relayError) console.log(`RELAY ERR ${r.relayError}`);

    const nodes = r.additionalNodes;
    console.log(`STANZA    ${nodes ? JSON.stringify(nodes) : "(no additional nodes)"}`);

    if (r.ack) {
        const err = r.ackError ? `  ERROR=${r.ackError}` : "";
        console.log(`ACK       tag=${r.ack.tag} attrs=${JSON.stringify(r.ack.attrs)}${err}`);
    } else {
        console.log("ACK       (none seen in the observation window)");
    }

    if (r.receipts?.length) {
        for (const rc of r.receipts) {
            console.log(`RECEIPT   type=${rc.attrs.type ?? "delivery"} from=${rc.attrs.from ?? "?"}`);
        }
    } else {
        console.log("RECEIPT   (none -- not delivered within the window)");
    }

    for (const f of r.otherFrames ?? []) {
        console.log(`OTHER     tag=${f.tag} attrs=${JSON.stringify(f.attrs)}`);
    }

    // The single most useful line: what the recipient's client was actually given.
    // Every envelope the matrix can send gets a line here -- a row whose payload
    // is not summarised reads as "nothing was sent", which is the one wrong
    // conclusion this rig exists to prevent.
    const inner = r.wireMessage?.viewOnceMessage?.message ?? r.wireMessage;
    const tpl = inner?.templateMessage;
    const flow =
        inner?.interactiveMessage?.nativeFlowMessage ??
        tpl?.interactiveMessageTemplate?.nativeFlowMessage;

    if (flow) {
        const names = (flow.buttons ?? []).map((b: any) => b.name).join(", ");
        const wrapper = tpl ? "templateMessage.interactiveMessageTemplate" : "interactiveMessage";
        console.log(
            `PAYLOAD   ${wrapper} nativeFlowMessage buttons=[${names}] params=${flow.messageParamsJson ?? "(unset)"} v=${flow.messageVersion ?? "(unset)"}${tpl ? ` templateId=${tpl.templateId ?? "(unset)"}` : ""}`,
        );
    } else if (inner?.buttonsMessage) {
        const bm = inner.buttonsMessage;
        const shown = (bm.buttons ?? [])
            .map((b: any) =>
                b.nativeFlowInfo
                    ? `${b.nativeFlowInfo.name}:${b.nativeFlowInfo.paramsJson}`
                    : `${b.type}:${b.buttonId}`,
            )
            .join(", ");
        console.log(`PAYLOAD   buttonsMessage headerType=${bm.headerType} buttons=[${shown}]`);
    } else if (tpl?.hydratedFourRowTemplate) {
        const h = tpl.hydratedFourRowTemplate;
        const shown = (h.hydratedButtons ?? [])
            .map((b: any) => b.quickReplyButton?.id ?? b.urlButton?.url ?? "(other)")
            .join(", ");
        console.log(
            `PAYLOAD   templateMessage.hydratedFourRowTemplate templateId=${h.templateId ?? "(unset)"} buttons=[${shown}]`,
        );
    } else if (inner?.listMessage) {
        const rows = (inner.listMessage.sections ?? []).flatMap((s: any) =>
            (s.rows ?? []).map((row: any) => row.rowId),
        );
        console.log(`PAYLOAD   listMessage rows=[${rows.join(", ")}]`);
    } else if (inner?.conversation !== undefined) {
        console.log(`PAYLOAD   conversation (plain text control)`);
    }
}

async function main() {
    const to = process.argv[2];
    if (!to) {
        console.error("usage: npx tsx qr-button-test.ts <recipient> [variantId|--replies]");
        process.exit(2);
    }
    if (!KEY || !SESSION) {
        console.error("Set QR_PROVIDER_API_KEY and QR_BRIDGE_SESSION_ID (see docker exec ... env / data dir).");
        process.exit(2);
    }

    const session = await call(`/sessions/${SESSION}`);
    if (session.status !== 200 || session.json.status !== "CONNECTED") {
        console.error(`Session is not connected: ${JSON.stringify(session.json)}`);
        process.exit(1);
    }
    console.log(`Session ${SESSION} CONNECTED as ${session.json.phone_number} (${session.json.display_name})`);
    console.log(`Recipient: ${to}`);

    const arg = process.argv[3];

    // Listen-only. Deliberately a separate path with its own early return:
    // falling through to the matrix would re-send nine messages to answer
    // "did they tap yet?".
    if (arg === "--replies") {
        const seconds = Number(process.argv[4] ?? 60);
        line("=");
        console.log(`WATCHING FOR BUTTON TAPS for ${seconds}s -- nothing is being sent.`);
        console.log("Tap a button on the handset now.");
        line("=");
        const watch = await call(`/diagnostics/button-lab/${SESSION}`, {
            method: "POST",
            body: JSON.stringify({ to, observe_only: true, observe_ms: seconds * 1000 }),
        });
        if (watch.status !== 200) {
            console.error(`Watch failed (${watch.status}): ${JSON.stringify(watch.json)}`);
            process.exit(1);
        }
        const replies = watch.json.buttonReplies ?? [];
        if (!replies.length) {
            console.log("\nNo button replies seen in the window.");
            console.log("A tap that produces nothing here is a FAILED test, even if the");
            console.log("buttons looked right on the phone.");
            process.exit(1);
        }
        console.log("\nBUTTON REPLIES -- this is what Django's automation would match on:");
        for (const r of replies) {
            console.log(`  button_id = ${r.button_id}    (title: ${r.title}, from: ${r.from})`);
        }
        return;
    }

    const variants = arg && !arg.startsWith("--") ? [arg] : undefined;
    // The full matrix now has fourteen rows, most of them known-unrenderable.
    // Firing all of them at a real customer to answer a question that ONE row
    // answers is not a test, it is spam, so the sweep has to be asked for by
    // name. Passing a variant id -- the normal way to use this -- is unchanged.
    if (!variants && arg !== "--all") {
        console.error("\nRefusing to run the FULL matrix without --all: it relays every row");
        console.error("in LAB_VARIANTS to this recipient, and most of them draw nothing.");
        console.error("");
        console.error("Run ONE row. In order of what is actually worth testing today:");
        console.error(`  npx tsx qr-button-test.ts ${to} 10_buttons_msg_rich_biz    # start here`);
        console.error(`  npx tsx qr-button-test.ts ${to} 11_interactive_rich_biz`);
        console.error(`  npx tsx qr-button-test.ts ${to} 13_interactive_as_template`);
        console.error(`  npx tsx qr-button-test.ts ${to} 12_buttons_msg_native_flow`);
        console.error(`  npx tsx qr-button-test.ts ${to} 14_hydrated_template       # expect iOS/Web only`);
        console.error("");
        console.error("Then watch for the tap:");
        console.error(`  npx tsx qr-button-test.ts ${to} --replies 60`);
        console.error("");
        console.error("Rows 01-09 are already measured -- see the ledger in config.ts.");
        console.error("If you really want the sweep, pass --all.");
        process.exit(2);
    }
    if (!variants) {
        console.log("\nNOTE: running the FULL matrix -- this relays EVERY row to this");
        console.log("recipient, most of which are already known to draw nothing.");
    }

    line("=");
    console.log("RUNNING NATIVE-FLOW MATRIX");
    console.log("A message id is not success. A server ack is not success.");
    console.log("Success is two tappable buttons on the handset, and a tap coming back.");
    line("=");

    const { status, json } = await call(`/diagnostics/button-lab/${SESSION}`, {
        method: "POST",
        body: JSON.stringify({
            to,
            body: BODY,
            buttons: BUTTONS,
            variants,
            observe_ms: 10000,
        }),
    });

    if (status !== 200) {
        console.error(`Diagnostic failed (${status}): ${JSON.stringify(json)}`);
        process.exit(1);
    }

    if (json.controlText) {
        console.log("\nCONTROL SEND (plain text -- proves the Signal session works at all)");
        renderResult(json.controlText);
    }

    console.log("\nMATRIX");
    for (const r of json.results ?? []) renderResult(r);

    line("=");
    console.log("NOW CHECK THE HANDSET.");
    console.log("Success is TWO TAPPABLE BUTTONS drawn on the phone. A message id above,");
    console.log("a clean ack and a delivery receipt are all returned for messages that");
    console.log("render nothing -- none of them is evidence.");
    console.log("");
    console.log("Then tap each button and run:");
    console.log(`  npx tsx qr-button-test.ts ${to} --replies 60`);
    console.log("Expected: button_id = interested, then button_id = not_interested.");
    line("=");

    if (json.buttonReplies?.length) {
        console.log("\nBUTTON REPLIES SEEN DURING THE RUN:");
        console.log(JSON.stringify(json.buttonReplies, null, 2));
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
