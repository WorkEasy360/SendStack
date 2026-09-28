import { Check, Coins, MessageSquareText, ShieldCheck, Smartphone, Users } from "lucide-react";
import Alert from "../ui/Alert";

/**
 * What this campaign will actually do, shown before anybody presses Launch.
 *
 * It replaced a one-line "estimated N contacts", which was the wrong number to
 * put in front of somebody about to spend credits: it counted the contacts the
 * audience rule selected, and said nothing about how many of them would be
 * refused, why, or what the send would cost.
 *
 * **Every number here comes from `GET /campaigns/campaigns/:id/preview/`.**
 * Nothing is computed in this component -- not the eligible count, not the
 * credits, not whether the send can go ahead. The server produces them from
 * the same compliance, quota and credit rules the send path runs, so the
 * split shown here is the split that will happen. A frontend that did its own
 * arithmetic would eventually disagree with the send, and the customer would
 * find out after being charged.
 *
 * The skipped rows are given as much prominence as the eligible one on
 * purpose. "38 people asked not to hear from you and are not included" is not
 * an error message -- it is the product doing its job, and it is worth seeing.
 */
function SendPreview({ preview, loading }) {
    if (loading) {
        return (
            <div className="rounded-card border border-ink-200 bg-surface p-5 mb-6">
                <p className="text-sm text-ink-400 m-0">Working out who is eligible…</p>
            </div>
        );
    }
    if (!preview) return null;

    const {
        selected, eligible, skipped = [], credits, plan, devices,
        can_send: canSend, blockers = [], delivery,
    } = preview;
    const about = preview.exact === false;

    return (
        <div className="rounded-card border border-ink-200 bg-surface overflow-hidden mb-6">
            <div className="px-5 py-3 border-b border-ink-200 bg-ink-50 flex items-center gap-2">
                <ShieldCheck size={16} className="text-brand-600" aria-hidden="true" />
                <h2 className="text-sm font-bold text-ink-900 m-0">Before you send</h2>
            </div>

            <div className="p-5">
                <dl className="m-0 space-y-2.5">
                    <Row
                        icon={Users}
                        label="Contacts selected"
                        value={format(selected, about)}
                    />
                    <Row
                        icon={Check}
                        label="Eligible to receive"
                        value={format(eligible, about)}
                        tone="good"
                        emphasis
                    />
                    {skipped.map((item) => (
                        <Row
                            key={item.reason}
                            label={item.label}
                            value={`${format(item.count, about)} — skipped`}
                            tone="muted"
                            detail={item.detail}
                        />
                    ))}
                    {credits?.tracked && (
                        <Row
                            icon={Coins}
                            label="Credits this will use"
                            value={format(credits.needed, about)}
                            detail={
                                credits.available !== null
                                    ? `${credits.available.toLocaleString()} available`
                                    : ""
                            }
                            tone={credits.sufficient ? undefined : "bad"}
                        />
                    )}
                    <Row
                        icon={Smartphone}
                        label="Connected numbers"
                        value={`${devices?.connected ?? 0} of ${devices?.total ?? 0}`}
                        tone={devices?.ready ? undefined : "bad"}
                    />
                    {delivery?.capability && (
                        <Row
                            icon={MessageSquareText}
                            label={`${KIND_LABEL[delivery.content_kind] || "Message"} delivery`}
                            value={
                                delivery.native
                                    ? "Shown natively"
                                    : delivery.flattened
                                      ? "Sent as a numbered list"
                                      : "Buttons kept, extras sent as text"
                            }
                            tone={delivery.native ? undefined : "muted"}
                            detail={
                                // Named only when a *number* is the reason.
                                // A mixed button list is reshaped by WhatsApp's
                                // message format on every connection, so
                                // listing every number would read as a fault
                                // in each of them.
                                delivery.native || !delivery.flattened
                                    ? ""
                                    : `${delivery.degraded_accounts.map((account) => account.name).join(", ")} cannot show this`
                            }
                        />
                    )}
                </dl>

                {/* Never a blocker. Every content kind is deliverable on every
                    connection -- a menu a number cannot draw is sent as a
                    numbered list the recipient can still answer, and the reply
                    maps back to the same row id. Refusing the send would be
                    worse than the degradation it avoided, so this says what
                    will happen and lets the customer decide. */}
                {delivery?.capability && !delivery.native && (
                    <div className="mt-4 rounded-lg border border-ink-200 bg-ink-50 p-3.5">
                        <p className="m-0 mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-500">
                            What your contacts will receive
                        </p>
                        <pre className="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-ink-700">
                            {delivery.as_sent}
                        </pre>
                        {delivery.flattened ? (
                            <p className="mt-2 mb-0 text-xs text-ink-500">
                                Replies like <span className="font-semibold">1</span> are matched back to the
                                option they chose, so automation works either way.
                            </p>
                        ) : (
                            <p className="mt-2 mb-0 text-xs text-ink-500">
                                The reply buttons are still tappable underneath this text &mdash; WhatsApp
                                just has no message that carries two kinds of button at once.
                            </p>
                        )}
                    </div>
                )}

                {about && (
                    <p className="text-xs text-ink-400 mt-4 mb-0">
                        This audience is large enough that the consent split is estimated from a
                        sample. The send itself checks every contact individually.
                    </p>
                )}

                {skipped.length > 0 && (
                    <div className="mt-4 rounded-lg bg-brand-50 border border-brand-200 px-3.5 py-3 flex gap-2.5">
                        <ShieldCheck size={16} className="text-brand-700 shrink-0 mt-0.5" aria-hidden="true" />
                        <p className="text-xs text-brand-900 m-0 leading-relaxed">
                            Skipped contacts are not messaged and not charged for. The same rules run
                            again on the server when you send.
                        </p>
                    </div>
                )}

                {!canSend && blockers.length > 0 && (
                    // Every blocker is a sentence written by the server, rendered
                    // as-is. Mapping codes to copy here would be a second place
                    // for the wording to drift out of step with the rule.
                    <div className="mt-4">
                        <Alert tone="warning" title="This campaign cannot be sent yet">
                            <ul className="list-disc pl-5 m-0 space-y-1">
                                {blockers.map((blocker) => (
                                    <li key={blocker} className="text-sm">{blocker}</li>
                                ))}
                            </ul>
                        </Alert>
                    </div>
                )}

                {canSend && (
                    <p className="text-sm text-brand-700 font-medium mt-4 mb-0 flex items-center gap-1.5">
                        <Check size={16} aria-hidden="true" />
                        Ready to send.
                    </p>
                )}
            </div>
        </div>
    );
}

