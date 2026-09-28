import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, MinusCircle, RefreshCw } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import EmptyState from "../../components/ui/EmptyState";
import Spinner from "../../components/ui/Spinner";

/**
 * The debug panel: what the bot actually did with each message, in order.
 *
 * This exists because "the bot didn't reply" has about fifteen possible causes
 * — the number was unlinked, an agent had the conversation, retrieval scored
 * below the floor, the matched rule was on cooldown, the model errored — and
 * none of them are visible from the thread. Each inbound message is one
 * expandable journey, timed step by step, so the slow or silent step is the one
 * you can see.
 *
 * Nothing secret reaches here: the server strips anything key-shaped out of a
 * trace row's metadata before it is written.
 */

const STEP_LABEL = {
    RECEIVED: "Message received",
    NORMALIZED: "Normalised",
    SESSION: "Conversation resolved",
    DEDUPE: "Duplicate & loop checks",
    STATE: "State check",
    HOURS: "Business hours",
    LANGUAGE: "Language",
    FLOW: "Flow",
    RULE: "Rule evaluation",
    AI_REQUEST: "AI request",
    RETRIEVAL: "Knowledge retrieval",
    TOOL: "Tool call",
    ACTION: "Action",
    RESPONSE: "Response generated",
    SEND: "Handed to the provider",
    DELIVERY: "Delivery status",
    HANDOFF: "Handed to a person",
    FALLBACK: "Fallback",
    ERROR: "Error",
};

const OUTCOME_ICON = {
    OK: { icon: CheckCircle2, className: "text-brand-600" },
    SKIPPED: { icon: MinusCircle, className: "text-ink-400" },
    ERROR: { icon: AlertTriangle, className: "text-red-500" },
};

export default function ChatbotTrace({ sessionId, onClose }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [open, setOpen] = useState({});

    const load = async () => {
        setLoading(true);
        try {
            const response = await api.get(`/chatbot/conversations/${sessionId}/trace/`);
            setData(response.data);
            // The newest journey is the one somebody came here to read.
            setOpen({ [response.data.journeys?.[0]?.correlation_id]: true });
            setError("");
        } catch (requestError) {
            setError(apiErrorMessage(requestError, "Could not load the debug trail."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionId]);

    if (loading) {
        return (
            <div className="flex-1 flex justify-center items-center">
                <Spinner />
            </div>
        );
    }

    return (
        <div className="scroll-y flex-1 p-4">
            <div className="flex items-center justify-between gap-3 mb-3">
                <p className="text-xs text-ink-500 m-0">
                    Every decision, newest first. Timings are how long each step took.
                </p>
                <div className="flex gap-2">
                    <Button size="xs" variant="secondary" icon={RefreshCw} onClick={load}>
                        Refresh
                    </Button>
                    <Button size="xs" variant="secondary" onClick={onClose}>
                        Back to the thread
                    </Button>
                </div>
            </div>

            {error && <Alert tone="error">{error}</Alert>}

            {!error && !data?.journeys?.length && (
                <EmptyState
                    variant="plain"
                    icon={AlertTriangle}
                    title="Nothing recorded yet"
                    description="The trail is written when a published chatbot processes a message on this conversation. Older entries are pruned after the retention window."
                />
            )}

            <div className="space-y-2">
                {(data?.journeys || []).map((journey) => {
                    const expanded = open[journey.correlation_id];
                    const first = journey.steps[0];
                    return (
                        <div
                            key={journey.correlation_id}
                            className={`bg-surface border rounded-card overflow-hidden ${
                                journey.has_error ? "border-red-200" : "border-ink-200"
                            }`}
                        >
                            <button
                                type="button"
                                onClick={() =>
                                    setOpen((current) => ({
                                        ...current,
                                        [journey.correlation_id]: !current[journey.correlation_id],
                                    }))
                                }
                                className="w-full flex items-center gap-2.5 px-3.5 py-2.5 text-left bg-transparent border-none cursor-pointer hover:bg-ink-50 transition-colors"
                            >
                                {expanded ? (
                                    <ChevronDown size={15} className="text-ink-400 shrink-0" aria-hidden="true" />
                                ) : (
                                    <ChevronRight size={15} className="text-ink-400 shrink-0" aria-hidden="true" />
                                )}
                                <div className="flex-1 min-w-0">
                                    <p className="text-sm font-semibold text-ink-900 m-0 truncate">
                                        {first?.metadata?.from_phone
                                            ? `Message from ${first.metadata.from_phone}`
                                            : first?.detail || "Message"}
                                    </p>
                                    <p className="text-[11px] text-ink-500 m-0 mt-0.5">
                                        {new Date(journey.started_at).toLocaleString()} · {journey.steps.length} steps ·{" "}
                                        {journey.total_ms} ms
                                    </p>
                                </div>
                                {journey.has_error && (
                                    <AlertTriangle size={15} className="text-red-500 shrink-0" aria-hidden="true" />
                                )}
                                <code className="text-[10px] text-ink-300 shrink-0 hidden sm:inline">
                                    {journey.correlation_id.slice(0, 8)}
                                </code>
                            </button>

                            {expanded && (
                                <ol className="list-none p-0 m-0 border-t border-ink-100">
                                    {journey.steps.map((step) => {
                                        const tone = OUTCOME_ICON[step.outcome] || OUTCOME_ICON.OK;
                                        const Icon = tone.icon;
                                        const details = Object.entries(step.metadata || {}).filter(
                                            ([, value]) => value !== null && value !== "" && value !== undefined,
                                        );
                                        return (
                                            <li
                                                key={step.id}
                                                className="flex items-start gap-2.5 px-3.5 py-2 border-b border-ink-50 last:border-b-0"
                                            >
                                                <Icon
                                                    size={14}
                                                    className={`shrink-0 mt-0.5 ${tone.className}`}
                                                    aria-label={step.outcome}
                                                />
                                                <div className="flex-1 min-w-0">
                                                    <div className="flex items-baseline justify-between gap-3">
                                                        <span className="text-xs font-bold text-ink-800">
                                                            {STEP_LABEL[step.step] || step.step_label || step.step}
                                                        </span>
                                                        <span className="text-[10px] text-ink-400 tabular-nums shrink-0">
                                                            {step.duration_ms} ms
                                                        </span>
                                                    </div>
                                                    {step.detail && (
                                                        <p className="text-xs text-ink-600 m-0 mt-0.5">{step.detail}</p>
                                                    )}
                                                    {details.length > 0 && (
                                                        <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1">
                                                            {details.map(([key, value]) => (
                                                                <span key={key} className="text-[10px] text-ink-400">
                                                                    <span className="font-semibold">{key}</span>:{" "}
                                                                    {typeof value === "object"
                                                                        ? JSON.stringify(value)
                                                                        : String(value)}
                                                                </span>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>
                                            </li>
                                        );
                                    })}
                                </ol>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
