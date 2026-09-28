import { BarChart3, ExternalLink, FileText, Film, ImageIcon, List, Phone } from "lucide-react";
import { toHtml } from "../../utils/formatting";
import { contentKind, listRows, BUTTON, LIST, POLL, TEXT } from "../../utils/interactive";
import { contextFromContact, renderButtons, renderList, renderPoll, renderText } from "../../utils/variables";

/**
 * A message as the recipient will see it, redrawn on every keystroke.
 *
 * Everything visual is rendered here rather than fetched: a round trip per
 * character would be neither instant nor kind to the server, and the variable
 * substitution and WhatsApp markup are both small enough to mirror faithfully
 * (see utils/variables.js and utils/formatting.js).
 *
 * What is deliberately *not* guessed at is how the message will be delivered
 * on a connection that cannot draw it. That answer comes from the server —
 * `delivery.as_sent`, produced by the same functions the sender calls — and is
 * shown as-is, because a fallback the composer approximated would eventually
 * disagree with what a customer received.
 */

const MEDIA_ICON = { IMAGE: ImageIcon, VIDEO: Film, DOCUMENT: FileText };

function Bubble({ children, className = "" }) {
    return (
        <div
            className={`theme-static relative max-w-[85%] rounded-xl rounded-tr-sm bg-[#d9fdd3] px-3 py-2 text-[13px] leading-snug text-ink-900 shadow-sm ${className}`}
        >
            {children}
        </div>
    );
}

function FormattedText({ text }) {
    // `toHtml` escapes its input before interpreting a single marker, so an
    // author writing "<script>" gets those characters shown to them rather
    // than executed. See utils/formatting.js.
    return (
        <p
            className="m-0 whitespace-pre-wrap break-words [&_.wa-code]:rounded [&_.wa-code]:bg-black/5 [&_.wa-code]:px-1 [&_.wa-code]:font-mono [&_.wa-code]:text-[12px] [&_.wa-block]:mt-1 [&_.wa-block]:block [&_.wa-block]:whitespace-pre-wrap [&_.wa-block]:rounded [&_.wa-block]:bg-black/5 [&_.wa-block]:p-1.5 [&_.wa-block]:font-mono [&_.wa-block]:text-[12px] [&_.wa-quote]:block [&_.wa-quote]:border-l-[3px] [&_.wa-quote]:border-ink-400 [&_.wa-quote]:pl-2 [&_.wa-quote]:text-ink-600"
            dangerouslySetInnerHTML={{ __html: toHtml(text) }}
        />
    );
}

