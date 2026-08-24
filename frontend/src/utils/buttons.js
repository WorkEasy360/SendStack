/**
 * Shared reading of interactive message buttons on the client.
 *
 * Mirrors `core.buttons` on the server, including its two mutually exclusive
 * shapes: up to three REPLY buttons, or a single URL button. They cannot be
 * mixed because WhatsApp sends them as different kinds of interactive message,
 * so every screen that edits buttons picks one mode rather than offering a
 * free-for-all the API would then reject.
 */

export const REPLY = "REPLY";
export const URL_TYPE = "URL";

export const MAX_REPLY_BUTTONS = 3;
export const MAX_TITLE_LENGTH = 20;
export const MAX_BODY_LENGTH = 1024;

/** The buttons on a saved record — always a list, never undefined. */
export function toButtons(record) {
    return record?.buttons || [];
}

/** `REPLY`, `URL`, or `null` for a message with no buttons. */
export function buttonKind(buttons) {
    if (!buttons?.length) return null;
    return buttons[0].type || REPLY;
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
