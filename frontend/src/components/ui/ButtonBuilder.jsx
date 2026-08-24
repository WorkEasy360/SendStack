import { ExternalLink, Link2, MousePointerClick, Plus, Reply, X } from "lucide-react";
import Button from "./Button";
import {
    MAX_REPLY_BUTTONS,
    MAX_TITLE_LENGTH,
    REPLY,
    URL_TYPE,
    buttonKind,
    deriveButtonId,
} from "../../utils/buttons";

/**
 * Editor for the interactive buttons under a message.
 *
 * The value is the `buttons` list the API takes — `{type, id, title, url}`.
 * The three modes are a real constraint, not a UI simplification: WhatsApp
 * sends reply buttons and a link button as different kinds of interactive
 * message, so a list mixing them is rejected on save. Choosing a mode up front
 * is what stops someone building a message that cannot be sent.
 *
 * A reply button's `id` is what automation matches on, so it is only ever
 * filled in while blank (see fillBlankId) and never rewritten afterwards —
 * relabelling a saved button must not unhook the rules pointing at it.
 */

const MODES = [
    { value: "NONE", label: "No buttons", icon: X },
    { value: REPLY, label: "Reply buttons", icon: Reply },
    { value: URL_TYPE, label: "Link button", icon: Link2 },
];

/**
 * Compact field for a button row.
 *
 * Deliberately not <Input>: appending `py-1.5 text-xs` to it would lose to its
 * own `py-2.5 text-sm` on Tailwind source order, so the row would silently
 * stay full height.
 */
function SmallInput({ className = "", ...props }) {
    return (
        <input
            className={`w-full rounded-md border border-ink-200 bg-white px-2 py-1.5 text-xs text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/15 ${className}`}
            {...props}
        />
    );
}

