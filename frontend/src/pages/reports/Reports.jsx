import { useEffect, useState } from "react";
import { BarChart3, CheckCheck, Eye, MessageSquare, MousePointerClick, Send, TriangleAlert } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Card, { CardHeader } from "../../components/ui/Card";
import StatCard from "../../components/ui/StatCard";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import DataTable from "../../components/ui/DataTable";
import { Select } from "../../components/ui/Field";
import { StatCardSkeleton, TableSkeleton } from "../../components/ui/Skeleton";

const RANGES = [
    { id: "today", label: "Today" },
    { id: "yesterday", label: "Yesterday" },
    { id: "last_7_days", label: "Last 7 days" },
    { id: "last_30_days", label: "Last 30 days" },
    { id: "", label: "All time" },
];

// Stacked-bar series, ordered bottom-to-top. Only statuses the daily
// breakdown endpoint actually returns are charted.
const SERIES = [
    { key: "delivered", label: "Delivered", color: "#10b981" },
    { key: "read", label: "Read", color: "#3b82f6" },
    { key: "sent", label: "Sent", color: "#6ee7b7" },
    { key: "queued", label: "Queued", color: "#fbbf24" },
    { key: "failed", label: "Failed", color: "#ef4444" },
];

function formatDay(iso) {
    return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Stacked daily volume chart, drawn inline so no chart library is needed. */
function DailyVolumeChart({ rows }) {
    const totals = rows.map((row) => SERIES.reduce((sum, series) => sum + (row[series.key] || 0), 0));
    const max = Math.max(...totals, 1);

    return (
        <div>
            <div className="scroll-x">
                <div className="flex items-end gap-1.5 min-w-full h-52 px-1" style={{ minWidth: rows.length * 34 }}>
                    {rows.map((row, index) => {
                        const total = totals[index];
                        return (
                            <div key={row.date} className="flex-1 min-w-[26px] flex flex-col items-center gap-1.5 group">
                                <span className="text-[11px] font-semibold text-ink-500 tabular-nums opacity-0 group-hover:opacity-100 transition-opacity">
                                    {total}
                                </span>
                                <div
                                    className="w-full flex flex-col-reverse justify-start rounded-t-md overflow-hidden bg-ink-100"
                                    style={{ height: `${Math.max((total / max) * 100, 2)}%` }}
                                    title={`${formatDay(row.date)} — ${total} message${total === 1 ? "" : "s"}`}
                                >
                                    {SERIES.map((series) => {
                                        const value = row[series.key] || 0;
                                        if (!value) return null;
                                        return (
                                            <div
                                                key={series.key}
                                                style={{
                                                    height: `${(value / total) * 100}%`,
                                                    background: series.color,
                                                }}
                                            />
                                        );
                                    })}
                                </div>
                                <span className="text-[10px] text-ink-400 whitespace-nowrap">{formatDay(row.date)}</span>
                            </div>
                        );
                    })}
                </div>
            </div>

            <div className="flex flex-wrap gap-x-4 gap-y-2 mt-5 pt-4 border-t border-ink-100">
                {SERIES.map((series) => (
                    <span key={series.key} className="inline-flex items-center gap-1.5 text-xs font-medium text-ink-600">
                        <span className="w-2.5 h-2.5 rounded-sm" style={{ background: series.color }} />
                        {series.label}
                    </span>
                ))}
            </div>
        </div>
    );
}

export default function Reports() {
    const [range, setRange] = useState("last_30_days");
    const [metrics, setMetrics] = useState(null);
    const [daily, setDaily] = useState([]);
    // Across every campaign, not filtered by the range picker: the tracking
    // summary has no date filter, and quietly showing all-time numbers under
    // a "last 7 days" heading would be a lie.
    const [links, setLinks] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    useEffect(() => {
        let cancelled = false;
        setLoading(true);

        const params = range ? { range } : {};
        Promise.all([
            api.get("/reports/dashboard/", { params }),
            api.get("/reports/messages/daily/", { params }),
        ])
            .then(([metricsRes, dailyRes]) => {
                if (cancelled) return;
                setMetrics(metricsRes.data);
                setDaily(dailyRes.data || []);
                setError("");

                // Its own request and its own failure: the rest of the page
                // is still worth reading without it.
                api.get("/tracking/links/summary/")
                    .then((response) => {
                        if (!cancelled) setLinks(response.data.links || []);
                    })
                    .catch(() => {
                        if (!cancelled) setLinks([]);
                    });
            })
            .catch((err) => {
                if (!cancelled) setError(apiErrorMessage(err, "Unable to load reports."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [range]);

    const breakdownColumns = [
        { key: "date", header: "Date", render: (row) => <span className="font-medium text-ink-900">{formatDay(row.date)}</span> },
        ...SERIES.map((series) => ({
            key: series.key,
            header: series.label,
            align: "right",
            hideBelow: series.key === "queued" || series.key === "sent" ? "sm" : undefined,
            render: (row) => <span className="tabular-nums">{row[series.key] || 0}</span>,
        })),
        {
            key: "total",
            header: "Total",
            align: "right",
            render: (row) => (
                <span className="tabular-nums font-semibold text-ink-900">
                    {SERIES.reduce((sum, series) => sum + (row[series.key] || 0), 0)}
                </span>
            ),
        },
    ];

    return (
        <div>
            <PageHeader
                title="Reports"
                description="Message delivery performance across your workspace."
                actions={
                    <Select
                        value={range}
                        onChange={(event) => setRange(event.target.value)}
                        aria-label="Reporting period"
                        className="min-w-44"
                    >
                        {RANGES.map((item) => (
                            <option key={item.id || "all"} value={item.id}>
                                {item.label}
                            </option>
                        ))}
                    </Select>
                }
            />

            <Alert>{error}</Alert>

            {loading ? (
                <div className="space-y-4">
                    <StatCardSkeleton count={4} />
                    <TableSkeleton rows={6} columns={6} />
                </div>
            ) : (
                <>
                    {metrics && (
                        <>
                            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                                <StatCard label="Messages sent" value={metrics.sent} icon={Send} tone="brand" />
                                <StatCard
                                    label="Delivered"
                                    value={metrics.delivered}
                                    hint={`${metrics.delivery_rate}% of sent`}
                                    icon={CheckCheck}
                                    tone="brand"
                                />
                                <StatCard
                                    label="Read"
                                    value={metrics.read}
                                    hint={`${metrics.read_rate}% of sent`}
                                    icon={Eye}
                                    tone="blue"
                                />
                                <StatCard label="Failed" value={metrics.failed} icon={TriangleAlert} tone="red" />
                            </div>

                            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                                <StatCard
                                    label="Total outbound"
                                    value={metrics.messages_total}
                                    icon={BarChart3}
                                    tone="violet"
                                />
                                <StatCard
                                    label="Replies received"
                                    value={metrics.replies}
                                    icon={MessageSquare}
                                    tone="blue"
                                    to="/inbox"
                                />
                                <StatCard
                                    label="Active campaigns"
                                    value={metrics.active_campaigns}
                                    hint={`${metrics.total_campaigns} total`}
                                    icon={Send}
                                    tone="brand"
                                    to="/campaigns"
                                />
                                <StatCard
                                    label="Active automations"
                                    value={
                                        metrics.active_automation_rules +
                                        metrics.active_follow_up_rules +
                                        metrics.active_workflows
                                    }
                                    hint={`${metrics.active_automation_rules} rules · ${metrics.active_follow_up_rules} follow-ups · ${metrics.active_workflows} workflows`}
                                    icon={BarChart3}
                                    tone="amber"
                                    to="/automation"
                                />
                            </div>
                        </>
                    )}

                    <Card className="mb-6">
                        <CardHeader
                            title="Daily message volume"
                            description="Outbound messages grouped by the day they were queued."
                            className="mb-5"
                        />
                        {daily.length === 0 ? (
                            <p className="text-sm text-ink-500 py-8 text-center m-0">
                                No messages were queued in this period.
                            </p>
                        ) : (
                            <DailyVolumeChart rows={daily} />
                        )}
                    </Card>

                    {links.length > 0 && (
                        <Card className="mb-6">
                            <CardHeader
                                title="Top links"
                                description="Every campaign, all time — which links people actually tapped."
                                className="mb-4"
                            />
                            <ul className="list-none p-0 m-0 space-y-2.5">
                                {links.slice(0, 10).map((link) => (
                                    <li key={link.url} className="flex items-center gap-3">
                                        <span className="shrink-0 w-8 h-8 rounded-lg bg-violet-50 text-violet-600 flex items-center justify-center">
                                            <MousePointerClick size={15} aria-hidden="true" />
                                        </span>
                                        <div className="min-w-0 flex-1">
                                            <a
                                                href={link.url}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="text-sm font-semibold text-ink-900 hover:text-brand-700 hover:underline truncate block"
                                            >
                                                {link.url}
                                            </a>
                                            <div className="mt-1 h-1.5 rounded-full bg-ink-100 overflow-hidden">
                                                <div
                                                    className="h-full bg-violet-500 rounded-full"
                                                    style={{ width: `${Math.min(link.click_rate, 100)}%` }}
                                                />
                                            </div>
                                        </div>
                                        <div className="shrink-0 text-right">
                                            <p className="text-sm font-bold text-ink-900 m-0 tabular-nums">
                                                {link.contacts_clicked}
                                            </p>
                                            <p className="text-[11px] text-ink-500 m-0 tabular-nums">
                                                {link.click_rate}% of {link.recipients}
                                            </p>
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        </Card>
                    )}

                    <div>
                        <h2 className="text-base font-bold text-ink-900 mb-3">Daily breakdown</h2>
                        <DataTable
                            columns={breakdownColumns}
                            rows={daily}
                            rowKey={(row) => row.date}
                            empty={
                                <EmptyState
                                    icon={BarChart3}
                                    title="Nothing to report yet"
                                    description="Once campaigns start sending, daily delivery figures appear here."
                                />
                            }
                        />
                    </div>
                </>
            )}
        </div>
    );
}
