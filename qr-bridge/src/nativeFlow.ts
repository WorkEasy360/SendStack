/**
 * The native-flow button engine, and the only place that knows it is a
 * third-party package.
 *
 * WHY A SECOND ENGINE RATHER THAN MORE PAYLOAD SHAPES
 *
 * `buttonLab.ts` measured eight hand-built envelopes against a real handset and
 * every one of them either drew nothing or was refused (see `config.ts`). The
 * conclusion recorded there -- that the interactiveMessage *content* is fine and
 * the missing piece is what the outgoing *stanza* declares -- is what this module
 * acts on. It does not invent another payload. It hands the job to
 * `baileys_helper`, a wrapper whose whole purpose is deriving the stanza nodes
 * the official client emits alongside an interactive message, and which is a
 * reimplementation of the same recipe carried by the maintained
 * `@itsukichan/baileys` fork (both were read before choosing; they agree node
 * for node).
 *
 * WHY THE WRAPPER AND NOT THE FORK
 *
 * `@itsukichan/baileys` 7.3.2 does this correctly too, but it is a whole engine
 * built on Baileys 7.x, and swapping it in would replace the code that owns QR
 * pairing, the auth-state format and every credential this deployment already
 * holds. `baileys_helper` needs none of that: it borrows the socket we already
 * have, calls the `generateWAMessageFromContent` and `relayMessage` of our
 * installed `@whiskeysockets/baileys` 6.7.24, and only adds the stanza nodes.
 * So QR login, stored credentials, text sends and media sends are not merely
 * expected to keep working -- no code on any of those paths changes at all.
 *
 * WHAT IT ACTUALLY ADDS, versus the closest row the lab already ran
 * (`07_biz_mixed_flow`, which acked cleanly and drew nothing):
 *
 *   - `native_flow` declared at `v="9"`, where the lab sent `v="2"`;
 *   - a `<bot biz_bot="1"/>` node on every private chat -- never sent before,
 *     and the single most likely reason a stanza that acked cleanly still drew
 *     nothing;
 *   - no `viewOnceMessage` wrapper -- the interactiveMessage travels bare;
 *   - `generateMessageIDV2`, which derives the id from the sender jid the way
 *     the official client does, rather than the random id `generateMessageID`
 *     returns.
 *
 * That is four changed variables from the nearest measured negative, which is
 * why this is worth a handset test rather than a rerun of one.
 */
import { createRequire } from "node:module";
import type { BinaryNode, WASocket } from "@whiskeysockets/baileys";
import type pino from "pino";

import type { InteractiveButton } from "./providers/base.js";

/** `baileys_helper` is CommonJS; this service is ESM. */
const require_ = createRequire(import.meta.url);

/** One button in the shape `baileys_helper` accepts, before it normalises. */
type HelperButton =
    | { id: string; text: string }
    | { name: string; buttonParamsJson: string };

/**
 * What the engine resolves to: the same WAMessage `sendMessage()` returns.
 *
 * `key.id` is what delivery receipts arrive against, and `message` is the
 * relayed content -- the lab re-derives the engine's own stanza nodes from it
 * rather than restating them (see nativeFlowStanzaNodes).
 */
export interface SentMessage {
    key?: { id?: string | null };
    message?: unknown;
}

interface BaileysHelper {
    /** Builds the interactiveMessage, derives the stanza nodes, relays. Resolves to the WAMessage. */
    sendButtons(
        sock: WASocket,
        jid: string,
        data: { text: string; footer?: string; buttons: HelperButton[] },
        options?: Record<string, unknown>,
    ): Promise<SentMessage>;
    /** The stanza node derivation on its own, so buttonLab.ts can measure the real thing. */
    getButtonArgs(normalisedContent: unknown): BinaryNode;
}

const helper: BaileysHelper = require_("baileys_helper");

