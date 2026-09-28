/**
 * WhatsApp's own text formatting, for the composer and its preview.
 *
 * WhatsApp formats plain text with markers it interprets on the recipient's
 * phone. There is no rich-text document here and there must not be one: what
 * gets stored, sent and delivered is the literal characters somebody typed, so
 * the backend needs no knowledge of any of this and a message composed here
 * renders identically in WhatsApp Web, iOS and Android.
 *
 * The eight below are what WhatsApp actually supports. Deliberately absent:
 * underline, colour, font size, links-with-different-text, headings, tables —
 * WhatsApp renders none of them, and offering a button that produced markers
 * the recipient sees as literal asterisks would be worse than not offering it.
 */

export const FORMATS = [
    { key: "bold", label: "Bold", marker: "*", sample: "bold" },
    { key: "italic", label: "Italic", marker: "_", sample: "italic" },
    { key: "strikethrough", label: "Strikethrough", marker: "~", sample: "struck" },
    { key: "monospace", label: "Monospace", marker: "```", sample: "monospace", block: true },
    { key: "code", label: "Inline code", marker: "`", sample: "code" },
];

/** Line-prefix formats: applied to whole lines rather than to a selection. */
export const LINE_FORMATS = [
    { key: "bullet", label: "Bulleted list", prefix: "* " },
    { key: "numbered", label: "Numbered list", prefix: "1. " },
    { key: "quote", label: "Quote", prefix: "> " },
];

/**
 * Wrap the selection in `marker`, or unwrap it if it is already wrapped.
 *
 * Returns `{value, start, end}` so the caller can restore the selection —
 * without that the cursor jumps to the end of the box on every click, which
 * makes the toolbar unusable for anything but a single word.
 */
export function toggleWrap(value, start, end, marker) {
    const before = value.slice(0, start);
    const selected = value.slice(start, end);
    const after = value.slice(end);

    const alreadyWrapped =
        before.endsWith(marker) && after.startsWith(marker);
    if (alreadyWrapped) {
        return {
            value: before.slice(0, -marker.length) + selected + after.slice(marker.length),
            start: start - marker.length,
            end: end - marker.length,
        };
    }

    if (selected.startsWith(marker) && selected.endsWith(marker) && selected.length > marker.length * 2) {
        const inner = selected.slice(marker.length, -marker.length);
        return { value: before + inner + after, start, end: start + inner.length };
    }

    // Nothing selected: drop the markers in and put the cursor between them,
    // so the next thing typed is formatted. Typing then untyping would
    // otherwise leave a pair of stray asterisks behind.
    const body = selected || "";
    return {
        value: `${before}${marker}${body}${marker}${after}`,
        start: start + marker.length,
        end: start + marker.length + body.length,
    };
}

/**
 * Add or remove a line prefix on every line the selection touches.
 *
 * Whole lines, because that is how WhatsApp reads them: "> " only makes a
 * quote at the start of a line, and prefixing mid-sentence would produce a
 * literal chevron in the middle of the message.
 */
export function toggleLinePrefix(value, start, end, prefix) {
    const lineStart = value.lastIndexOf("\n", start - 1) + 1;
    const lineEndIndex = value.indexOf("\n", end);
    const lineEnd = lineEndIndex === -1 ? value.length : lineEndIndex;

    const block = value.slice(lineStart, lineEnd);
    const lines = block.split("\n");
    const numbered = /^\d+\.\s/;
    const allPrefixed = lines.every(
        (line) => line.startsWith(prefix) || (prefix === "1. " && numbered.test(line)),
    );

    const updated = lines
        .map((line, index) => {
            if (allPrefixed) {
                return prefix === "1. " ? line.replace(numbered, "") : line.slice(prefix.length);
            }
            // A numbered list counts up rather than repeating "1." — which is
            // what WhatsApp renders and what anybody typing one expects.
            return prefix === "1. " ? `${index + 1}. ${line}` : `${prefix}${line}`;
        })
        .join("\n");

    const next = value.slice(0, lineStart) + updated + value.slice(lineEnd);
    return { value: next, start: lineStart, end: lineStart + updated.length };
}

const ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (character) => ESCAPE[character]);
}

/**
 * Render WhatsApp markup as HTML, for the preview bubble only.
 *
 * **Escaped first, always.** The input is a template body an author typed,
 * which may itself contain angle brackets, and this result goes through
 * `dangerouslySetInnerHTML`. Escaping before any marker is interpreted is what
 * makes that safe: every `<` in the source is already `&lt;` by the time a tag
 * is inserted, so nothing an author types can become markup.
 *
 * Only ever used to *show* a message. What is sent is the raw text.
 */
export function toHtml(text) {
    let html = escapeHtml(text);

    // Monospace blocks are lifted out first: their content is literal, so it
    // must not then be scanned for the single-character markers. The
    // placeholder is a private-use codepoint written as an escape rather than
    // a literal -- anything printable would collide with text an author
    // actually typed, and a raw control character in a source file breaks
    // every tool that treats the file as text.
    const blocks = [];
    html = html.replace(/```([\s\S]+?)```/g, (_, inner) => {
        blocks.push(inner);
        return `\uE000${blocks.length - 1}\uE001`;
    });

    html = html.replace(/`([^`\n]+)`/g, '<code class="wa-code">$1</code>');
    html = html.replace(/(^|[\s(])\*(\S(?:[^*\n]*\S)?)\*(?=[\s.,!?)]|$)/g, "$1<strong>$2</strong>");
    html = html.replace(/(^|[\s(])_(\S(?:[^_\n]*\S)?)_(?=[\s.,!?)]|$)/g, "$1<em>$2</em>");
    html = html.replace(/(^|[\s(])~(\S(?:[^~\n]*\S)?)~(?=[\s.,!?)]|$)/g, "$1<s>$2</s>");

    html = html
        .split("\n")
        .map((line) => {
            if (line.startsWith("&gt; ")) {
                return `<span class="wa-quote">${line.slice(5)}</span>`;
            }
            return line;
        })
        .join("\n");

    html = html.replace(
        /\uE000(\d+)\uE001/g,
        (_, index) => `<code class="wa-block">${blocks[Number(index)]}</code>`,
    );

    return html;
}
