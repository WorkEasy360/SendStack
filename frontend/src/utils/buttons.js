/**
 * Shared reading of interactive message buttons on the client.
 *
 * Mirrors `core.buttons` on the server, including its per-kind limits: up to
 * three REPLY buttons, at most one URL button and at most one CALL button, in
 * any combination. One message can carry all three.
 *
 * **What it cannot do is show all three as buttons.** WhatsApp has no envelope
 * carrying two kinds at once, so on the wire one kind stays tappable and the
 * rest are written into the body as labelled lines — `planDelivery` below, and
 * `core.buttons.plan_delivery` on the server, which is the copy that actually
 * builds the payload. The composer shows the result before anything is saved,
 * so a combination is an authoring convenience rather than a promise the send
 * has to break.
 *
 * A CALL button is never delivered as a button by any connection — Meta offers
 * one only inside an approved template. It is offered anyway because it
 * degrades predictably to a labelled number that every WhatsApp client turns
 * into a dial link, which is better than making somebody paste a number into
 * the body and hope.
 */

export const REPLY = "REPLY";
export const URL_TYPE = "URL";
export const CALL = "CALL";

export const MAX_REPLY_BUTTONS = 3;
export const MAX_TITLE_LENGTH = 20;
export const MAX_BODY_LENGTH = 1024;

/** The buttons on a saved record — always a list, never undefined. */
export function toButtons(record) {
    return record?.buttons || [];
}

/** `{ replies, link, call }` — the list split by kind, mirroring `split_buttons`. */
export function splitButtons(buttons) {
    const list = buttons || [];
    return {
        replies: list.filter((button) => (button.type || REPLY) === REPLY),
        link: list.find((button) => button.type === URL_TYPE) || null,
        call: list.find((button) => button.type === CALL) || null,
    };
}

/**
 * The kind that goes out tappable — `REPLY`, `URL`, `CALL`, or `null` for a
 * message with no buttons. Mirrors the server's `button_kind`.
 *
 * Reply buttons outrank a link because they are the only kind an answer comes
 * back from; a call button never wins, because no connection has ever
 * delivered one.
 */
export function buttonKind(buttons) {
    const { replies, link, call } = splitButtons(buttons);
    if (replies.length) return REPLY;
    if (link) return URL_TYPE;
    if (call) return CALL;
    return null;
}

/**
 * `{ tappable, spelledOut }` for a connection that can show buttons at all —
 * the browser's copy of `core.buttons.plan_delivery`, used only to *say* what
 * will happen. The server is what decides it.
 */
export function planDelivery(buttons) {
    const { replies, link } = splitButtons(buttons);
    const tappable = replies.length ? replies : link ? [link] : [];
    return { tappable, spelledOut: (buttons || []).filter((button) => !tappable.includes(button)) };
}

/**
 * The id a new reply button gets, matching the server's `_derive_id`.
 *
 * Derived once, on add, and then left alone: automation rules and workflow
 * conditions store this id, so rewriting it when someone edits a label would
 * silently unhook every rule pointing at the button.
 */
export function deriveButtonId(title, taken = []) {
    const base =
        title
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "_")
            .replace(/^_+|_+$/g, "") || "button";

    let candidate = base;
    let suffix = 2;
    while (taken.includes(candidate)) {
        candidate = `${base}_${suffix}`;
        suffix += 1;
    }
    return candidate;
}

/** Every id a contact could send back by tapping one of these buttons. */
export function replyButtonIds(buttons) {
    return (buttons || []).filter((button) => (button.type || REPLY) === REPLY && button.id).map((button) => button.id);
}