/**
 * Whether the engine will be able to find Baileys when a send actually happens.
 *
 * The engine resolves Baileys with `require("@whiskeysockets/baileys")` and
 * Baileys 6.7.24 is ESM, so that call only works on a Node with `require(esm)`
 * -- 20.19 and up. Below that it throws, the engine catches it while trying its
 * three candidate package names, and the failure surfaces as
 * "Missing baileys internals" at SEND time, on a real customer's message,
 * having looked perfectly healthy at startup.
 *
 * So it is resolved once here, at import, where the answer is free and nothing
 * is at stake. This deliberately does NOT throw: text and media sends do not go
 * near the engine, and refusing to start the whole bridge over a feature that
 * is off by default would turn a degraded button into an outage.
 */
export function nativeFlowEngineAvailable(): { ok: boolean; reason?: string } {
    try {
        const baileys: any = require_("@whiskeysockets/baileys");
        return baileys?.generateWAMessageFromContent && baileys?.normalizeMessageContent
            ? { ok: true }
            : { ok: false, reason: "Baileys loaded but is missing generateWAMessageFromContent/normalizeMessageContent." };
    } catch (err) {
        return {
            ok: false,
            reason: `Cannot require() Baileys on Node ${process.version} (needs >= 20.19 for require(esm)): ${(err as Error).message}`,
        };
    }
}

/**
 * `baileys_helper` reports through `console`, and this service logs through
 * pino. Left alone, a native-flow send prints an unstructured line to stdout
 * that no log collector can parse, on every send.
 *
 * The redirect is installed once, at startup, rather than wrapped around each
 * call: swapping globals per-call races with itself the moment two sends
 * overlap, and this bridge sends concurrently. Nothing else in `src/` writes to
 * `console` (everything takes a pino child), so there is nothing else to catch.
 */
export function captureHelperConsole(logger: pino.Logger): void {
    const sink = (...args: unknown[]) => logger.debug({ helper: args }, "baileys_helper");
    console.log = sink;
    console.warn = sink;
}

/**
 * Our buttons in the shape the engine takes.
 *
 * A reply button goes over as the engine's legacy `{ id, text }`, which it wraps
 * into a `quick_reply` whose `buttonParamsJson.id` is *our* id verbatim -- that
 * is what comes back on a tap and what Django's automation matches on, so it
 * must survive this translation unchanged. A URL button has no reply id and is
 * handed over already in native form.
 */
export function toHelperButtons(buttons: InteractiveButton[]): HelperButton[] {
    return buttons.map((button) =>
        button.type === "URL"
            ? {
                  name: "cta_url",
                  buttonParamsJson: JSON.stringify({
                      display_text: button.title,
                      url: button.url,
                      merchant_url: button.url,
                  }),
              }
            : { id: button.id, text: button.title },
    );
}

/**
 * Send body text with native tappable buttons under it.
 *
 * Returns the WAMessage, whose `key.id` delivery receipts arrive against --
 * the same shape `sendMessage()` returns, so the caller cannot tell which
 * engine ran.
 */
export function sendNativeFlowButtons(
    sock: WASocket,
    jid: string,
    interactive: { body: string; footer?: string; buttons: InteractiveButton[] },
) {
    return helper.sendButtons(sock, jid, {
        text: interactive.body,
        ...(interactive.footer ? { footer: interactive.footer } : {}),
        buttons: toHelperButtons(interactive.buttons),
    });
}

/**
 * The engine's own stanza-node derivation, exposed for the lab.
 *
 * buttonLab.ts uses this rather than rebuilding the node tree, so what the lab
 * measures is exactly what the send path will emit. A hand-copied node in the
 * lab would make a passing measurement prove nothing about production.
 */
export function nativeFlowStanzaNodes(content: unknown, isPrivateChat: boolean): BinaryNode[] {
    const nodes = [helper.getButtonArgs(content)];
    if (isPrivateChat) nodes.push({ tag: "bot", attrs: { biz_bot: "1" } });
    return nodes;
}
