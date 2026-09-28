/**
 * A measurement rig for native-flow interactive messages.
 *
 * WHY THIS EXISTS SEPARATELY FROM baileysProvider.ts
 *
 * `sendInteractiveButtons()` sends one shape and reports whether Baileys threw.
 * That is the wrong instrument for the question "does a QR-linked device render
 * tappable buttons", because every shape returns a message id whether or not the
 * server accepted it and whether or not the handset drew anything. The prior
 * five-shape comparison recorded in `config.ts` was run with that instrument and
 * two of its five rows turned out to measure nothing at all (see
 * `SENDMESSAGE_DROPS_LEGACY_BUTTONS` below).
 *
 * So this module measures four things the send path cannot see:
 *
 *   1. the exact protobuf we hand to `relayMessage`, dumped verbatim;
 *   2. the exact extra binary nodes appended to the outgoing stanza;
 *   3. every inbound frame the server sends back that references our message id
 *      -- the server ACK (and its `error` attribute, if any) and the delivery
 *      receipt, read straight off the websocket rather than inferred;
 *   4. any interactive response that comes back when a button is tapped.
 *
 * It deliberately drives the session's EXISTING socket. Opening a second socket
 * against the same auth directory is the precise condition that corrupted this
 * project's Signal sessions before (two sockets racing one ratchet), so the lab
 * borrows the live one instead of connecting on its own.
 */
import { randomBytes } from "node:crypto";

import {
    proto,
    generateWAMessageFromContent,
    normalizeMessageContent,
    isJidGroup,
    type WASocket,
    type BinaryNode,
} from "@whiskeysockets/baileys";

import { sendNativeFlowButtons, nativeFlowStanzaNodes, type SentMessage } from "./nativeFlow.js";
import { extractButtonReply } from "./buttonReply.js";
import type { InteractiveButton } from "./providers/base.js";

/**
 * Baileys 6.7.24 has no `buttons` / `templateButtons` support at all.
 *
 * `generateWAMessageContent()` in this version branches on `'text' in message`
 * and assigns `extendedTextMessage`; it never reads a `buttons` or
 * `templateButtons` key (grep the installed `lib/Utils/messages.js` -- the only
 * hits for "buttons" are the *response* message and a link-preview helper).
 * Unknown keys are not rejected, they are ignored.
 *
 * Consequence: the bridge's `buttons_legacy` and `template` formats put a plain
 * text message on the wire. Their measured result -- "arrived, rendered as
 * plain text" -- is therefore a fact about Baileys, not about WhatsApp. Neither
 * row is evidence that WhatsApp flattened anything, because no button structure
 * was ever sent. They are excluded from this matrix for that reason.
 */
export const SENDMESSAGE_DROPS_LEGACY_BUTTONS = true;

export interface LabButton {
    id: string;
    title: string;
}

/**
 * The extra stanza nodes a native-flow message may need.
 *
 * This is the part the previous investigation never varied. `relayMessage`
 * builds a `<message type="text">` stanza for an interactive message --
 * `getMessageType()` in `lib/Socket/messages-send.js` returns "text" for
 * everything that is not a poll, and `getMediaType()` has no branch for
 * `interactiveMessage` either -- so nothing in the stanza envelope declares
 * that the ciphertext contains interactive content. `additionalNodes` is the
 * only hook Baileys gives us to say so, and both `biz` and `interactive` are
 * real tokens in WhatsApp's own binary dictionary
 * (`lib/WABinary/constants.js`), which is what makes this worth measuring
 * rather than a guess.
 */
function bizNode(flowName: string): BinaryNode {
    return {
        tag: "biz",
        attrs: {},
        content: [
            {
                tag: "interactive",
                attrs: { type: "native_flow", v: "1" },
                content: [{ tag: "native_flow", attrs: { name: flowName, v: "2" } }],
            },
        ],
    };
}

