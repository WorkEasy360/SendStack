/**
 * Shared reading of list and poll message content on the client.
 *
 * Mirrors `core/interactive.py`, including its limits — which are WhatsApp's,
 * not this product's, so a builder that let somebody exceed one would only be
 * deferring a rejection until save.
 *
 * The four content kinds are mutually exclusive because WhatsApp sends them as
 * four different kinds of message. There is no envelope carrying a menu *and*
 * a poll, so the composer picks one rather than offering a combination the API
 * would refuse. (Within the BUTTON kind the three *button* kinds do combine —
 * see utils/buttons.js, which is a different question.)
 */

import { CALL, REPLY, URL_TYPE, buttonKind } from "./buttons";

export const TEXT = "TEXT";
export const BUTTON = "BUTTON";
export const LIST = "LIST";
export const POLL = "POLL";

export const MAX_LIST_HEADER_LENGTH = 60;
export const MAX_LIST_FOOTER_LENGTH = 60;
export const MAX_LIST_BUTTON_LENGTH = 20;
export const MAX_LIST_SECTIONS = 10;
/** Rows across *every* section, not per section — WhatsApp counts the whole menu. */
export const MAX_LIST_ROWS = 10;
export const MAX_ROW_TITLE_LENGTH = 24;
export const MAX_ROW_DESCRIPTION_LENGTH = 72;
export const MAX_SECTION_TITLE_LENGTH = 24;

export const MAX_POLL_QUESTION_LENGTH = 255;
export const MIN_POLL_OPTIONS = 2;
export const MAX_POLL_OPTIONS = 12;
export const MAX_POLL_OPTION_LENGTH = 100;

export const DEFAULT_LIST_BUTTON = "View options";

/** An empty list, for a composer switching into LIST mode. */
export function emptyList() {
    return {
        header: "",
        footer: "",
        button: DEFAULT_LIST_BUTTON,
        sections: [{ title: "", rows: [{ id: "", title: "", description: "" }] }],
    };
}

/** An empty poll, opened with the two options a poll must have at minimum. */
export function emptyPoll() {
    return { question: "", options: ["", ""], allow_multiple: false };
}

/** TEXT / BUTTON / LIST / POLL for a template, campaign or draft. */
export function contentKind(record) {
    if (record?.poll_content?.question || record?.poll_content?.options?.length) return POLL;
    if (record?.list_content?.sections?.length) return LIST;
    if (record?.buttons?.length) return BUTTON;
    return TEXT;
}

/** Every row of a list, flattened across its sections, in display order. */
export function listRows(content) {
    return (content?.sections || []).flatMap((section) => section.rows || []);
}

/**
 * The id a row gets when its author does not supply one, matching the
 * server's `_derive_id`.
 *
 * Derived once and then left alone: automation rules store this id, so
 * rewriting it when somebody edits a label would unhook every rule pointing at
 * the row.
 */
export function deriveRowId(title, taken = []) {
    const base =
        (title || "")
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "_")
            .replace(/^_+|_+$/g, "") || "option";

    let candidate = base;
    let suffix = 2;
    while (taken.includes(candidate)) {
        candidate = `${base}_${suffix}`;
        suffix += 1;
    }
    return candidate;
}

/**
 * Which provider capability this content needs, or "" for content that needs
 * none. Mirrors `apps.templates_app.delivery.required_capability`.
 *
 * One name for content that may need several — a button message can carry
 * reply, link and call buttons at once. This is what the composer uses to
 * decide whether there is anything a connection could refuse, which only needs
 * the primary kind; the full answer comes back from the server's delivery
 * preview.
 */
export function requiredCapability(record) {
    const kind = contentKind(record);
    if (kind === LIST) return "lists";
    if (kind === POLL) return "polls";
    if (kind === BUTTON) {
        const type = buttonKind(record.buttons) || REPLY;
        if (type === URL_TYPE) return "url_buttons";
        if (type === CALL) return "call_buttons";
        return "buttons";
    }
    return "";
}

/**
 * Whether a draft is complete enough to save, as one sentence or "".
 *
 * Deliberately *not* the full server-side validation: this is what the Save
 * button reads, and duplicating every limit here would create a second place
 * for them to drift. The server remains the authority and returns a 400 with
 * its own wording; this catches the two mistakes somebody makes constantly
 * (an empty row, one poll option) before a round trip.
 */
export function contentProblem(record) {
    const kind = contentKind(record);

    if (kind === LIST) {
        const rows = listRows(record.list_content);
        if (!rows.length) return "Add at least one row to the menu.";
        if (rows.some((row) => !row.title.trim())) return "Every menu row needs a label.";
        if (rows.length > MAX_LIST_ROWS) {
            return `A menu can offer at most ${MAX_LIST_ROWS} rows in total (${rows.length} added).`;
        }
        if ((record.list_content.sections || []).length > 1) {
            const untitled = record.list_content.sections.some((section) => !section.title.trim());
            if (untitled) return "Every section needs a title when there is more than one.";
        }
        if (!record.body?.trim()) return "A menu message needs body text to open the menu from.";
    }

    if (kind === POLL) {
        const options = (record.poll_content.options || []).filter((option) => option.trim());
        if (!record.poll_content.question?.trim()) return "The poll needs a question.";
        if (options.length < MIN_POLL_OPTIONS) return `A poll needs at least ${MIN_POLL_OPTIONS} options.`;
        const folded = options.map((option) => option.trim().toLowerCase());
        if (new Set(folded).size !== folded.length) return "Every poll option must be different.";
    }

    return "";
}

/**
 * The payload to send, with the fields for the *other* kinds explicitly
 * cleared.
 *
 * Clearing matters on an edit: switching a template from a menu to a poll has
 * to remove the menu, and omitting the field would leave the old one in place
 * and the save would be refused for carrying two.
 */
export function contentPayload(form) {
    const kind = contentKind(form);
    return {
        buttons: kind === BUTTON ? form.buttons : [],
        list_content: kind === LIST ? form.list_content : {},
        poll_content:
            kind === POLL
                ? {
                      ...form.poll_content,
                      options: (form.poll_content.options || []).filter((option) => option.trim()),
                  }
                : {},
    };
}