export default function ButtonBuilder({
    value = [],
    onChange,
    disabled = false,
    label = "Buttons",
    hint = "Shown under the message. Tapping a reply button can trigger an automation rule.",
}) {
    const buttons = value || [];
    const mode = buttonKind(buttons) || "NONE";

    const setMode = (next) => {
        if (next === mode) return;
        // Switching mode replaces the list rather than converting it: a reply
        // button has no url and a link button has no id, so there is nothing
        // meaningful to carry across.
        if (next === "NONE") return onChange([]);
        if (next === URL_TYPE) return onChange([{ type: URL_TYPE, id: "", title: "", url: "" }]);
        return onChange([{ type: REPLY, id: "", title: "", url: "" }]);
    };

    const update = (index, patch) => {
        onChange(buttons.map((button, position) => (position === index ? { ...button, ...patch } : button)));
    };

    /**
     * Fill in a blank id once the label is finished, not while it is being
     * typed: deriving on every keystroke turned "interested" into the id "i",
     * because the first character was already a complete-looking label.
     *
     * Only a blank id is ever filled, so a button that has been saved — and
     * that automation rules already point at — keeps its id when relabelled.
     */
    const fillBlankId = (index) => {
        const button = buttons[index];
        if (button.type === URL_TYPE || button.id || !button.title.trim()) return;
        update(index, {
            id: deriveButtonId(
                button.title,
                buttons.filter((_, position) => position !== index).map((other) => other.id)
            ),
        });
    };

    const addReply = () => {
        onChange([...buttons, { type: REPLY, id: "", title: "", url: "" }]);
    };

    const remove = (index) => {
        onChange(buttons.filter((_, position) => position !== index));
    };

    return (
        <div className="mb-4">
            <span className="block text-sm font-semibold text-ink-700 mb-1.5">{label}</span>

            <div className="inline-flex rounded-lg border border-ink-200 bg-ink-50 p-0.5 mb-2">
                {MODES.map(({ value: modeValue, label: modeLabel, icon: Icon }) => (
                    <button
                        key={modeValue}
                        type="button"
                        disabled={disabled}
                        onClick={() => setMode(modeValue)}
                        className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                            mode === modeValue
                                ? "bg-white text-ink-900 shadow-card"
                                : "text-ink-500 hover:text-ink-800"
                        }`}
                    >
                        <Icon size={13} aria-hidden="true" />
                        {modeLabel}
                    </button>
                ))}
            </div>

            {mode === REPLY && (
                <ul className="list-none p-0 m-0 space-y-2">
                    {buttons.map((button, index) => (
                        <li
                            key={index}
                            className="flex items-start gap-2 rounded-lg border border-ink-200 bg-white p-2.5"
                        >
                            <MousePointerClick size={15} aria-hidden="true" className="mt-2 shrink-0 text-ink-400" />
                            <div className="min-w-0 flex-1 grid gap-1.5 sm:grid-cols-2">
                                <div>
                                    <SmallInput
                                        value={button.title}
                                        maxLength={MAX_TITLE_LENGTH}
                                        placeholder="Button label"
                                        disabled={disabled}
                                        onChange={(event) => update(index, { title: event.target.value })}
                                        onBlur={() => fillBlankId(index)}
                                    />
                                    <span className="block text-[11px] text-ink-400 mt-1">
                                        {button.title.length}/{MAX_TITLE_LENGTH}
                                    </span>
                                </div>
                                <div>
                                    <SmallInput
                                        value={button.id}
                                        placeholder="button_id"
                                        disabled={disabled}
                                        className="font-mono"
                                        onChange={(event) => update(index, { id: event.target.value })}
                                    />
                                    <span className="block text-[11px] text-ink-400 mt-1">
                                        Automation matches this id
                                    </span>
                                </div>
                            </div>
                            <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                icon={X}
                                disabled={disabled}
                                className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                                onClick={() => remove(index)}
                                aria-label={`Remove ${button.title || "button"}`}
                            />
                        </li>
                    ))}

                    <li className="flex items-center gap-2.5">
                        <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            icon={Plus}
                            disabled={disabled || buttons.length >= MAX_REPLY_BUTTONS}
                            onClick={addReply}
                        >
                            Add button
                        </Button>
                        <span className="text-xs text-ink-500">
                            {buttons.length} of {MAX_REPLY_BUTTONS} — WhatsApp allows no more than three.
                        </span>
                    </li>
                </ul>
            )}

            {mode === URL_TYPE && (
                <div className="flex items-start gap-2 rounded-lg border border-ink-200 bg-white p-2.5">
                    <ExternalLink size={15} aria-hidden="true" className="mt-2 shrink-0 text-ink-400" />
                    <div className="min-w-0 flex-1 grid gap-1.5 sm:grid-cols-2">
                        <div>
                            <SmallInput
                                value={buttons[0]?.title || ""}
                                maxLength={MAX_TITLE_LENGTH}
                                placeholder="Button label"
                                disabled={disabled}
                                onChange={(event) => update(0, { title: event.target.value })}
                            />
                            <span className="block text-[11px] text-ink-400 mt-1">
                                {(buttons[0]?.title || "").length}/{MAX_TITLE_LENGTH}
                            </span>
                        </div>
                        <div>
                            <SmallInput
                                type="url"
                                value={buttons[0]?.url || ""}
                                placeholder="https://..."
                                disabled={disabled}
                                onChange={(event) => update(0, { url: event.target.value })}
                            />
                            <span className="block text-[11px] text-ink-400 mt-1">
                                Opens the link — nothing comes back, so it can't trigger automation.
                            </span>
                        </div>
                    </div>
                </div>
            )}

            {hint && <span className="block text-xs text-ink-500 mt-1.5">{hint}</span>}
        </div>
    );
}

/** The buttons as the contact sees them, for previews and message bubbles. */
export function ButtonPreview({ buttons, className = "" }) {
    if (!buttons?.length) return null;

    return (
        <div className={`flex flex-col gap-1 ${className}`}>
            {buttons.map((button, index) => (
                <span
                    key={button.id || index}
                    className="flex items-center justify-center gap-1.5 rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-xs font-semibold text-brand-700"
                >
                    {button.type === URL_TYPE && <ExternalLink size={12} aria-hidden="true" />}
                    {button.title || <em className="text-ink-400 font-normal">Untitled</em>}
                </span>
            ))}
        </div>
    );
}