/**
 * The same node as `bizNode`, as a WhatsApp client actually emits it in 2026.
 *
 * WHERE THIS COMES FROM. Rows 04-07 and the `baileys_helper` engine in row 09
 * all send `<biz>` with NO attributes and one child. That shape is what every
 * 2025-era helper package copied from each other. Reading the June-2026 build
 * of a fork that is still tracking the protocol (`@itsliaaa/baileys`
 * 0.3.18-final, `lib/WABinary/generic-utils.js`, `getBizBinaryNode`) shows the
 * node grew three attributes and a second child since then:
 *
 *   <biz actual_actors="2" host_storage="2" privacy_mode_ts="<unix secs>">
 *     <interactive type="native_flow" v="1"><native_flow v="9" name="mixed"/></interactive>
 *     <quality_control decision_id="<20 random bytes, hex>" source_type="third_party">
 *       <decision_source value="df"/>
 *     </quality_control>
 *   </biz>
 *
 * `source_type="third_party"` is the interesting one: this is the envelope a
 * *non-first-party* sender is expected to use, which is exactly our position.
 * A bare `<biz/>` is not a smaller version of this node -- it is a node from an
 * older protocol revision, which is a live explanation for row 07's clean ack
 * and blank handset.
 *
 * `decision_id` is regenerated per call on purpose; the fork derives it from
 * `randomBytes(20)` per message, and reusing one across sends is the kind of
 * thing a server-side dedupe would notice.
 */
function richBizNode(): BinaryNode {
    return {
        tag: "biz",
        attrs: {
            actual_actors: "2",
            host_storage: "2",
            privacy_mode_ts: `${(Date.now() / 1000) | 0}`,
        },
        content: [
            {
                tag: "interactive",
                attrs: { type: "native_flow", v: "1" },
                content: [{ tag: "native_flow", attrs: { v: "9", name: "mixed" } }],
            },
            {
                tag: "quality_control",
                attrs: {
                    decision_id: randomBytes(20).toString("hex"),
                    source_type: "third_party",
                },
                content: [{ tag: "decision_source", attrs: { value: "df" } }],
            },
        ],
    };
}

/**
 * A lab button in the vocabulary the send path speaks.
 *
 * The lab only ever tests reply buttons -- a URL button has no id to come back,
 * so it cannot answer the question this rig exists to answer.
 */
function toReplyButton(button: LabButton): InteractiveButton {
    return { type: "REPLY", id: button.id, title: button.title, url: "" };
}

function quickReplyButtons(buttons: LabButton[]) {
    return buttons.map((b) => ({
        name: "quick_reply",
        buttonParamsJson: JSON.stringify({ display_text: b.title, id: b.id }),
    }));
}

function buildInteractive(
    body: string,
    footer: string | undefined,
    buttons: LabButton[],
    extra: { messageParamsJson?: string; messageVersion?: number; header?: unknown },
) {
    const { messageParamsJson, messageVersion, header } = extra;
    return proto.Message.InteractiveMessage.create({
        ...(header ? { header } : {}),
        body: proto.Message.InteractiveMessage.Body.create({ text: body }),
        ...(footer ? { footer: proto.Message.InteractiveMessage.Footer.create({ text: footer }) } : {}),
        nativeFlowMessage: proto.Message.InteractiveMessage.NativeFlowMessage.create({
            buttons: quickReplyButtons(buttons),
            ...(messageParamsJson !== undefined ? { messageParamsJson } : {}),
            ...(messageVersion !== undefined ? { messageVersion } : {}),
        }),
    } as any);
}

function emptyHeader() {
    return proto.Message.InteractiveMessage.Header.create({
        title: "",
        subtitle: "",
        hasMediaAttachment: false,
    });
}

/** The viewOnceMessage envelope an interactive message has historically needed. */
function viewOnce(interactiveMessage: unknown) {
    return {
        viewOnceMessage: {
            message: {
                messageContextInfo: { deviceListMetadata: {}, deviceListMetadataVersion: 2 },
                interactiveMessage,
            },
        },
    };
}

export interface LabVariant {
    id: string;
    /** What this row is actually testing, so the report explains itself. */
    hypothesis: string;
    /**
     * Build a payload for the lab to relay itself. Every hand-built row uses
     * this. Exactly one of `build` or `send` is present.
     */
    build?(
        body: string,
        footer: string | undefined,
        buttons: LabButton[],
    ): { content: unknown; additionalNodes?: BinaryNode[] };
    /**
     * Send through a real production engine instead.
     *
     * A row that reimplements what the send path does can pass while the send
     * path still fails -- the two drift the moment either is edited. A row that
     * calls the shipping function cannot: if it renders on the handset, the
     * format it names is ready to enable, with nothing left to port.
     */
    send?(
        sock: WASocket,
        jid: string,
        body: string,
        footer: string | undefined,
        buttons: LabButton[],
    ): Promise<SentMessage>;
}

