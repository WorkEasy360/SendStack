import { useEffect, useRef, useState } from "react";
import { Bold, Braces, Code, Italic, List, ListOrdered, Quote, Smile, Strikethrough, Terminal } from "lucide-react";
import { LINE_FORMATS, toggleLinePrefix, toggleWrap } from "../../utils/formatting";
import { Textarea } from "./Field";

/**
 * The message body box, with WhatsApp's own formatting and a variable picker.
 *
 * Every control here produces **plain text**. WhatsApp formats with markers it
 * interprets on the recipient's phone, so the toolbar inserts `*` and `_` into
 * the value rather than maintaining a rich-text document — which is what lets
 * an author type the markers by hand, paste a message from WhatsApp, and get
 * exactly what they see in the preview.
 *
 * Only the formats WhatsApp actually renders are offered. Underline, colour
 * and font size are absent because a button that inserted markers the
 * recipient sees as literal punctuation would be worse than no button.
 */

const WRAP_FORMATS = [
    { key: "bold", label: "Bold", marker: "*", icon: Bold },
    { key: "italic", label: "Italic", marker: "_", icon: Italic },
    { key: "strikethrough", label: "Strikethrough", marker: "~", icon: Strikethrough },
    { key: "code", label: "Inline code", marker: "`", icon: Code },
    { key: "monospace", label: "Monospace block", marker: "```", icon: Terminal },
];

const LINE_ICONS = { bullet: List, numbered: ListOrdered, quote: Quote };

/**
 * A small, fixed set rather than a full emoji keyboard.
 *
 * A complete picker is a large dependency and a search box nobody uses for
 * this: business messages reach for the same two dozen symbols, and every
 * system keyboard is one shortcut away for the rest.
 */
const EMOJI = [
    "😀", "😊", "🙏", "👍", "👌", "🎉", "✅", "❌", "⚠️", "❤️",
    "🔥", "⭐", "📢", "📞", "📱", "💬", "📅", "⏰", "📍", "🚀",
    "💰", "🎁", "🛒", "📦", "✨", "👋", "🙌", "💡", "📝", "🔔",
];

function ToolbarButton({ label, icon: Icon, onClick, active = false }) {
    return (
        <button
            type="button"
            title={label}
            aria-label={label}
            onMouseDown={(event) => event.preventDefault()}
            onClick={onClick}
            className={`inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
                active ? "bg-surface text-ink-900 shadow-card" : "text-ink-500 hover:bg-surface hover:text-ink-800"
            }`}
        >
            <Icon size={14} aria-hidden="true" />
        </button>
    );
}

