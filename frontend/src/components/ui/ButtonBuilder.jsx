import { ExternalLink, Link2, ListOrdered, MousePointerClick, Phone, Reply, X } from "lucide-react";
import Button from "./Button";
import {
    CALL,
    MAX_REPLY_BUTTONS,
    MAX_TITLE_LENGTH,
    REPLY,
    URL_TYPE,
    deriveButtonId,
    planDelivery,
    splitButtons,
} from "../../utils/buttons";

/**
 * Editor for the interactive buttons under a message.
 *
 * The value is the `buttons` list the API takes — `{type, id, title, url,
 * phone}`. The three kinds combine freely, up to three reply buttons plus one
 * link and one call button, because that is what an author is usually trying
 * to build: "yes / no, or call us".
 *
 * **What they cannot do is all arrive as buttons.** WhatsApp has no message
 * that carries two kinds at once, so one kind stays tappable and the rest are
 * written into the body as labelled lines — `planDelivery`, mirroring the
 * server function that actually builds the payload. That is stated here, next
 * to the fields, rather than left for the send to reveal: the combination is
 * still worth offering, because the alternative is somebody pasting the link
 * into the body by hand and getting the same result with more work.
 *
 * A reply button's `id` is what automation matches on, so it is only ever
 * filled in while blank (see fillBlankId) and never rewritten afterwards —
 * relabelling a saved button must not unhook the rules pointing at it.
 */

//: Reply buttons, then the link, then the call button — the order they are
//: kept in, so the numbered fallback reads in the order the editor shows.
const KIND_ORDER = [REPLY, URL_TYPE, CALL];

const blank = (type) => ({ type, id: "", title: "", url: "", phone: "" });

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
            className={`w-full rounded-md border border-ink-200 bg-surface px-2 py-1.5 text-xs text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/15 ${className}`}
            {...props}
        />
    );
}

/** One editable button, whatever its kind — icon, two fields, remove. */
function ButtonRow({ icon: Icon, children, onRemove, disabled, label }) {
    return (
        <li className="flex items-start gap-2 rounded-lg border border-ink-200 bg-surface p-2.5">
            <Icon size={15} aria-hidden="true" className="mt-2 shrink-0 text-ink-400" />
            <div className="min-w-0 flex-1 grid gap-1.5 @md:grid-cols-2">{children}</div>
            <Button
                type="button"
                variant="ghost"
                size="sm"
                icon={X}
                disabled={disabled}
                className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                onClick={onRemove}
                aria-label={`Remove ${label}`}
            />
        </li>
    );
}

/** A one-line note under the builder — the same shape for every reason. */
function Note({ icon: Icon, children }) {
    return (
        <p className="flex gap-2 items-start text-xs text-ink-600 bg-ink-50 border border-ink-200 rounded-lg p-2.5 mt-2 mb-0">
            <Icon size={14} aria-hidden="true" className="mt-0.5 shrink-0 text-ink-400" />
            <span>{children}</span>
        </p>
    );
}