const KIND_LABEL = { BUTTON: "Button", LIST: "Menu", POLL: "Poll" };

function format(value, about) {
    const number = Number(value || 0).toLocaleString();
    return about ? `about ${number}` : number;
}

function Row({ icon: Icon, label, value, detail, tone, emphasis }) {
    const valueClass =
        tone === "good"
            ? "text-brand-700 font-bold"
            : tone === "bad"
              ? "text-red-600 font-bold"
              : tone === "muted"
                ? "text-ink-400"
                : "text-ink-900 font-medium";

    return (
        <div className={`flex items-start justify-between gap-4 ${emphasis ? "py-1" : ""}`}>
            <div className="flex items-start gap-2 min-w-0">
                {Icon ? (
                    <Icon size={15} className="text-ink-400 shrink-0 mt-0.5" aria-hidden="true" />
                ) : (
                    <span className="w-[15px] shrink-0" aria-hidden="true" />
                )}
                <div className="min-w-0">
                    <dt className="text-sm text-ink-600 m-0">{label}</dt>
                    {detail && <dd className="text-xs text-ink-400 m-0 mt-0.5">{detail}</dd>}
                </div>
            </div>
            <dd className={`text-sm m-0 shrink-0 ${valueClass}`}>{value}</dd>
        </div>
    );
}

export default SendPreview;
