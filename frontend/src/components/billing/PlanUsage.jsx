import { Link } from "react-router-dom";
import { Contact, Send, Smartphone, Users } from "lucide-react";
import Alert from "../ui/Alert";
import Card, { CardHeader } from "../ui/Card";
import { useEntitlements } from "../../context/EntitlementsContext";

/**
 * Messages, contacts, devices and team members against what the plan allows.
 *
 * Four meters rather than four StatCards: the number on its own ("4,200
 * messages") does not tell anybody whether they are about to run out, and
 * running out is the only reason this panel exists.
 *
 * Warnings appear at 80%, 90% and 100%. They are a courtesy, not a control --
 * the backend refuses the 10,001st message whether or not this panel ever
 * rendered.
 */

const METRICS = [
    { key: "messages", label: "Messages", icon: Send, note: "this billing period" },
    { key: "contacts", label: "Contacts", icon: Contact },
    { key: "devices", label: "WhatsApp devices", icon: Smartphone },
    { key: "users", label: "Team members", icon: Users },
];

const WARN_AT = 80;
const URGENT_AT = 90;

function toneFor(percentage) {
    if (percentage >= 100) return "full";
    if (percentage >= URGENT_AT) return "urgent";
    if (percentage >= WARN_AT) return "warn";
    return "ok";
}

const BAR = {
    ok: "bg-brand-500",
    warn: "bg-amber-500",
    urgent: "bg-orange-500",
    full: "bg-red-500",
};

const number = (value) => (typeof value === "number" ? value.toLocaleString() : "—");

function Meter({ metric, block }) {
    const Icon = metric.icon;
    const unlimited = block.unlimited;
    const percentage = unlimited ? 0 : block.percentage ?? 0;
    const tone = toneFor(percentage);

    return (
        <div>
            <div className="flex items-baseline justify-between gap-3 mb-1.5">
                <span className="flex items-center gap-1.5 text-sm font-medium text-ink-700 min-w-0">
                    <Icon size={15} className="shrink-0 text-ink-400" aria-hidden="true" />
                    <span className="truncate">{metric.label}</span>
                </span>
                <span className="shrink-0 text-sm tabular-nums text-ink-500">
                    <span className="text-ink-900 font-semibold">{number(block.used)}</span>
                    {unlimited ? (
                        <span className="text-ink-400"> / unlimited</span>
                    ) : (
                        <span className="text-ink-400"> / {number(block.limit)}</span>
                    )}
                </span>
            </div>
            <div
                className="h-1.5 w-full rounded-full bg-ink-100 overflow-hidden"
                role="progressbar"
                aria-valuenow={unlimited ? 0 : percentage}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`${metric.label} used`}
            >
                {!unlimited && (
                    <div
                        className={`h-full rounded-full transition-[width] duration-500 ${BAR[tone]}`}
                        style={{ width: `${Math.min(Math.max(percentage, 0), 100)}%` }}
                    />
                )}
            </div>
            {metric.note && !unlimited && (
                <p className="text-xs text-ink-400 mt-1 mb-0">{metric.note}</p>
            )}
        </div>
    );
}

function MeterSkeleton() {
    return (
        <div>
            <div className="flex items-center justify-between gap-3 mb-1.5">
                <div className="h-4 w-28 rounded bg-ink-100 animate-pulse" />
                <div className="h-4 w-16 rounded bg-ink-100 animate-pulse" />
            </div>
            <div className="h-1.5 w-full rounded-full bg-ink-100" />
        </div>
    );
}

/** The one-line nudge above the meters, at 80% / 90% / 100% of anything. */
function warningFor(usage, planName) {
    const worst = METRICS.map((metric) => ({ metric, block: usage[metric.key] }))
        .filter(({ block }) => block && !block.unlimited)
        .sort((a, b) => (b.block.percentage ?? 0) - (a.block.percentage ?? 0))[0];

    if (!worst || (worst.block.percentage ?? 0) < WARN_AT) return null;

    const label = worst.metric.label.toLowerCase();
    const plan = planName ? `your ${planName} plan` : "your plan";

    if (worst.block.percentage >= 100) {
        return {
            tone: "error",
            title: `You have reached your ${label} limit.`,
            body:
                worst.metric.key === "messages"
                    ? `Sending stops until the next billing period, or until ${plan} is upgraded.`
                    : `Upgrade ${plan} to add more.`,
        };
    }
    return {
        tone: "warning",
        title: `You are using ${worst.block.percentage}% of your ${label}.`,
        body: `${worst.block.remaining?.toLocaleString()} left on ${plan}.`,
    };
}

export default function PlanUsage({ className = "" }) {
    const { usage, plan, subscription, loading, error, planName } = useEntitlements();

    if (loading) {
        return (
            <Card className={className}>
                <CardHeader title="Plan usage" className="mb-4" />
                <div className="grid gap-4 sm:grid-cols-2">
                    {METRICS.map((metric) => (
                        <MeterSkeleton key={metric.key} />
                    ))}
                </div>
            </Card>
        );
    }

    // No plan is not an error and not an empty state -- it is most of this
    // product's history. Nothing to meter, so nothing to show.
    if (error || !usage || !plan) return null;

    const warning = warningFor(usage, planName);
    const lapsed = subscription?.access === "EXPIRED";
    const inGrace = subscription?.access === "GRACE";

    return (
        <Card className={className}>
            <CardHeader
                title="Plan usage"
                description={
                    subscription?.current_period_end
                        ? `${plan.name} · resets ${new Date(subscription.current_period_end).toLocaleDateString(
                              undefined,
                              { month: "short", day: "numeric" }
                          )}`
                        : plan.name
                }
                action={
                    <Link
                        to="/billing"
                        className="text-sm font-medium text-brand-700 hover:text-brand-800 no-underline"
                    >
                        Plans
                    </Link>
                }
                className="mb-4"
            />

            {lapsed && (
                <Alert tone="error" title="This workspace's subscription has ended.">
                    Sending, imports and new devices are paused until it is renewed. Nothing has been deleted.
                </Alert>
            )}
            {!lapsed && inGrace && (
                <Alert tone="warning" title="This workspace's subscription is past due.">
                    Everything still works for now. Renewing keeps it that way.
                </Alert>
            )}
            {!lapsed && !inGrace && warning && (
                <Alert tone={warning.tone} title={warning.title}>
                    {warning.body}
                </Alert>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
                {METRICS.map((metric) =>
                    usage[metric.key] ? (
                        <Meter key={metric.key} metric={metric} block={usage[metric.key]} />
                    ) : null
                )}
            </div>
        </Card>
    );
}