/**
 * The matrix.
 *
 * Ordered so each row changes exactly ONE thing from a row above it. That is
 * what makes a negative result informative: if 04 renders and 02 does not, the
 * biz node is the cause and nothing else moved.
 */
export const LAB_VARIANTS: LabVariant[] = [
    {
        id: "01_interactive_direct",
        hypothesis:
            "interactiveMessage, no wrapper, no stanza declaration. The bridge's current `interactive_direct`; the control for rows 02-07.",
        build: (body, footer, buttons) => ({
            content: { interactiveMessage: buildInteractive(body, footer, buttons, {}) },
        }),
    },
    {
        id: "02_viewonce_wrapper",
        hypothesis:
            "The shape most guidance describes, and the bridge's current `native_flow`: viewOnceMessage -> messageContextInfo + interactiveMessage.",
        build: (body, footer, buttons) => ({
            content: viewOnce(buildInteractive(body, footer, buttons, {})),
        }),
    },
    {
        id: "03_params_json",
        hypothesis:
            "Row 02 plus nativeFlowMessage.messageParamsJson/messageVersion -- the bridge's `native_flow_v1`. Isolates whether the omitted proto fields matter.",
        build: (body, footer, buttons) => ({
            content: viewOnce(
                buildInteractive(body, footer, buttons, { messageParamsJson: "{}", messageVersion: 1 }),
            ),
        }),
    },
    {
        id: "04_biz_node",
        hypothesis:
            "Row 02 plus a biz/interactive/native_flow node on the stanza. THE PRIMARY HYPOTHESIS: the server discards interactive content the stanza envelope never declares, which would explain a message id, a clean ack, and nothing on the handset.",
        build: (body, footer, buttons) => ({
            content: viewOnce(buildInteractive(body, footer, buttons, {})),
            additionalNodes: [bizNode("quick_reply")],
        }),
    },
    {
        id: "05_biz_node_direct",
        hypothesis:
            "Row 04 without the viewOnce wrapper. Separates 'needs the wrapper' from 'needs the stanza declaration'.",
        build: (body, footer, buttons) => ({
            content: { interactiveMessage: buildInteractive(body, footer, buttons, {}) },
            additionalNodes: [bizNode("quick_reply")],
        }),
    },
    {
        id: "06_biz_node_header_params",
        hypothesis:
            "Row 04 plus an empty Header (hasMediaAttachment:false) and messageParamsJson -- the maximal well-formed native flow, in case the client needs a header before it will draw a button row.",
        build: (body, footer, buttons) => ({
            content: viewOnce(
                buildInteractive(body, footer, buttons, {
                    messageParamsJson: "{}",
                    messageVersion: 1,
                    header: emptyHeader(),
                }),
            ),
            additionalNodes: [bizNode("quick_reply")],
        }),
    },
    {
        id: "07_biz_mixed_flow",
        hypothesis:
            "Row 06 with the flow declared as `mixed` rather than `quick_reply`, the other name forks use. The last free parameter in the biz node.",
        build: (body, footer, buttons) => ({
            content: viewOnce(
                buildInteractive(body, footer, buttons, {
                    messageParamsJson: "{}",
                    messageVersion: 1,
                    header: emptyHeader(),
                }),
            ),
            additionalNodes: [bizNode("mixed")],
        }),
    },
    {
        id: "08_list_message",
        hypothesis:
            "Not a button at all: a hand-relayed listMessage. Baileys 6.7.24 dropped `sections` from sendMessage() the same way it dropped `buttons`, but the proto still defines ListMessage, so it can be relayed directly. A native list is the only other tappable envelope, and if it renders it beats the numbered-text fallback outright.",
        build: (body, footer, buttons) => ({
            content: {
                listMessage: proto.Message.ListMessage.create({
                    title: body,
                    description: body,
                    buttonText: "Select",
                    listType: proto.Message.ListMessage.ListType.SINGLE_SELECT,
                    sections: [
                        {
                            title: "Choose one",
                            rows: buttons.map((b) => ({ title: b.title, rowId: b.id, description: "" })),
                        },
                    ],
                    ...(footer ? { footerText: footer } : {}),
                } as any),
            },
        }),
    },
    {
        id: "09_helper_engine",
        hypothesis:
            "Not another hand-built payload: the `baileys_helper` engine's own send path, called exactly as production would call it (format `native_flow_helper`). It differs from row 07 -- the closest clean-ack-but-blank result -- in four places at once: native_flow v=\"9\" not v=\"2\", a <bot biz_bot=\"1\"/> node this matrix has never sent, no viewOnce wrapper, and a generateMessageIDV2 message id. Rows 01-08 establish that no payload reshaping works, so this row asks the only remaining question: whether the stanza declaration is what was missing. If this renders, nothing needs porting -- set QR_BRIDGE_INTERACTIVE_FORMAT=native_flow_helper.",
        send: (sock, jid, body, footer, buttons) =>
            sendNativeFlowButtons(sock, jid, { body, footer, buttons: buttons.map(toReplyButton) }),
    },

    // ---------------------------------------------------------------------
    // Rows 10-14: the 2026 recipe.
    //
    // Everything above (including row 09's engine) sends a bare `<biz/>` node,
    // which is the 2025 shape every helper package copied from every other.
    // These rows send `richBizNode()` -- the node as a fork that is still
    // tracking the protocol emits it in June 2026 -- and vary only the payload
    // underneath it. If none of them draws a button, the biz declaration is
    // exhausted and the question is settled for good.
    // ---------------------------------------------------------------------

    {
        id: "10_buttons_msg_rich_biz",
        hypothesis:
            "THE PRIMARY 2026 HYPOTHESIS. A legacy `buttonsMessage` -- hand-relayed, because Baileys 6.7.24's sendMessage() drops the `buttons` key before it reaches the wire -- under the full 2026 biz node. This is byte-for-byte what @itsliaaa/baileys 0.3.18-final (June 2026) puts on the wire for sendMessage(jid, { text, buttons: [{ id, text }] }): its `buttons` shortcut builds a ButtonsMessage, not an interactiveMessage, and shouldIncludeBizBinaryNode() then attaches the biz node. It also explains why the flow is declared `name=\"mixed\"` -- a ButtonsMessage may hold RESPONSE and NATIVE_FLOW buttons at once. Note this is NOT the row the memory calls 'never measured WhatsApp': that one never sent a button structure at all.",
        build: (body, footer, buttons) => ({
            content: {
                buttonsMessage: proto.Message.ButtonsMessage.create({
                    contentText: body,
                    ...(footer ? { footerText: footer } : {}),
                    headerType: proto.Message.ButtonsMessage.HeaderType.EMPTY,
                    buttons: buttons.map((b) => ({
                        buttonId: b.id,
                        buttonText: { displayText: b.title },
                        type: proto.Message.ButtonsMessage.Button.Type.RESPONSE,
                    })),
                } as any),
            },
            additionalNodes: [richBizNode()],
        }),
    },
    {
        id: "11_interactive_rich_biz",
        hypothesis:
            "Row 06's payload -- the maximal well-formed interactiveMessage, which is what production would send -- with the bare biz node swapped for the 2026 one. Exactly one variable moves from row 07 (clean ack, blank handset): the biz node's attributes and its quality_control child. If this renders and 07 did not, the answer is 'our stanza declaration was a protocol revision behind' and production needs a node change, not a payload change.",
        build: (body, footer, buttons) => ({
            content: {
                interactiveMessage: buildInteractive(body, footer, buttons, {
                    messageParamsJson: "{}",
                    messageVersion: 1,
                    header: emptyHeader(),
                }),
            },
            additionalNodes: [richBizNode()],
        }),
    },
    {
        id: "12_buttons_msg_native_flow",
        hypothesis:
            "Row 10 with the buttons carried as NATIVE_FLOW `quick_reply` entries (`nativeFlowInfo.paramsJson`) rather than legacy RESPONSE entries. Separates 'the client needs a ButtonsMessage envelope' from 'the client needs native-flow button bodies'. Taps on this shape come back as buttonsResponseMessage, which buttonReply.ts already flattens.",
        build: (body, footer, buttons) => ({
            content: {
                buttonsMessage: proto.Message.ButtonsMessage.create({
                    contentText: body,
                    ...(footer ? { footerText: footer } : {}),
                    headerType: proto.Message.ButtonsMessage.HeaderType.EMPTY,
                    buttons: buttons.map((b) => ({
                        type: proto.Message.ButtonsMessage.Button.Type.NATIVE_FLOW,
                        nativeFlowInfo: {
                            name: "quick_reply",
                            paramsJson: JSON.stringify({ display_text: b.title, id: b.id }),
                        },
                    })),
                } as any),
            },
            additionalNodes: [richBizNode()],
        }),
    },
    {
        id: "13_interactive_as_template",
        hypothesis:
            "The interactiveMessage from row 11, wrapped in `templateMessage.interactiveMessageTemplate` with a templateId. Added to the same fork on 02-02-26 with the note that a minimal templateId is what 'satisfies WhatsApp'. The hypothesis is that a consumer client renders a native-flow button row only when it arrives as a TEMPLATE -- which is how every Cloud API button message arrives -- and that the templateId is the thing our sends have always lacked.",
        build: (body, footer, buttons) => ({
            content: {
                templateMessage: proto.Message.TemplateMessage.create({
                    interactiveMessageTemplate: buildInteractive(body, footer, buttons, {
                        messageParamsJson: "{}",
                        messageVersion: 1,
                        header: emptyHeader(),
                    }),
                    templateId: `sendstack-${Date.now()}`,
                } as any),
            },
            additionalNodes: [richBizNode()],
        }),
    },
    {
        id: "14_hydrated_template",
        hypothesis:
            "A hydratedFourRowTemplate carrying quickReplyButtons -- the oldest button envelope of all, but hand-relayed with a templateId and the 2026 biz node, neither of which the 2021-era examples had. Included because the same fork carries a dated maintainer note (03-02-26) that this one renders on WhatsApp Web/Desktop and iOS and NOT on Android outside newsletters. So a partial result here is the expected result, and it is worth knowing: a row that draws buttons on iOS and nothing on Android is a different business decision from one that draws nothing anywhere. Taps come back as templateButtonReplyMessage.",
        build: (body, footer, buttons) => {
            const hydrated = {
                hydratedContentText: body,
                ...(footer ? { hydratedFooterText: footer } : {}),
                hydratedButtons: buttons.map((b, index) => ({
                    index,
                    quickReplyButton: { displayText: b.title, id: b.id },
                })),
                templateId: `sendstack-${Date.now()}`,
            };
            return {
                content: {
                    templateMessage: proto.Message.TemplateMessage.create({
                        hydratedFourRowTemplate: hydrated,
                        hydratedTemplate: hydrated,
                    } as any),
                },
                additionalNodes: [richBizNode()],
            };
        },
    },
];

