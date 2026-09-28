/**
 * Reading the button a contact tapped, whichever shape WhatsApp reported it in.
 *
 * This lives on its own because two callers depend on it and they must not
 * drift: the inbound webhook path in `providers/baileysProvider.ts`, which is
 * what Django's automation matches ids against, and `buttonLab.ts`, which is
 * what a human uses to decide whether native buttons work at all. If the lab
 * flattened taps with its own copy of this logic, a lab run could report the
 * right id while production reported nothing, and the measurement would be
 * worthless precisely when it mattered.
 */
import { normalizeMessageContent } from "@whiskeysockets/baileys";

export interface ButtonReply {
    id: string;
    title: string;
}

/**
 * The id and label of a tapped button, or null if this message is not one.
 *
 * FOUR SHAPES, ONE MEANING: a native-flow reply (what our own interactive
 * messages come back as), the older buttons and template replies, and a list
 * selection. Flattened here so Django only ever sees "this id was tapped".
 *
 * THE CONTENT IS UNWRAPPED FIRST. A reply does not necessarily arrive bare:
 * WhatsApp wraps message content depending on the chat's settings and which
 * device answered, and a wrapped `interactiveResponseMessage` is still a
 * tapped button. Reading `message.interactiveResponseMessage` directly would
 * return null for those and forward the tap as an unknown message with no body
 * -- the customer would have answered and nothing would have matched.
 *
 * `normalizeMessageContent` handles most wrappers (`ephemeralMessage`,
 * `viewOnceMessage` and its V2 forms, `documentWithCaptionMessage`,
 * `editedMessage`) and is a no-op on the bare form. It does NOT handle
 * `deviceSentMessage` -- read `getFutureProofMessage` in Baileys 6.7.24's
 * `Utils/messages.js`, which does not list it -- so that one is peeled here.
 * Today no caller can hit it, because both filter `key.fromMe` and
 * `deviceSentMessage` only ever carries our own send echoed back from another
 * of our devices. It is peeled anyway: this is the one place that decides what
 * a tap means, and a shared flattener that is correct only because of what its
 * current callers happen to filter is a trap for the next caller.
 *
 * The two are interleaved in a loop because either can wrap the other -- a
 * disappearing-messages chat produces `ephemeralMessage` inside
 * `deviceSentMessage`, and one pass in a fixed order would leave the second
 * wrapper on.
 *
 * Each check reads a nested VALUE rather than testing `in`: Baileys message
 * objects are protobuf instances whose prototype carries every optional field,
 * so `"buttonsResponseMessage" in message` is true for every message.
 */
export function extractButtonReply(message: any): ButtonReply | null {
    let content: any = message ? normalizeMessageContent(message) : null;
    // Bounded like Baileys' own unwrap loop: a malformed or hostile message
    // must not be able to spin here.
    for (let depth = 0; depth < 5 && content?.deviceSentMessage?.message; depth++) {
        content = normalizeMessageContent(content.deviceSentMessage.message);
    }
    if (!content) return null;

    const buttons = content.buttonsResponseMessage;
    if (buttons?.selectedButtonId) {
        return { id: buttons.selectedButtonId, title: buttons.selectedDisplayText ?? buttons.selectedButtonId };
    }

    const template = content.templateButtonReplyMessage;
    if (template?.selectedId) {
        return { id: template.selectedId, title: template.selectedDisplayText ?? template.selectedId };
    }

    const listRowId = content.listResponseMessage?.singleSelectReply?.selectedRowId;
    if (listRowId) {
        return { id: listRowId, title: content.listResponseMessage.title ?? listRowId };
    }

    const nativeFlow = content.interactiveResponseMessage?.nativeFlowResponseMessage;
    if (nativeFlow?.paramsJson) {
        try {
            const params = JSON.parse(nativeFlow.paramsJson);
            if (params?.id) {
                const label = content.interactiveResponseMessage?.body?.text;
                return { id: String(params.id), title: label ?? String(params.id) };
            }
        } catch {
            // A params blob we can't read is not a reason to drop the message;
            // it falls through and is forwarded as ordinary text.
        }
    }

    return null;
}