export default function MessageComposer({
    value,
    onChange,
    variables = { builtin: [], custom: [] },
    label = "Message",
    hint,
    rows = 6,
    required = false,
    placeholder,
}) {
    const boxRef = useRef(null);
    const [showEmoji, setShowEmoji] = useState(false);
    const [showVariables, setShowVariables] = useState(false);
    const pending = useRef(null);

    // The selection has to be restored *after* React has re-rendered with the
    // new value, or the cursor lands at the end of the box on every click and
    // the toolbar becomes unusable for anything but a single word.
    useEffect(() => {
        if (!pending.current || !boxRef.current) return;
        const { start, end } = pending.current;
        pending.current = null;
        boxRef.current.focus();
        boxRef.current.setSelectionRange(start, end);
    }, [value]);

    const apply = (transform) => {
        const box = boxRef.current;
        if (!box) return;
        const result = transform(value || "", box.selectionStart, box.selectionEnd);
        pending.current = { start: result.start, end: result.end };
        onChange(result.value);
    };

    const insert = (text) => {
        const box = boxRef.current;
        const start = box ? box.selectionStart : (value || "").length;
        const end = box ? box.selectionEnd : start;
        const next = `${(value || "").slice(0, start)}${text}${(value || "").slice(end)}`;
        pending.current = { start: start + text.length, end: start + text.length };
        onChange(next);
    };

    const allVariables = [...(variables.builtin || []), ...(variables.custom || [])];

    return (
        <div className="mb-4">
            <span className="block text-sm font-semibold text-ink-700 mb-1.5">
                {label}
                {required && <span className="text-red-500 ml-0.5">*</span>}
            </span>

            <div className="rounded-lg border border-ink-200 bg-surface focus-within:border-brand-500 focus-within:ring-4 focus-within:ring-brand-500/15">
                <div className="flex flex-wrap items-center gap-0.5 border-b border-ink-100 bg-ink-50 px-1.5 py-1">
                    {WRAP_FORMATS.map(({ key, label: title, marker, icon }) => (
                        <ToolbarButton
                            key={key}
                            label={title}
                            icon={icon}
                            onClick={() => apply((text, start, end) => toggleWrap(text, start, end, marker))}
                        />
                    ))}

                    <span className="mx-1 h-4 w-px bg-ink-200" aria-hidden="true" />

                    {LINE_FORMATS.map(({ key, label: title, prefix }) => (
                        <ToolbarButton
                            key={key}
                            label={title}
                            icon={LINE_ICONS[key]}
                            onClick={() => apply((text, start, end) => toggleLinePrefix(text, start, end, prefix))}
                        />
                    ))}

                    <span className="mx-1 h-4 w-px bg-ink-200" aria-hidden="true" />

                    <div className="relative">
                        <ToolbarButton
                            label="Emoji"
                            icon={Smile}
                            active={showEmoji}
                            onClick={() => {
                                setShowEmoji((open) => !open);
                                setShowVariables(false);
                            }}
                        />
                        {showEmoji && (
                            <div className="absolute left-0 top-8 z-20 w-64 rounded-lg border border-ink-200 bg-surface p-2 shadow-lg">
                                <div className="grid grid-cols-10 gap-0.5">
                                    {EMOJI.map((emoji) => (
                                        <button
                                            key={emoji}
                                            type="button"
                                            className="rounded p-1 text-base leading-none hover:bg-ink-100"
                                            onClick={() => {
                                                insert(emoji);
                                                setShowEmoji(false);
                                            }}
                                        >
                                            {emoji}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>

                    <div className="relative">
                        <ToolbarButton
                            label="Insert a variable"
                            icon={Braces}
                            active={showVariables}
                            onClick={() => {
                                setShowVariables((open) => !open);
                                setShowEmoji(false);
                            }}
                        />
                        {showVariables && (
                            <div className="absolute left-0 top-8 z-20 max-h-64 w-60 overflow-auto rounded-lg border border-ink-200 bg-surface p-1.5 shadow-lg">
                                {!allVariables.length && (
                                    <p className="m-0 px-2 py-1.5 text-xs text-ink-500">
                                        No variables available yet.
                                    </p>
                                )}
                                {(variables.custom || []).length > 0 && (
                                    <p className="m-0 px-2 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                                        From your contacts
                                    </p>
                                )}
                                {(variables.custom || []).map((name) => (
                                    <button
                                        key={`custom-${name}`}
                                        type="button"
                                        className="block w-full rounded px-2 py-1 text-left font-mono text-xs text-ink-700 hover:bg-ink-100"
                                        onClick={() => {
                                            insert(`{{${name}}}`);
                                            setShowVariables(false);
                                        }}
                                    >
                                        {`{{${name}}}`}
                                    </button>
                                ))}
                                {(variables.builtin || []).length > 0 && (
                                    <p className="m-0 px-2 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                                        Built in
                                    </p>
                                )}
                                {(variables.builtin || []).map((name) => (
                                    <button
                                        key={`builtin-${name}`}
                                        type="button"
                                        className="block w-full rounded px-2 py-1 text-left font-mono text-xs text-ink-700 hover:bg-ink-100"
                                        onClick={() => {
                                            insert(`{{${name}}}`);
                                            setShowVariables(false);
                                        }}
                                    >
                                        {`{{${name}}}`}
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>
                </div>

                <Textarea
                    ref={boxRef}
                    rows={rows}
                    value={value || ""}
                    placeholder={placeholder}
                    onChange={(event) => onChange(event.target.value)}
                    // The border and focus ring live on the wrapper so the
                    // toolbar and the box read as one control.
                    className="border-0 bg-transparent focus:ring-0 focus:border-transparent"
                />
            </div>

            {hint && <span className="mt-1.5 block text-xs text-ink-500">{hint}</span>}
        </div>
    );
}