/** One inbound stanza that referenced our message, captured off the websocket. */
export interface CapturedFrame {
    at: number;
    tag: string;
    attrs: Record<string, string>;
    /** Child tags only -- enough to tell a receipt from an ack from an error. */
    children: string[];
}

export interface VariantResult {
    variant: string;
    hypothesis: string;
    messageId: string | null;
    /** The protobuf actually handed to relayMessage. */
    wireMessage: unknown;
    /** The extra stanza nodes actually appended, or null when none were. */
    additionalNodes: BinaryNode[] | null;
    relayError: string | null;
    /** Server ACK: proof of acceptance, and its `error` attr if it was rejected. */
    ack: CapturedFrame | null;
    ackError: string | null;
    /** Delivery/read receipts that named this message id. */
    receipts: CapturedFrame[];
    /** Everything else that named this id -- retries, notifications, errors. */
    otherFrames: CapturedFrame[];
}

export interface ButtonLabInput {
    to: string;
    body: string;
    footer?: string;
    buttons: LabButton[];
    /** Variant ids to run; omitted means the whole matrix. */
    variants?: string[];
    /** How long to watch the socket after each relay, for ack and receipt. */
    observeMs?: number;
    /** Send a plain text message first, to prove the Signal session is healthy. */
    includeControlText?: boolean;
    /**
     * Watch for taps without sending anything.
     *
     * The verdict on a button message is not the send, it is the tap that comes
     * back afterwards -- and the tap happens minutes later, whenever the human
     * holding the phone gets to it, long after the sending run has returned.
     * Without this flag the only way to listen again is to run the matrix
     * again, which re-sends every variant to a real customer to answer a
     * question about a message they already have. So this mode attaches the
     * same listeners the sending run uses and relays nothing at all.
     */
    observeOnly?: boolean;
}

