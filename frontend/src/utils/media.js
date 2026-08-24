/**
 * Shared reading of message attachments on the client.
 *
 * Anything created before multi-attachment support has an empty `media_items`
 * and a single `media_url`, so every screen that shows or edits attachments
 * goes through `toMediaItems` and never reads `media_url` directly. This
 * mirrors `core.media.resolve_media_items` on the server.
 */
export function toMediaItems(record) {
    if (!record) return [];
    if (record.media_items?.length) return record.media_items;

    const type = record.message_type;
    if (!record.media_url || !type || type === "TEXT") return [];
    return [
        {
            url: record.media_url,
            type,
            filename: record.filename || "",
            caption: record.caption || "",
        },
    ];
}

/**
 * The message type a set of attachments implies.
 *
 * The first attachment defines it, matching what the API stores back into
 * `message_type` — so the form never shows a type the save would overwrite.
 */
export function primaryMessageType(items, fallback = "TEXT") {
    return items?.length ? items[0].type : fallback;
}
