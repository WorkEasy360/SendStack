import { useEffect, useState } from "react";
import { AlertTriangle, BarChart3, Bot, Clock, MessageSquare, Send, Sparkles, UserCheck } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Card, { CardHeader } from "../../components/ui/Card";
import EmptyState from "../../components/ui/EmptyState";
import { Select } from "../../components/ui/Field";
import Spinner from "../../components/ui/Spinner";
import StatCard from "../../components/ui/StatCard";
import BotPicker from "./BotPicker";

/**
 * One bot's numbers.
 *
 * The Overview tab answers "is the whole thing working?"; this answers "is
 * *this* bot working, and where is it failing?" — which is why the two screens
 * that matter here are the rules that never fire and the questions that went
 * unanswered. Both are lists of things to go and fix.
 */

const WINDOWS = [7, 30, 90];

function seconds(value) {
    if (!value) return "—";
    return value < 60 ? `${value.toFixed(1)}s` : `${Math.round(value / 60)}m`;
}

export default function ChatbotAnalytics({ bots, selected, onSelectBot }) {
    const [days, setDays] = useState(30);
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    // The id, not the object: the parent rebuilds `selected` on every refresh,
    // and depending on the object would refetch the whole analytics payload
    // each time.
    const selectedId = selected?.id;
    useEffect(() => {
        if (!selectedId) {
            setLoading(false);
            return undefined;
        }
        let cancelled = false;
        setLoading(true);
        api.get(`/chatbot/bots/${selectedId}/analytics/`, { params: { days } })
            .then((response) => {
                if (!cancelled) {
                    setData(response.data);
                    setError("");
                }
            })
            .catch((requestError) => {
                if (!cancelled) setError(apiErrorMessage(requestError, "Could not load the analytics."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [selectedId, days]);

    if (!bots.length) {
        return <EmptyState icon={BarChart3} title="No chatbots yet" description="Create a chatbot to see its numbers." />;
    }

    return (
        <div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
                <BotPicker bots={bots} selected={selected} onSelect={onSelectBot} />
                <div className="w-40">
                    <Select value={days} onChange={(event) => setDays(Number(event.target.value))} aria-label="Time range">
                        {WINDOWS.map((entry) => (
                            <option key={entry} value={entry}>
                                Last {entry} days
                            </option>
                        ))}
                    </Select>
                </div>
            </div>

            {error && <Alert tone="error">{error}</Alert>}

            {loading ? (
                <div className="flex justify-center py-20">
                    <Spinner />
                </div>
            ) : (
                data && (
                    <>
                        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                            <StatCard label="Conversations started" value={data.total_conversations} icon={MessageSquare} tone="brand" />
                            <StatCard label="Completed" value={`${data.completion_rate}%`} icon={Bot} />
                            <StatCard label="Messages received" value={data.messages_received} icon={MessageSquare} />
                            <StatCard label="Messages sent" value={data.messages_sent} icon={Send} />
                        </div>

                        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                            <StatCard label="AI responses" value={data.ai_responses} icon={Sparkles} tone="violet" />
                            <StatCard
                                label="Handed to a person"
                                value={data.human_handoffs}
                                hint={`${data.handoff_rate}% of conversations`}
                                icon={UserCheck}
                                tone="amber"
                            />
                            <StatCard
                                label="Agent takeovers"
                                value={data.agent_takeovers}
                                hint="A person stepped in themselves"
                                icon={UserCheck}
                            />
                            <StatCard
                                label="Average response"
                                value={seconds(data.average_response_seconds)}
                                icon={Clock}
                            />
                        </div>

                        <div className="grid lg:grid-cols-2 gap-4 mb-4">
                            <Card>
                                <CardHeader
                                    title="Rules"
                                    description="A rule that has never matched usually has a typo in its pattern."
                                    className="mb-4"
                                />
                                {data.rules?.length ? (
                                    <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                                        {data.rules.map((rule) => (
                                            <li key={rule.id} className="flex items-center justify-between gap-3 py-2.5">
                                                <div className="min-w-0">
                                                    <p className="text-sm text-ink-800 m-0 truncate">
                                                        {rule.name}
                                                        {!rule.is_active && (
                                                            <span className="ml-2 text-xs text-ink-400">off</span>
                                                        )}
                                                    </p>
                                                    <p className="text-[11px] text-ink-400 m-0 truncate">
                                                        {rule.match_type.toLowerCase().replace(/_/g, " ")}
                                                        {rule.pattern ? ` · ${rule.pattern}` : ""}
                                                    </p>
                                                </div>
                                                <span
                                                    className={`text-sm font-bold tabular-nums shrink-0 ${
                                                        rule.match_count ? "text-ink-900" : "text-amber-600"
                                                    }`}
                                                >
                                                    {rule.match_count}
                                                </span>
                                            </li>
                                        ))}
                                    </ul>
                                ) : (
                                    <p className="text-sm text-ink-500 m-0">This bot has no rules.</p>
                                )}
                            </Card>

                            <Card>
                                <CardHeader
                                    title="Why it hands over"
                                    description="The biggest bar is the gap in what the bot can answer."
                                    className="mb-4"
                                />
                                {data.handoffs?.length ? (
                                    <ul className="list-none p-0 m-0 space-y-2.5">
                                        {data.handoffs.map((row) => (
                                            <li key={row.trigger}>
                                                <div className="flex items-center justify-between gap-3 text-sm mb-1">
                                                    <span className="text-ink-700 min-w-0 truncate">{row.label}</span>
                                                    <span className="font-bold text-ink-900 tabular-nums shrink-0">
                                                        {row.count}
                                                    </span>
                                                </div>
                                                <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
                                                    <div
                                                        className="h-full bg-amber-500 rounded-full"
                                                        style={{
                                                            width: `${
                                                                (row.count /
                                                                    Math.max(...data.handoffs.map((entry) => entry.count))) *
                                                                100
                                                            }%`,
                                                        }}
                                                    />
                                                </div>
                                            </li>
                                        ))}
                                    </ul>
                                ) : (
                                    <p className="text-sm text-ink-500 m-0">Nothing has been handed over.</p>
                                )}
                            </Card>
                        </div>

                        <div className="grid lg:grid-cols-2 gap-4">
                            <Card>
                                <CardHeader
                                    title="Top questions"
                                    description="Unanswered ones are documents worth adding."
                                    className="mb-4"
                                />
                                {data.top_questions?.length ? (
                                    <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                                        {data.top_questions.map((row, index) => (
                                            <li key={index} className="py-2.5">
                                                <p className="text-sm text-ink-800 m-0" title={row.question}>
                                                    {row.question}
                                                </p>
                                                <p className="text-[11px] text-ink-500 m-0 mt-0.5">
                                                    asked {row.asked}× · relevance {row.average_relevance}
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
                                    <p className="text-sm text-ink-500 m-0">Nothing recorded yet.</p>
                                )}
                            </Card>

                            <div className="space-y-4">
                                <Card>
                                    <CardHeader title="What they ask about" className="mb-4" />
                                    {data.top_intents?.length ? (
                                        <div className="flex flex-wrap gap-2">
                                            {data.top_intents.map((row) => (
                                                <Badge key={row.intent} tone="blue">
                                                    {row.label} · {row.count}
                                                </Badge>
                                            ))}
                                        </div>
                                    ) : (
                                        <p className="text-sm text-ink-500 m-0">Nothing recorded yet.</p>
                                    )}
                                </Card>

                                <Card>
                                    <CardHeader
                                        title="Most used flow steps"
                                        description="Where customers actually go."
                                        className="mb-4"
                                    />
                                    {data.flow_usage?.length ? (
                                        <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                                            {data.flow_usage.map((row) => (
                                                <li
                                                    key={row.node_id}
                                                    className="flex items-center justify-between gap-3 py-2 text-sm"
                                                >
                                                    <span className="text-ink-700 truncate">
                                                        {row.node_id}
                                                        <span className="text-ink-400 ml-1.5 text-xs">
                                                            {row.node_type?.toLowerCase()}
                                                        </span>
                                                    </span>
                                                    <span className="font-bold text-ink-900 tabular-nums shrink-0">
                                                        {row.count}
                                                    </span>
                                                </li>
                                            ))}
                                        </ul>
                                    ) : (
                                        <p className="text-sm text-ink-500 m-0">This bot has no flow, or nobody has reached it.</p>
                                    )}
                                </Card>

                                {data.failed_responses > 0 && (
                                    <Alert tone="warning">
                                        <AlertTriangle size={14} className="inline-block mr-1.5 -mt-0.5" aria-hidden="true" />
                                        {data.failed_responses} replies could not be delivered. Check the number's
                                        connection on the Devices page, and the Debug panel on an affected conversation.
                                    </Alert>
                                )}
                            </div>
                        </div>
                    </>
                )
            )}
        </div>
    );
}