export interface ButtonLabReport {
    to: string;
    jid: string;
    startedAt: string;
    /** The control send. If this never reaches the handset, nothing below means anything. */
    controlText: VariantResult | null;
    results: VariantResult[];
    /** Interactive/button responses seen while the lab was running. */
    buttonReplies: unknown[];
    notes: string[];
}

const DEFAULT_OBSERVE_MS = 12_000;

function summarise(node: BinaryNode): CapturedFrame {
    return {
        at: Date.now(),
        tag: node.tag,
        attrs: (node.attrs ?? {}) as Record<string, string>,
        children: Array.isArray(node.content)
            ? (node.content as BinaryNode[]).map((c) => c?.tag).filter(Boolean)
            : [],
    };
}

/**
 * Run the matrix against one recipient on an already-connected socket.
 *
 * Each variant is relayed, then the websocket is watched for `observeMs` for
 * anything carrying that message id. Nothing here interprets the result as
 * success: only the handset can do that, so the report records what the wire
 * did and leaves the verdict to the person holding the phone.
 */
export async function runButtonLab(
    sock: WASocket,
    jid: string,
    input: ButtonLabInput,
): Promise<ButtonLabReport> {
    const observeMs = input.observeMs ?? DEFAULT_OBSERVE_MS;
    const wanted = input.variants?.length
        ? LAB_VARIANTS.filter((v) => input.variants!.includes(v.id))
        : LAB_VARIANTS;

    const report: ButtonLabReport = {
        to: input.to,
        jid,
        startedAt: new Date().toISOString(),
        controlText: null,
        results: [],
        buttonReplies: [],
        notes: [],
    };

    // Every inbound stanza, bucketed by the message id it references. Attached
    // once for the whole run so a late receipt still lands in the right row.
    const frames = new Map<string, CapturedFrame[]>();
    const onFrame = (node: BinaryNode) => {
        const id = node?.attrs?.id;
        if (!id) return;
        const list = frames.get(id) ?? [];
        list.push(summarise(node));
        frames.set(id, list);
    };
    (sock.ws as unknown as { on(e: string, f: (n: BinaryNode) => void): void }).on("frame", onFrame);

    // A tapped button arrives as an ordinary inbound message; capture the raw
    // interactive payloads so the report can show the id that came back.
    const onMessages = ({ messages }: { messages: any[] }) => {
        for (const m of messages ?? []) {
            if (m?.key?.fromMe) continue;
            // Flattened by the SAME function the production webhook path uses, so a
            // button_id printed here is a button_id Django would have received. The
            // raw payload rides along for the case where extraction returns null and
            // the shape needs looking at.
            const reply = extractButtonReply(m.message);
            if (!reply) continue;
            report.buttonReplies.push({
                from: m.key.remoteJid,
                at: new Date().toISOString(),
                button_id: reply.id,
                title: reply.title,
                payload: JSON.parse(JSON.stringify(m.message)),
            });
        }
    };
    sock.ev.on("messages.upsert", onMessages as any);

    try {
        if (input.observeOnly) {
            // Nothing is relayed; the listeners above are the whole point. The
            // window is the operator's to choose -- they are the one deciding
            // how long to hold the phone.
            report.notes.push(
                `Observe-only: no message was sent. Watched for taps for ${observeMs}ms.`,
            );
            await new Promise((r) => setTimeout(r, observeMs));
            return report;
        }

        if (input.includeControlText !== false) {
            report.controlText = await relayOne(
                sock,
                jid,
                "00_control_text",
                "Plain text on the same socket and the same Signal session. If this does not reach the handset, every row below is untestable -- the session is broken, not the envelope.",
                { conversation: `${input.body} (control: plain text)` },
                undefined,
                frames,
                observeMs,
            );
        }

        for (const variant of wanted) {
            if (variant.send) {
                report.results.push(await sendOne(sock, jid, variant, input, frames, observeMs));
                continue;
            }
            const built = variant.build!(input.body, input.footer, input.buttons);
            report.results.push(
                await relayOne(
                    sock,
                    jid,
                    variant.id,
                    variant.hypothesis,
                    built.content,
                    built.additionalNodes,
                    frames,
                    observeMs,
                ),
            );
        }
    } finally {
        (sock.ws as unknown as { off(e: string, f: (n: BinaryNode) => void): void }).off("frame", onFrame);
        sock.ev.off("messages.upsert", onMessages as any);
    }

    return report;
}

