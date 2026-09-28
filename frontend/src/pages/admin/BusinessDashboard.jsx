import { useEffect, useState } from "react";
import {
    Activity,
    AlertTriangle,
    ArrowDownRight,
    ArrowUpRight,
    HelpCircle,
    Repeat,
    TrendingUp,
    Users,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useBranding } from "../../context/BrandingContext";
import { usePageTitle } from "../../hooks/useSeo";
import { formatMoney } from "../../utils/plans";
import Alert from "../../components/ui/Alert";
import Card, { CardHeader } from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";

const WINDOWS = [7, 30, 90];

/**
 * The business side of the operations dashboard: revenue, activation,
 * retention and the workspaces that need a human.
 *
 * **Every metric renders its own definition**, on the little "?" beside it,
 * from the `definitions` block the API returns. That is not decoration. The
 * first argument anybody has about a churn number is what counts as churn, and
 * the fastest way to end it is for the number to carry the answer. It also
 * means the definition cannot drift from the implementation: both come from
 * apps/reports/business.py, in the same response.
 *
 * This does not replace the Phase 5 operations dashboard, which still reports
 * queues, workers, dead letters and backups. Revenue and reliability are
 * different questions asked by the same person at different moments, and
 * merging them would produce one screen that answers neither well.
 */
function BusinessDashboard() {
    const { branding } = useBranding();
    usePageTitle(`Business · ${branding.product_name}`);

    const [days, setDays] = useState(30);
    const [data, setData] = useState(null);
    const [health, setHealth] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError("");

        Promise.all([
            api.get("/reports/business/", { params: { days } }),
            // Its own request and its own failure: the revenue picture is still
            // worth reading if the health signals cannot be gathered.
            api.get("/reports/business/health/").catch(() => null),
        ])
            .then(([businessRes, healthRes]) => {
                if (cancelled) return;
                setData(businessRes.data);
                setHealth(healthRes?.data || null);
            })
            .catch((err) => {
                if (!cancelled) setError(apiErrorMessage(err, "The business dashboard could not be loaded."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [days]);

    const definitions = data?.definitions || {};
    const revenue = data?.revenue || {};
    const activation = data?.activation || {};
    const retention = data?.retention || {};
    const volume = data?.volume || {};
    const currency = revenue.currency || "INR";

    return (
        <div>
            <PageHeader
                title="Business"
                description="Revenue, activation and retention across the whole deployment."
                actions={
                    <div className="flex gap-1 rounded-lg border border-ink-200 bg-surface p-1">
                        {WINDOWS.map((option) => (
                            <button
                                key={option}
                                type="button"
                                onClick={() => setDays(option)}
                                className={`px-3 py-1.5 rounded-md text-sm font-medium ${
                                    days === option
                                        ? "bg-ink-900 text-ink-50"
                                        : "text-ink-600 hover:bg-ink-50"
                                }`}
                            >
                                {option}d
                            </button>
                        ))}
                    </div>
                }
            />

            <Alert>{error}</Alert>
            {loading && <Spinner />}

            {!loading && data && (
                <>
                    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-6">
                        <Metric
                            label="MRR"
                            value={formatMoney(revenue.mrr_minor, currency)}
                            hint={`${formatMoney(revenue.arr_minor, currency)} ARR`}
                            icon={TrendingUp}
                            definition={definitions.mrr}
                        />
                        <Metric
                            label="Paying customers"
                            value={num(revenue.paying_customers)}
                            hint={`${num(revenue.trialing_customers)} on trial`}
                            icon={Users}
                            definition={definitions.paying_customers}
                        />
                        <Metric
                            label="ARPU"
                            value={formatMoney(revenue.arpu_minor, currency)}
                            icon={Activity}
                            definition={definitions.arpu}
                        />
                        <Metric
                            label="Churn"
                            value={
                                revenue.churn_rate === null || revenue.churn_rate === undefined
                                    ? "—"
                                    : `${revenue.churn_rate}%`
                            }
                            hint={`${num(revenue.churned)} of ${num(revenue.churn_base)}`}
                            icon={ArrowDownRight}
                            definition={definitions.churn_rate}
                        />
                    </div>

                    <div className="grid gap-6 lg:grid-cols-2 mb-6">
                        <Card>
                            <CardHeader
                                title="Money in the window"
                                description={`Last ${data.window_days} days.`}
                                className="mb-4"
                            />
                            <dl className="m-0 space-y-2.5">
                                <Line
                                    label="New subscriptions"
                                    value={num(revenue.new_subscriptions)}
                                    definition={definitions.new_subscriptions}
                                />
                                <Line label="Cancellations" value={num(revenue.cancellations)} />
                                <Line label="Expirations" value={num(revenue.expirations)} />
                                <Line
                                    label="Revenue collected"
                                    value={formatMoney(revenue.revenue_collected_minor, currency)}
                                    definition={definitions.revenue_collected}
                                />
                                <Line
                                    label="Failed payments"
                                    value={num(revenue.failed_payments)}
                                    tone={revenue.failed_payments ? "bad" : undefined}
                                    definition={definitions.failed_payments}
                                />
                            </dl>
                            {/*
                              Said on the screen, not only in the docs. Cash and
                              MRR are different numbers and an annual plan makes
                              them look wildly inconsistent for a month.
                            */}
                            <p className="text-xs text-ink-400 mt-4 mb-0">
                                Revenue collected is cash that arrived in this window. MRR above is
                                the contracted run rate. A yearly plan sold today moves the first by
                                twelve months and the second by one.
                            </p>
                        </Card>

                        <Card>
                            <CardHeader
                                title="By plan"
                                description="Where the run rate comes from."
                                className="mb-4"
                            />
                            {(revenue.by_plan || []).length === 0 ? (
                                <p className="text-sm text-ink-400 m-0">No paying subscriptions yet.</p>
                            ) : (
                                <dl className="m-0 space-y-2.5">
                                    {revenue.by_plan.map((row) => (
                                        <Line
                                            key={row.plan}
                                            label={`${row.plan} (${row.subscriptions})`}
                                            value={formatMoney(row.mrr_minor, currency)}
                                        />
                                    ))}
                                </dl>
                            )}
                        </Card>
                    </div>

                    <div className="grid gap-6 lg:grid-cols-2 mb-6">
                        <Card>
                            <CardHeader
                                title="Activation"
                                description="Workspaces created in this window."
                                className="mb-4"
                            />
                            <Funnel
                                rows={[
                                    ["Signed up", activation.signups],
                                    ["Connected WhatsApp", activation.connected_whatsapp],
                                    ["Added contacts", activation.added_contacts],
                                    ["Sent a message", activation.sent_first_message],
                                    ["Created a campaign", activation.created_first_campaign],
                                    ["Sent a campaign", activation.sent_first_campaign],
                                    ["Activated", activation.activated],
                                ]}
                            />
                            <p className="text-xs text-ink-400 mt-4 mb-0">
                                {definitions.activation_rate}
                            </p>
                        </Card>

                        <Card>
                            <CardHeader
                                title="Retention"
                                description="Active means sent something, not signed in."
                                className="mb-4"
                            />
                            <dl className="m-0 space-y-2.5">
                                <Line label="New customers" value={num(retention.new_customers)} icon={ArrowUpRight} />
                                <Line label="Active customers" value={num(retention.active_customers)} />
                                <Line label="Returning" value={num(retention.returning_customers)} icon={Repeat} />
                                <Line
                                    label="Lapsed"
                                    value={num(retention.lapsed_customers)}
                                    tone={retention.lapsed_customers ? "bad" : undefined}
                                />
                                <Line
                                    label="Retention rate"
                                    value={
                                        retention.retention_rate === null ||
                                        retention.retention_rate === undefined
                                            ? "—"
                                            : `${retention.retention_rate}%`
                                    }
                                />
                            </dl>
                            <p className="text-xs text-ink-400 mt-4 mb-0">
                                Lapsed means they sent in the previous window and not in this one.
                                That is not churn — they may still be paying.
                            </p>
                        </Card>
                    </div>

                    <Card className="mb-6">
                        <CardHeader
                            title="Volume"
                            description={`Campaigns and messages over the last ${data.window_days} days.`}
                            className="mb-4"
                        />
                        <div className="grid grid-cols-2 sm:grid-cols-5 gap-4">
                            <Stat label="Campaigns" value={num(volume.campaigns)} />
                            <Stat label="Messages" value={num(volume.messages)} />
                            <Stat label="Sent" value={num(volume.sent)} />
                            <Stat label="Failed" value={num(volume.failed)} tone={volume.failed ? "bad" : undefined} />
                            <Stat
                                label="Failure rate"
                                value={
                                    volume.failure_rate === null || volume.failure_rate === undefined
                                        ? "—"
                                        : `${volume.failure_rate}%`
                                }
                            />
                        </div>
                    </Card>

                    {health && <HealthSignals health={health} />}
                </>
            )}
        </div>
    );
}

/**
 * The workspaces worth a person looking at.
 *
 * Read-only, and nothing here sends anything. Appearing on this list is a
 * prompt for somebody to decide what to do, not a trigger for an automated
 * email -- see docs/METRICS.md on why reactivation is a decision.
 */
function HealthSignals({ health }) {
    const groups = [
        ["never_activated", "Signed up, never activated", "The most fixable failure in the product."],
        ["gone_quiet", "Were sending, stopped", "Different from never starting."],
        ["device_disconnected", "Device disconnected", "Their campaigns will not send."],
        ["trial_ending", "Trial ending within 3 days", ""],
        ["repeated_failures", "Repeated send failures", "20 or more failures in the last week."],
    ];

    return (
        <Card>
            <CardHeader
                title="Needs a look"
                description="Signals with something a person could actually do about them."
                className="mb-4"
            />
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {groups.map(([key, title, note]) => {
                    const rows = health.signals?.[key] || [];
                    return (
                        <div key={key}>
                            <h3 className="text-sm font-bold text-ink-900 m-0 mb-1 flex items-center gap-1.5">
                                {rows.length > 0 && (
                                    <AlertTriangle size={14} className="text-amber-600" aria-hidden="true" />
                                )}
                                {title}
                                <span className="text-ink-400 font-normal">({rows.length})</span>
                            </h3>
                            {note && <p className="text-xs text-ink-400 m-0 mb-2">{note}</p>}
                            {rows.length === 0 ? (
                                <p className="text-xs text-ink-400 m-0">Nothing here.</p>
                            ) : (
                                <ul className="list-none p-0 m-0 space-y-1">
                                    {rows.slice(0, 8).map((row, index) => (
                                        <li key={`${row.tenant}-${index}`} className="text-xs text-ink-600">
                                            {row.name || `Workspace ${row.tenant}`}
                                            {row.device && <span className="text-ink-400"> · {row.device}</span>}
                                            {row.failures_7d && (
                                                <span className="text-ink-400"> · {row.failures_7d} failures</span>
                                            )}
                                            {row.days_remaining !== undefined && (
                                                <span className="text-ink-400"> · {row.days_remaining}d left</span>
                                            )}
                                        </li>
                                    ))}
                                    {rows.length > 8 && (
                                        <li className="text-xs text-ink-400">and {rows.length - 8} more</li>
                                    )}
                                </ul>
                            )}
                        </div>
                    );
                })}
            </div>
        </Card>
    );
}

function num(value) {
    return Number(value || 0).toLocaleString();
}

function Metric({ label, value, hint, icon: Icon, definition }) {
    return (
        <Card>
            <div className="flex items-start justify-between gap-2">
                <p className="text-xs font-medium text-ink-500 m-0 flex items-center gap-1">
                    {label}
                    {definition && <Definition text={definition} />}
                </p>
                {Icon && <Icon size={16} className="text-ink-300 shrink-0" aria-hidden="true" />}
            </div>
            <p className="text-2xl font-bold text-ink-900 m-0 mt-2">{value}</p>
            {hint && <p className="text-xs text-ink-400 m-0 mt-1">{hint}</p>}
        </Card>
    );
}

function Line({ label, value, hint, tone, icon: Icon, definition }) {
    return (
        <div className="flex items-baseline justify-between gap-4 text-sm">
            <dt className="text-ink-500 m-0 flex items-center gap-1.5">
                {Icon && <Icon size={14} className="text-ink-300" aria-hidden="true" />}
                {label}
                {definition && <Definition text={definition} />}
            </dt>
            <dd className={`m-0 font-bold ${tone === "bad" ? "text-red-600" : "text-ink-900"}`}>
                {value}
                {hint && <span className="text-ink-400 font-normal"> {hint}</span>}
            </dd>
        </div>
    );
}

function Stat({ label, value, tone }) {
    return (
        <div>
            <p className="text-xs text-ink-500 m-0">{label}</p>
            <p className={`text-xl font-bold m-0 mt-0.5 ${tone === "bad" ? "text-red-600" : "text-ink-900"}`}>
                {value}
            </p>
        </div>
    );
}

/** The definition, on the tooltip, straight from the API response. */
function Definition({ text }) {
    return (
        <span title={text} className="cursor-help text-ink-300">
            <HelpCircle size={12} aria-label={text} />
        </span>
    );
}

function Funnel({ rows }) {
    const top = Math.max(...rows.map(([, value]) => Number(value || 0)), 1);
    return (
        <div className="space-y-2">
            {rows.map(([label, value]) => {
                const count = Number(value || 0);
                return (
                    <div key={label}>
                        <div className="flex items-baseline justify-between gap-4 text-sm mb-1">
                            <span className="text-ink-600">{label}</span>
                            <span className="font-bold text-ink-900">{num(count)}</span>
                        </div>
                        <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
                            <div
                                className="h-full bg-brand-500 rounded-full"
                                style={{ width: `${(count / top) * 100}%` }}
                            />
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

export default BusinessDashboard;