function MediaBlock({ items }) {
    if (!items?.length) return null;
    return (
        <div className="mb-1.5 space-y-1">
            {items.map((item, index) => {
                const Icon = MEDIA_ICON[item.type] || FileText;
                const isImage = item.type === "IMAGE";
                return (
                    <div key={`${item.url}-${index}`} className="overflow-hidden rounded-lg bg-black/5">
                        {isImage ? (
                            <img
                                src={item.url}
                                alt={item.filename || "Attachment"}
                                className="block max-h-40 w-full object-cover"
                                // A broken link in a preview is information, not
                                // an error: it means the recipient's phone will
                                // not be able to fetch it either.
                                onError={(event) => {
                                    event.currentTarget.style.display = "none";
                                }}
                            />
                        ) : (
                            <div className="flex items-center gap-2 px-2.5 py-3">
                                <Icon size={18} aria-hidden="true" className="shrink-0 text-ink-500" />
                                <span className="truncate text-[12px] font-medium text-ink-700">
                                    {item.filename || item.url}
                                </span>
                            </div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}

function ButtonsBlock({ buttons }) {
    if (!buttons?.length) return null;
    return (
        <div className="mt-1.5 space-y-1 border-t border-black/10 pt-1.5">
            {buttons.map((button, index) => (
                <div
                    key={button.id || index}
                    className="flex items-center justify-center gap-1.5 rounded-md bg-surface/70 px-2 py-1.5 text-[13px] font-semibold text-[#027eb5]"
                >
                    {button.type === "URL" && <ExternalLink size={13} aria-hidden="true" />}
                    {button.type === "CALL" && <Phone size={13} aria-hidden="true" />}
                    {button.title || <span className="font-normal italic text-ink-400">Untitled</span>}
                </div>
            ))}
        </div>
    );
}

function ListBlock({ content }) {
    const rows = listRows(content);
    return (
        <>
            {content.header && <p className="m-0 mb-1 font-bold">{content.header}</p>}
            {content.footer && <p className="m-0 mt-1.5 text-[11px] text-ink-500">{content.footer}</p>}
            <div className="mt-1.5 flex items-center justify-center gap-1.5 rounded-md border-t border-black/10 bg-surface/70 px-2 py-1.5 text-[13px] font-semibold text-[#027eb5]">
                <List size={13} aria-hidden="true" />
                {content.button || "View options"}
            </div>
            {rows.length > 0 && (
                // Shown open rather than behind the tap: the point of the
                // preview is to check the rows, and hiding them behind a
                // button nobody can press here would defeat it.
                <div className="mt-1.5 space-y-1 rounded-lg bg-surface/60 p-1.5">
                    {rows.map((row, index) => (
                        <div key={row.id || index} className="border-b border-black/5 px-1 py-1 last:border-0">
                            <p className="m-0 text-[12px] font-semibold">
                                {row.title || <span className="font-normal italic text-ink-400">Untitled row</span>}
                            </p>
                            {row.description && (
                                <p className="m-0 text-[11px] text-ink-500">{row.description}</p>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </>
    );
}

function PollBlock({ content }) {
    const options = (content.options || []).filter((option) => option.trim());
    return (
        <div>
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-500">
                <BarChart3 size={12} aria-hidden="true" />
                Poll
                {content.allow_multiple && <span className="font-normal normal-case">· several answers</span>}
            </div>
            <p className="m-0 font-semibold">
                {content.question || <span className="font-normal italic text-ink-400">Your question</span>}
            </p>
            <div className="mt-1.5 space-y-1">
                {options.map((option, index) => (
                    <div
                        key={index}
                        className="flex items-center gap-2 rounded-md bg-surface/70 px-2 py-1.5 text-[12px]"
                    >
                        <span className="h-3.5 w-3.5 shrink-0 rounded-full border border-ink-300" />
                        {option}
                    </div>
                ))}
                {!options.length && (
                    <p className="m-0 text-[12px] italic text-ink-400">Add some options</p>
                )}
            </div>
        </div>
    );
}

/**
 * @param form     the draft being edited — body, media_items, buttons,
 *                 list_content, poll_content, variables
 * @param contact  a real contact to render against, or null for sample data
 * @param delivery the server's answer about this connection (optional)
 */
export default function WhatsAppPreview({ form, contact = null, delivery = null, className = "" }) {
    const context = contextFromContact(contact);
    const fallbacks = form.variables || {};

    const kind = contentKind(form);
    const body = renderText(form.body, context, fallbacks);
    const buttons = renderButtons(form.buttons, context, fallbacks);
    const list = renderList(form.list_content, context, fallbacks);
    const poll = renderPoll(form.poll_content, context, fallbacks);

    const degraded = delivery && delivery.native === false;

    return (
        <div className={className}>
            <div className="theme-static rounded-xl bg-[#efeae2] bg-[radial-gradient(circle_at_1px_1px,rgba(0,0,0,0.04)_1px,transparent_0)] [background-size:16px_16px] p-3">
                <div className="mb-2 flex items-center justify-between">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-500">
                        WhatsApp preview
                    </span>
                    {!contact && (
                        <span className="text-[11px] text-ink-400">Sample data</span>
                    )}
                </div>

                <div className="flex flex-col items-end gap-1.5">
                    {degraded ? (
                        // The exact text the server says will go on the wire,
                        // and only the buttons it says survive as buttons.
                        // Shown instead of the structured content, not beside
                        // it: this *is* the message on this connection, and
                        // drawing a menu the recipient will never see would be
                        // the misdescription the capability layer exists to
                        // prevent. `as_sent_buttons` is usually empty — it is
                        // non-empty only for a mix WhatsApp cannot put in one
                        // envelope, where the reply buttons still arrive
                        // tappable and the rest are in the text above them.
                        <Bubble>
                            <FormattedText text={renderText(delivery.as_sent, context, fallbacks)} />
                            <ButtonsBlock
                                buttons={renderButtons(delivery.as_sent_buttons, context, fallbacks)}
                            />
                        </Bubble>
                    ) : (
                        <>
                            {kind !== POLL && (form.media_items?.length > 0 || body) && (
                                <Bubble>
                                    <MediaBlock items={form.media_items} />
                                    {body && <FormattedText text={body} />}
                                    {kind === BUTTON && <ButtonsBlock buttons={buttons} />}
                                    {kind === LIST && <ListBlock content={list} />}
                                </Bubble>
                            )}
                            {kind === POLL && (
                                <>
                                    {(form.media_items?.length > 0 || body) && (
                                        <Bubble>
                                            <MediaBlock items={form.media_items} />
                                            {body && <FormattedText text={body} />}
                                        </Bubble>
                                    )}
                                    <Bubble>
                                        <PollBlock content={poll} />
                                    </Bubble>
                                </>
                            )}
                            {!body && !form.media_items?.length && kind === TEXT && (
                                <Bubble className="opacity-60">
                                    <span className="italic text-ink-500">Your message will appear here</span>
                                </Bubble>
                            )}
                        </>
                    )}
                </div>
            </div>

            {delivery?.note && (
                <p className="mt-2 mb-0 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900">
                    {delivery.note}
                </p>
            )}
        </div>
    );
}