/**
 * Relay a hand-built payload, then watch the socket for what the server says
 * about it.
 */
async function relayOne(
    sock: WASocket,
    jid: string,
    variantId: string,
    hypothesis: string,
    content: unknown,
    additionalNodes: BinaryNode[] | undefined,
    frames: Map<string, CapturedFrame[]>,
    observeMs: number,
): Promise<VariantResult> {
    const result = blankResult(variantId, hypothesis, additionalNodes ?? null);

    try {
        const msg = generateWAMessageFromContent(jid, content as any, {
            userJid: sock.user?.id ?? "",
        });
        result.messageId = msg.key.id ?? null;
        // Dumped through protobuf's own JSON so the report shows what will be
        // serialised, not what we typed -- a field the proto drops disappears here too.
        result.wireMessage = JSON.parse(JSON.stringify(msg.message));

        await sock.relayMessage(jid, msg.message!, {
            messageId: msg.key.id!,
            ...(additionalNodes ? { additionalNodes } : {}),
        });
    } catch (err) {
        result.relayError = (err as Error).message;
        return result;
    }

    await observe(result, frames, observeMs);
    return result;
}

/**
 * Run a row that goes through a production engine, then watch the same way.
 *
 * The engine appends its stanza nodes deep inside its own call to
 * `relayMessage` and never hands them back, so they are RE-DERIVED here from
 * the message it returned, by calling the engine's own derivation on the
 * engine's own output. That is not a restatement of what we think it sent: the
 * derivation is a pure function of the relayed content, so feeding it the
 * content that was actually relayed reproduces the nodes that actually went
 * out. Reporting them matters because a blank handset with a clean ack is only
 * interpretable if the report shows what the stanza declared.
 *
 * If the re-derivation throws, the row still stands -- the nodes are commentary
 * on a send that already happened, and losing them must not lose the ack.
 */