export default function ButtonBuilder({
    value = [],
    onChange,
    disabled = false,
    label = "Buttons",
    hint = "Shown under the message. Tapping a reply button can trigger an automation rule.",
    // The connection this message will go out on, where the caller knows it.
    // "QR_PROVIDER" means the buttons are delivered as a numbered list -- not
    // an error, and not something the user has to configure, so it is stated
    // rather than warned about.
    provider = null,
}) {
    const buttons = value || [];
    const { replies, link, call } = splitButtons(buttons);

    /**
     * Written back in kind order on every change. A stable sort, so reply
     * buttons keep the order they were added in while a link added first
     * still ends up beneath them — which is the order they are numbered in
     * when the connection cannot show buttons at all.
     */
    const write = (next) =>
        onChange([...next].sort((a, b) => KIND_ORDER.indexOf(a.type || REPLY) - KIND_ORDER.indexOf(b.type || REPLY)));

    const add = (type) => write([...buttons, blank(type)]);

    const update = (index, patch) => {
        write(buttons.map((button, position) => (position === index ? { ...button, ...patch } : button)));
    };

    const remove = (index) => write(buttons.filter((_, position) => position !== index));

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
        if (button.type !== REPLY || button.id || !button.title.trim()) return;
        update(index, {
            id: deriveButtonId(
                button.title,
                buttons.filter((_, position) => position !== index).map((other) => other.id)
            ),
        });
    };

    // What this list will actually arrive as. `spelledOut` is what loses its
    // tap to the message format itself and is therefore true on every number;
    // `numbered` is the separate case of a connection that shows no buttons at
    // all, where even the reply buttons become text.
    const { spelledOut } = planDelivery(buttons);
    const numbered = provider === "QR_PROVIDER" && replies.length > 0;
    const linkSpelledOut = spelledOut.includes(link);

    const ADD_ACTIONS = [
        {
            type: REPLY,
            label: "Quick reply",
            icon: Reply,
            full: replies.length >= MAX_REPLY_BUTTONS,
            title: `WhatsApp allows at most ${MAX_REPLY_BUTTONS} reply buttons`,
        },
        { type: URL_TYPE, label: "Link", icon: Link2, full: Boolean(link), title: "One link button per message" },
        { type: CALL, label: "Call", icon: Phone, full: Boolean(call), title: "One call button per message" },
    ];

    return (
        <div className="@container mb-4">
            <span className="block text-sm font-semibold text-ink-700 mb-1.5">{label}</span>

            {buttons.length > 0 && (
                <ul className="list-none p-0 m-0 space-y-2 mb-2">
                    {buttons.map((button, index) => {
                        const kind = button.type || REPLY;

                        if (kind === URL_TYPE) {
                            return (
                                <ButtonRow
                                    key={`url-${index}`}
                                    icon={ExternalLink}
                                    disabled={disabled}
                                    onRemove={() => remove(index)}
                                    label={button.title || "link button"}
                                >
                                    <div>
                                        <SmallInput
                                            value={button.title}
                                            maxLength={MAX_TITLE_LENGTH}
                                            placeholder="Shop now"
                                            disabled={disabled}
                                            onChange={(event) => update(index, { title: event.target.value })}
                                        />
                                        <span className="block text-[11px] text-ink-400 mt-1">
                                            {button.title.length}/{MAX_TITLE_LENGTH}
                                        </span>
                                    </div>
                                    <div>
                                        <SmallInput
                                            type="url"
                                            value={button.url}
                                            placeholder="https://..."
                                            disabled={disabled}
                                            onChange={(event) => update(index, { url: event.target.value })}
                                        />
                                        <span className="block text-[11px] text-ink-400 mt-1">
                                            Opens the link — nothing comes back, so it can&rsquo;t trigger
                                            automation.
                                        </span>
                                    </div>
                                </ButtonRow>
                            );
                        }

                        if (kind === CALL) {
                            return (
                                <ButtonRow
                                    key={`call-${index}`}
                                    icon={Phone}
                                    disabled={disabled}
                                    onRemove={() => remove(index)}
                                    label={button.title || "call button"}
                                >
                                    <div>
                                        <SmallInput
                                            value={button.title}
                                            maxLength={MAX_TITLE_LENGTH}
                                            placeholder="Call now"
                                            disabled={disabled}
                                            onChange={(event) => update(index, { title: event.target.value })}
                                        />
                                        <span className="block text-[11px] text-ink-400 mt-1">
                                            {button.title.length}/{MAX_TITLE_LENGTH}
                                        </span>
                                    </div>
                                    <div>
                                        <SmallInput
                                            type="tel"
                                            value={button.phone}
                                            placeholder="+919876543210"
                                            disabled={disabled}
                                            onChange={(event) => update(index, { phone: event.target.value })}
                                        />
                                        <span className="block text-[11px] text-ink-400 mt-1">
                                            International format, including the country code.
                                        </span>
                                    </div>
                                </ButtonRow>
                            );
                        }

                        return (
                            <ButtonRow
                                key={`reply-${index}`}
                                icon={MousePointerClick}
                                disabled={disabled}
                                onRemove={() => remove(index)}
                                label={button.title || "button"}
                            >
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
                            </ButtonRow>
                        );
                    })}
                </ul>
            )}

            {/* One row of adders rather than a mode switch: every kind can be
                added while the others are there, and a kind already at its
                limit is disabled rather than hidden so the limit is visible. */}
            <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-ink-500">Add</span>
                {ADD_ACTIONS.map(({ type, label: actionLabel, icon: Icon, full, title }) => (
                    <Button
                        key={type}
                        type="button"
                        size="xs"
                        variant="secondary"
                        icon={Icon}
                        disabled={disabled || full}
                        title={full ? title : undefined}
                        onClick={() => add(type)}
                    >
                        {actionLabel}
                    </Button>
                ))}
                {replies.length > 0 && (
                    <span className="text-xs text-ink-500">
                        {replies.length} of {MAX_REPLY_BUTTONS} reply buttons
                    </span>
                )}
            </div>

            {linkSpelledOut && (
                // Not a warning: the message still goes out with both, and the
                // link is still a link. It says which of the two keeps its tap,
                // so the choice belongs to the author instead of surprising
                // them after the send.
                <Note icon={ExternalLink}>
                    A link button can&rsquo;t sit beside reply buttons &mdash; WhatsApp sends the two as
                    different kinds of message. The reply buttons stay tappable and the link goes out as{" "}
                    <span className="font-semibold">
                        {link.title || "Shop now"}: {link.url || "https://..."}
                    </span>{" "}
                    in the message text, which every WhatsApp client turns into a tappable link.
                </Note>
            )}

            {call && (
                /* Stated for every connection, not just this one: WhatsApp
                   offers a call button only inside an approved template,
                   so no number can send one on an ordinary message. This
                   is what it will actually look like. */
                <Note icon={Phone}>
                    WhatsApp only shows a call button inside an approved message template, so this will be
                    sent as{" "}
                    <span className="font-semibold">
                        {call.title || "Call now"}: {call.phone || "+919876543210"}
                    </span>{" "}
                    &mdash; which every WhatsApp client turns into a tappable dial link.
                </Note>
            )}

            {numbered && (
                // Stated as what will happen, not flagged as a problem: there
                // is nothing here for the user to fix, and the automation they
                // attach to these buttons works either way.
                <Note icon={ListOrdered}>
                    This number is connected by QR code, and WhatsApp only shows tappable buttons on
                    official API numbers. We&rsquo;ll send these as a numbered list instead &mdash; replies
                    like <span className="font-semibold">1</span> or{" "}
                    <span className="font-semibold">{replies[0]?.title || "Yes"}</span> still trigger the
                    same automation.
                </Note>
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
                    className="flex items-center justify-center gap-1.5 rounded-lg border border-ink-200 bg-surface px-3 py-1.5 text-xs font-semibold text-brand-700"
                >
                    {button.type === URL_TYPE && <ExternalLink size={12} aria-hidden="true" />}
                    {button.type === CALL && <Phone size={12} aria-hidden="true" />}
                    {button.title || <em className="text-ink-400 font-normal">Untitled</em>}
                </span>
            ))}
        </div>
    );
}
