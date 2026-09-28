import { useEffect, useMemo, useState } from "react";
import {
    AlertTriangle,
    Bot,
    CheckCircle2,
    Clock,
    Inbox,
    MessageSquare,
    Send,
    Sparkles,
    UserCheck,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import EmptyState from "../../components/ui/EmptyState";
import { Select } from "../../components/ui/Field";
import Spinner from "../../components/ui/Spinner";
import StatCard from "../../components/ui/StatCard";

/**
 * The Chatbot dashboard.
 *
 * Every figure comes from `/chatbot/overview/`, which is one call returning one
 * consistent set of numbers. A dozen endpoints rendering one card row would be
 * a dozen chances for the tiles to disagree with each other by a few seconds.
 */

const WINDOWS = [
    { days: 7, label: "Last 7 days" },
    { days: 30, label: "Last 30 days" },
    { days: 90, label: "Last 90 days" },
];

function seconds(value) {
    if (!value) return "—";
    if (value < 60) return `${value.toFixed(1)}s`;
    return `${Math.round(value / 60)}m`;
}

export default function ChatbotOverview({ onOpenTab, bots = [] }) {
    const [days, setDays] = useState(30);
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        api.get("/chatbot/overview/", { params: { days } })
            .then((response) => {
                if (cancelled) return;
                setData(response.data);
                setError("");
            })
            .catch((requestError) => {
                if (!cancelled) setError(apiErrorMessage(requestError, "Could not load the chatbot dashboard."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [days]);

    const published = useMemo(() => bots.filter((bot) => bot.status === "PUBLISHED"), [bots]);

    if (loading && !data) {
        return (
            <div className="flex justify-center py-20">
                <Spinner />
            </div>
        );
    }

    if (error) {
        return <Alert tone="error">{error}</Alert>;
    }

    if (!bots.length) {
        return (
            <EmptyState
                icon={Bot}
                title="No chatbots yet"
                description="A chatbot answers customers on one of your WhatsApp numbers — with rules you write, a flow you draw, or your AI agent."
                action={
                    <Button icon={Bot} onClick={() => onOpenTab("bots")}>
                        Create your first chatbot
                    </Button>
                }
            />
        );
    }

    return (
        <div>
            <div className="flex items-center justify-between gap-3 mb-5">
                <div className="flex items-center gap-2 text-sm text-ink-500">
                    {published.length > 0 ? (
                        <>
                            <CheckCircle2 size={16} className="text-brand-600" aria-hidden="true" />
                            {published.length} live {published.length === 1 ? "bot" : "bots"}
                        </>
                    ) : (
                        <>
                            <AlertTriangle size={16} className="text-amber-500" aria-hidden="true" />
                            Nothing is published yet, so no customer is being answered.
                        </>
                    )}
                </div>
                <div className="w-44">
                    <Select value={days} onChange={(event) => setDays(Number(event.target.value))} aria-label="Time range">
                        {WINDOWS.map((entry) => (
                            <option key={entry.days} value={entry.days}>
                                {entry.label}
                            </option>
                        ))}
                    </Select>
                </div>
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                <StatCard label="Conversations" value={data.total_conversations} icon={MessageSquare} tone="brand" />
                <StatCard label="Active now" value={data.active_conversations} icon={Inbox} tone="blue" />
                <StatCard label="Unread" value={data.unread_conversations} icon={Inbox} tone="amber" />
                <StatCard label="Today" value={data.today_conversations} icon={Clock} />
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                <StatCard label="Messages received" value={data.messages_received} icon={Inbox} />
                <StatCard label="Messages sent" value={data.messages_sent} icon={Send} />
                <StatCard label="AI responses" value={data.ai_responses} icon={Sparkles} tone="violet" />
                <StatCard label="Human handoffs" value={data.human_handoffs} icon={UserCheck} tone="amber" />
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                <StatCard
                    label="Bot resolution rate"
                    value={`${data.bot_resolution_rate}%`}
                    hint="Conversations the bot finished on its own"
                    icon={Bot}
                    tone="brand"
                />
                <StatCard
                    label="Average response"
                    value={seconds(data.average_response_seconds)}
                    hint="From the customer's message to ours"
                    icon={Clock}
                />
                <StatCard
                    label="Fallback rate"
                    value={`${data.fallback_rate}%`}
                    hint="Messages nothing could answer"
                    icon={AlertTriangle}
                    tone={data.fallback_rate > 30 ? "red" : "default"}
                />
                <StatCard
                    label="Failed responses"
                    value={data.failed_responses}
                    hint="Replies the provider refused"
                    icon={AlertTriangle}
                    tone={data.failed_responses ? "red" : "default"}
                />
            </div>

            <div className="grid lg:grid-cols-3 gap-4 mb-4">
                <Card className="lg:col-span-2">
                    <CardHeader
                        title="Conversations per day"
                        description="Started, not messages — one bar is one customer arriving."
                        className="mb-4"
                    />
                    <DailyChart rows={data.weekly_conversations || []} />
                </Card>

                <Card>
                    <CardHeader title="Why the bot hands over" className="mb-4" />
                    {data.handoffs?.length ? (
                        <ul className="list-none p-0 m-0 space-y-2.5">
                            {data.handoffs.map((row) => (
                                <li key={row.trigger} className="flex items-center justify-between gap-3 text-sm">
                                    <span className="text-ink-700 min-w-0 truncate">{row.label}</span>
                                    <span className="font-bold text-ink-900 tabular-nums shrink-0">{row.count}</span>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p className="text-sm text-ink-500 m-0">
                            Nothing has been handed to a person in this period.
                        </p>
                    )}
                </Card>
            </div>

            <div className="grid lg:grid-cols-2 gap-4">
                <Card>
                    <CardHeader
                        title="Your bots"
                        description="How much work each one is actually doing."
                        action={
                            <Button size="sm" variant="secondary" onClick={() => onOpenTab("bots")}>
                                Manage
                            </Button>
                        }
                        className="mb-4"
                    />
                    <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                        {(data.bots || []).map((bot) => (
                            <li key={bot.id} className="flex items-center justify-between gap-3 py-2.5">
                                <div className="min-w-0">
                                    <p className="text-sm font-semibold text-ink-900 m-0 truncate">{bot.name}</p>
                                    <p className="text-xs text-ink-500 m-0">
                                        {bot.conversations} conversations · {bot.handoffs} handoffs
                                    </p>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <span className="text-xs font-bold text-ink-700 tabular-nums">
                                        {bot.bot_resolution_rate}%
                                    </span>
                                    <Badge status={bot.status} />
                                </div>
                            </li>
                        ))}
                    </ul>
                </Card>

                <Card>
                    <CardHeader
                        title="Top questions"
                        description="What customers ask, and what the knowledge base could not answer."
                        className="mb-4"
                    />
                    {data.top_questions?.length ? (
                        <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                            {data.top_questions.slice(0, 8).map((row, index) => (
                                <li key={index} className="py-2.5">
                                    <p className="text-sm text-ink-800 m-0 truncate" title={row.question}>
                                        {row.question}
                                    </p>
                                    <p className="text-xs text-ink-500 m-0 mt-0.5">
                                        asked {row.asked}×
                                        {row.unanswered > 0 && (
                                            <span className="text-amber-600 font-semibold">
                                                {" "}· {row.unanswered} unanswered
                                            </span>
                                        )}
                                    </p>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p className="text-sm text-ink-500 m-0">
                            Nothing yet. Questions appear here once the AI has answered some.
                        </p>
                    )}
                </Card>
            </div>
        </div>
    );
}

/**
 * A bar per day, drawn with divs.
 *
 * No charting library: this is one series of small integers, and the whole
 * thing is twenty lines of flexbox. Pulling in a chart package for it would
 * cost more in bundle size than the rest of this page.
 */
function DailyChart({ rows }) {
    const peak = Math.max(1, ...rows.map((row) => row.count));

    if (!rows.length) {
        return <p className="text-sm text-ink-500 m-0">No conversations in this period.</p>;
    }

    return (
        <div>
            <div className="flex items-end gap-1 h-40" role="img" aria-label="Conversations per day">
                {rows.map((row) => (
                    <div key={row.date} className="flex-1 min-w-0 flex flex-col items-center justify-end h-full">
                        <span className="text-[10px] text-ink-500 tabular-nums mb-1">{row.count || ""}</span>
                        <div
                            title={`${row.date}: ${row.count}`}
                            className="w-full rounded-t bg-brand-500/80 hover:bg-brand-600 transition-colors min-h-[2px]"
                            style={{ height: `${(row.count / peak) * 100}%` }}
                        />
                    </div>
                ))}
            </div>
            <div className="flex justify-between text-[11px] text-ink-400 mt-2">
                <span>{rows[0]?.date}</span>
                <span>{rows[rows.length - 1]?.date}</span>
            </div>
        </div>
    );
}