async function sendOne(
    sock: WASocket,
    jid: string,
    variant: LabVariant,
    input: ButtonLabInput,
    frames: Map<string, CapturedFrame[]>,
    observeMs: number,
): Promise<VariantResult> {
    const result = blankResult(variant.id, variant.hypothesis, null);

    try {
        const msg = await variant.send!(sock, jid, input.body, input.footer, input.buttons);
        result.messageId = msg?.key?.id ?? null;
        if (msg?.message) {
            result.wireMessage = JSON.parse(JSON.stringify(msg.message));
            try {
                result.additionalNodes = nativeFlowStanzaNodes(
                    normalizeMessageContent(msg.message as any),
                    !isJidGroup(jid),
                );
            } catch {
                // Left null: the send is the measurement, the nodes are its footnote.
            }
        }
    } catch (err) {
        result.relayError = (err as Error).message;
        return result;
    }

    await observe(result, frames, observeMs);
    return result;
}

function blankResult(
    variant: string,
    hypothesis: string,
    additionalNodes: BinaryNode[] | null,
): VariantResult {
    return {
        variant,
        hypothesis,
        messageId: null,
        wireMessage: null,
        additionalNodes,
        relayError: null,
        ack: null,
        ackError: null,
        receipts: [],
        otherFrames: [],
    };
}

/**
 * Watch the websocket for anything naming this message id, and sort what
 * arrives into the ack, the receipts, and everything else.
 */
async function observe(
    result: VariantResult,
    frames: Map<string, CapturedFrame[]>,
    observeMs: number,
): Promise<void> {
    await new Promise((r) => setTimeout(r, observeMs));

    for (const frame of frames.get(result.messageId!) ?? []) {
        if (frame.tag === "ack") {
            result.ack = frame;
            // The server reports a rejected stanza by attribute, not by closing
            // the socket -- this is the field that would prove a server-side refusal.
            if (frame.attrs.error) result.ackError = frame.attrs.error;
        } else if (frame.tag === "receipt") {
            result.receipts.push(frame);
        } else {
            result.otherFrames.push(frame);
        }
    }
}
