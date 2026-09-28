import { Link } from "react-router-dom";
import { Check, ChevronRight, Circle, X } from "lucide-react";
import Card, { CardHeader } from "../ui/Card";
import { IconButton } from "../ui/Button";

/**
 * The first-run checklist.
 *
 * **Every tick comes from the server**, computed from what the workspace
 * actually contains -- a connected number, a contact we are allowed to
 * message, a message that reached the provider. There is no endpoint that
 * marks a step done and nothing here writes progress, so a customer cannot
 * tick their way to an activated account and a stale browser cannot show a
 * step as finished when it is not.
 *
 * The only thing this component can change is whether it is *shown*.
 */

//: Where each step actually gets done. Keyed by the server's step key, so a
//: step added to OnboardingState.STEPS renders with a working link the moment
//: it appears in the payload -- and one without a route still renders, just
//: without a link, rather than crashing the dashboard.
const STEP_LINKS = {
    legal_accepted: { to: "/settings", label: "Review" },
    whatsapp_connected: { to: "/whatsapp", label: "Connect" },
    contacts_added: { to: "/contacts", label: "Add contacts" },
    consent_confirmed: { to: "/compliance", label: "Set up consent" },
    test_message_sent: { to: "/inbox", label: "Send a message" },
    first_campaign_created: { to: "/campaigns/new", label: "Create campaign" },
    first_campaign_sent: { to: "/campaigns", label: "Send it" },
};

export default function OnboardingChecklist({ state, onDismiss, className = "" }) {
    if (!state || state.is_complete) return null;

    const steps = state.steps || [];
    const done = steps.filter((step) => step.done).length;
    const total = steps.length || 1;
    const percent = Math.round((done / total) * 100);
    const next = state.next_step;

    return (
        <Card className={className}>
            <CardHeader
                title="Get set up"
                description={`${done} of ${total} done`}
                action={
                    onDismiss && (
                        <IconButton
                            icon={X}
                            onClick={onDismiss}
                            aria-label="Hide this checklist"
                            title="Hide this checklist"
                        />
                    )
                }
                className="mb-4"
            />

            <div
                className="h-2 rounded-full bg-ink-100 overflow-hidden mb-5"
                role="progressbar"
                aria-valuenow={percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Setup progress"
            >
                <div className="h-full rounded-full bg-brand-600 transition-all" style={{ width: `${percent}%` }} />
            </div>

            <ol className="list-none p-0 m-0 space-y-1">
                {steps.map((step) => {
                    const link = STEP_LINKS[step.key];
                    const isNext = !step.done && next?.key === step.key;

                    return (
                        <li key={step.key}>
                            <div
                                className={`flex items-center gap-3 rounded-lg px-2.5 py-2 ${
                                    isNext ? "bg-brand-50" : ""
                                }`}
                            >
                                {step.done ? (
                                    <span className="w-5 h-5 shrink-0 rounded-full bg-brand-600 text-white flex items-center justify-center">
                                        <Check size={13} strokeWidth={3} aria-hidden="true" />
                                        <span className="sr-only">Done:</span>
                                    </span>
                                ) : (
                                    <Circle
                                        size={20}
                                        className={`shrink-0 ${isNext ? "text-brand-500" : "text-ink-300"}`}
                                        aria-hidden="true"
                                    />
                                )}

                                <span
                                    className={`text-sm grow min-w-0 ${
                                        step.done
                                            ? "text-ink-400 line-through"
                                            : isNext
                                              ? "font-semibold text-ink-900"
                                              : "text-ink-700"
                                    }`}
                                >
                                    {step.label}
                                </span>

                                {!step.done && link && (
                                    <Link
                                        to={link.to}
                                        className="shrink-0 inline-flex items-center gap-0.5 text-sm font-medium text-brand-700 hover:text-brand-800 no-underline"
                                    >
                                        {link.label}
                                        <ChevronRight size={15} aria-hidden="true" />
                                    </Link>
                                )}
                            </div>
                        </li>
                    );
                })}
            </ol>
        </Card>
    );
}
