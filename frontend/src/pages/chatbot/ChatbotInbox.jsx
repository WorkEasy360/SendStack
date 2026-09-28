import { useCallback, useEffect, useRef, useState } from "react";
import {
    ArrowLeft,
    Bot,
    Bug,
    Check,
    CheckCheck,
    CheckCircle2,
    Clock,
    Hand,
    Inbox,
    Paperclip,
    RotateCcw,
    Send,
    UserCheck,
    Wifi,
    WifiOff,
    X,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import useRealtime from "../../hooks/useRealtime";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import EmptyState from "../../components/ui/EmptyState";
import SearchInput from "../../components/ui/SearchInput";
import Spinner from "../../components/ui/Spinner";
import { Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import ChatbotTrace from "./ChatbotTrace";

/**
 * The chatbot inbox: a WhatsApp-shaped thread with the bot's state on top of it.
 *
 * The live update path is the platform's existing SSE stream (`useRealtime`),
 * not a new socket layer — the server already publishes `message.received`,
 * `message.status` and now `chatbot.state`, and a second real-time mechanism
 * would be a second thing to keep alive in production. Polling stays as the
 * fallback for a deployment whose Redis is unreachable, at a much slower
 * cadence while the stream is up.
 */

const POLL_MS = 15000;
const LIVE_POLL_MS = 90000;

const BUCKETS = [
    { id: "active", label: "Active" },
    { id: "bot", label: "Bot" },
    { id: "human", label: "Human" },
    { id: "resolved", label: "Resolved" },
    { id: "", label: "All" },
];

const STATE_TONE = {
    NEW: "blue",
    BOT_ACTIVE: "green",
    WAITING_FOR_CUSTOMER: "gray",
    WAITING_FOR_AGENT: "amber",
    HUMAN_ACTIVE: "violet",
    RESOLVED: "gray",
    CLOSED: "gray",
};

const STATE_LABEL = {
    NEW: "New",
    BOT_ACTIVE: "Bot handling",
    WAITING_FOR_CUSTOMER: "Waiting for them",
    WAITING_FOR_AGENT: "Waiting for us",
    HUMAN_ACTIVE: "You have this",
    RESOLVED: "Resolved",
    CLOSED: "Closed",
};

function initials(value) {
    return (value || "?").trim().charAt(0).toUpperCase();
}

function shortTime(value) {
    if (!value) return "";
    return new Date(value).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function relative(value) {
    if (!value) return "";
    const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
    if (minutes < 1) return "now";
    if (minutes < 60) return `${minutes}m`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
    return `${Math.round(minutes / 1440)}d`;
}

export default function ChatbotInbox({ bots, accounts, can }) {
    const toast = useToast();
    const [sessions, setSessions] = useState([]);
    const [selected, setSelected] = useState(null);
    const [messages, setMessages] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadingThread, setLoadingThread] = useState(false);
    const [bucket, setBucket] = useState("active");
    const [search, setSearch] = useState("");
    const [botFilter, setBotFilter] = useState("");
    const [accountFilter, setAccountFilter] = useState("");
    const [unreadOnly, setUnreadOnly] = useState(false);
    const [draft, setDraft] = useState("");
    const [sending, setSending] = useState(false);
    const [busy, setBusy] = useState("");
    const [showTrace, setShowTrace] = useState(false);
    const [error, setError] = useState("");

    const bottomRef = useRef(null);
    const selectedRef = useRef(null);
    selectedRef.current = selected;

    const loadSessions = useCallback(async () => {
        try {
            const params = { page_size: 60 };
            if (bucket) params.bucket = bucket;
            if (search.trim()) params.search = search.trim();
            if (botFilter) params.chatbot = botFilter;
            if (accountFilter) params.account = accountFilter;
            if (unreadOnly) params.unread = 1;

            const response = await api.get("/chatbot/conversations/", { params });
            setSessions(response.data.results ?? response.data ?? []);
            setError("");
        } catch (requestError) {
            setError(apiErrorMessage(requestError, "Could not load conversations."));
        } finally {
            setLoading(false);
        }
    }, [bucket, search, botFilter, accountFilter, unreadOnly]);

    const loadThread = useCallback(async (session, { quiet = false } = {}) => {
        if (!session) return;
        if (!quiet) setLoadingThread(true);
        try {
            const response = await api.get(`/chatbot/conversations/${session.id}/messages/`, {
                params: { page_size: 60 },
            });
            const rows = response.data.results ?? response.data ?? [];
            // The API returns newest first so page 1 is the live end; the
            // thread reads oldest-first.
            setMessages([...rows].reverse());
        } catch (requestError) {
            if (!quiet) toast.error(apiErrorMessage(requestError, "Could not load that conversation."));
        } finally {
            setLoadingThread(false);
        }
    }, [toast]);

    useEffect(() => {
        loadSessions();
    }, [loadSessions]);

    // Keyed on the id rather than the object: `selected` is replaced wholesale
    // by every list refresh, and depending on it would refetch the thread on
    // each poll. `selectedRef` gives the effect the current row without making
    // it a dependency.
    const selectedId = selected?.id;
    useEffect(() => {
        const session = selectedRef.current;
        if (!selectedId || !session) {
            setMessages([]);
            return;
        }
        loadThread(session);
        api.post(`/chatbot/conversations/${selectedId}/mark-read/`).catch(() => {});
    }, [selectedId, loadThread]);

    useEffect(() => {
        bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }, [messages.length]);

    const onEvent = useCallback(
        (payload) => {
            // Anything that changes a conversation refreshes the list; a
            // message on the open thread also refreshes the thread. Refetching
            // rather than splicing the payload in: the event carries a preview,
            // not the full row, and a half-built message in the thread would
            // render wrong until the next poll.
            if (["message.received", "message.sent", "message.status", "chatbot.state", "chatbot.handoff"].includes(payload.type)) {
                loadSessions();
                const open = selectedRef.current;
                if (open && payload.data?.conversation === open.conversation) {
                    loadThread(open, { quiet: true });
                }
            }
        },
        [loadSessions, loadThread],
    );

    const { connected, available } = useRealtime({ onEvent });

    useEffect(() => {
        const interval = setInterval(() => {
            loadSessions();
            if (selectedRef.current) loadThread(selectedRef.current, { quiet: true });
        }, connected ? LIVE_POLL_MS : POLL_MS);
        return () => clearInterval(interval);
    }, [connected, loadSessions, loadThread]);

    const refresh = async () => {
        await loadSessions();
        if (selected) {
            const response = await api.get(`/chatbot/conversations/${selected.id}/`);
            setSelected(response.data);
        }
    };

    const act = async (path, label) => {
        setBusy(path);
        try {
            const response = await api.post(`/chatbot/conversations/${selected.id}/${path}/`);
            setSelected(response.data);
            toast.success(label);
            loadSessions();
        } catch (requestError) {
            toast.error(apiErrorMessage(requestError, `Could not ${label.toLowerCase()}.`));
        } finally {
            setBusy("");
        }
    };

    const send = async () => {
        if (!draft.trim()) return;
        setSending(true);
        try {
            await api.post(`/chatbot/conversations/${selected.id}/reply/`, { body: draft.trim() });
            setDraft("");
            await loadThread(selected, { quiet: true });
            await refresh();
        } catch (requestError) {
            toast.error(apiErrorMessage(requestError, "Could not send that reply."));
        } finally {
            setSending(false);
        }
    };

    if (loading) {
        return (
            <div className="flex justify-center py-20">
                <Spinner />
            </div>
        );
    }

    return (
        <div>
            {error && (
                <Alert tone="error" className="mb-4">
                    {error}
                </Alert>
            )}

            <div className="flex flex-wrap items-center gap-2 mb-4">
                <div className="flex gap-1 bg-ink-100 rounded-lg p-1">
                    {BUCKETS.map((entry) => (
                        <button
                            key={entry.id || "all"}
                            type="button"
                            onClick={() => setBucket(entry.id)}
                            className={`px-3 py-1.5 text-sm font-semibold rounded-md cursor-pointer border-none transition-colors ${
                                bucket === entry.id
                                    ? "bg-surface text-ink-900 shadow-card"
                                    : "bg-transparent text-ink-500 hover:text-ink-800"
                            }`}
                        >
                            {entry.label}
                        </button>
                    ))}
                </div>

                <button
                    type="button"
                    onClick={() => setUnreadOnly((value) => !value)}
                    className={`px-3 py-1.5 text-sm font-semibold rounded-lg cursor-pointer transition-colors ${
                        unreadOnly
                            ? "bg-brand-600 text-white border border-transparent"
                            : "bg-surface text-ink-600 border border-ink-200 hover:bg-ink-50"
                    }`}
                >
                    Unread
                </button>

                {bots.length > 1 && (
                    <div className="w-44">
                        <Select value={botFilter} onChange={(event) => setBotFilter(event.target.value)} aria-label="Chatbot">
                            <option value="">All bots</option>
                            {bots.map((bot) => (
                                <option key={bot.id} value={bot.id}>
                                    {bot.name}
                                </option>
                            ))}
                        </Select>
                    </div>
                )}

                {accounts.length > 1 && (
                    <div className="w-44">
                        <Select
                            value={accountFilter}
                            onChange={(event) => setAccountFilter(event.target.value)}
                            aria-label="WhatsApp number"
                        >
                            <option value="">All numbers</option>
                            {accounts.map((account) => (
                                <option key={account.id} value={account.id}>
                                    {account.name}
                                </option>
                            ))}
                        </Select>
                    </div>
                )}

                <div className="flex-1 min-w-[180px]">
                    <SearchInput value={search} onChange={setSearch} placeholder="Search name or number…" />
                </div>

                <span
                    className="inline-flex items-center gap-1.5 text-xs text-ink-500"
                    title={
                        available === false
                            ? "Live updates are unavailable on this deployment; the inbox is polling."
                            : connected
                              ? "Live"
                              : "Reconnecting…"
                    }
                >
                    {connected ? (
                        <Wifi size={13} className="text-brand-600" aria-hidden="true" />
                    ) : (
                        <WifiOff size={13} className="text-ink-400" aria-hidden="true" />
                    )}
                    {connected ? "Live" : "Polling"}
                </span>
            </div>

            <div className="grid lg:grid-cols-[340px_1fr] gap-4 h-[calc(100vh-330px)] min-h-[480px]">
                {/* --- conversation list --- */}
                <div
                    className={`bg-surface border border-ink-200 rounded-card overflow-hidden flex flex-col ${
                        selected ? "hidden lg:flex" : "flex"
                    }`}
                >
                    {sessions.length === 0 ? (
                        <EmptyState
                            variant="plain"
                            icon={Inbox}
                            title="Nothing here"
                            description="Conversations appear the moment a customer messages a number one of your bots is on."
                        />
                    ) : (
                        <ul className="list-none p-0 m-0 scroll-y flex-1">
                            {sessions.map((session) => (
                                <li key={session.id}>
                                    <button
                                        type="button"
                                        onClick={() => setSelected(session)}
                                        className={`w-full text-left px-3.5 py-3 border-0 border-b border-ink-100 cursor-pointer transition-colors ${
                                            selected?.id === session.id
                                                ? "bg-brand-50/60"
                                                : "bg-transparent hover:bg-ink-50"
                                        }`}
                                    >
                                        <div className="flex items-start gap-2.5">
                                            <span className="shrink-0 w-9 h-9 rounded-full bg-ink-200 text-ink-600 text-sm font-bold flex items-center justify-center">
                                                {initials(session.contact_name || session.contact_phone)}
                                            </span>
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-baseline justify-between gap-2">
                                                    <span className="text-sm font-semibold text-ink-900 truncate">
                                                        {session.contact_name || session.contact_phone}
                                                    </span>
                                                    <span className="text-[11px] text-ink-400 shrink-0 tabular-nums">
                                                        {relative(session.updated_at)}
                                                    </span>
                                                </div>
                                                <p className="text-xs text-ink-500 m-0 mt-0.5 truncate">
                                                    {session.last_message_preview || "No messages yet"}
                                                </p>
                                                <div className="flex items-center gap-1.5 mt-1.5">
                                                    <Badge tone={STATE_TONE[session.state]} size="xs">
                                                        {STATE_LABEL[session.state] || session.state}
                                                    </Badge>
                                                    {session.unread_count > 0 && (
                                                        <span className="ml-auto min-w-[18px] h-[18px] px-1 rounded-full bg-brand-600 text-white text-[10px] font-bold flex items-center justify-center tabular-nums">
                                                            {session.unread_count}
                                                        </span>
                                                    )}
                                                </div>
                                            </div>
                                        </div>
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>

                {/* --- thread --- */}
                <div
                    className={`bg-ink-50 border border-ink-200 rounded-card overflow-hidden flex-col ${
                        selected ? "flex" : "hidden lg:flex"
                    }`}
                >
                    {!selected ? (
                        <div className="flex-1 flex items-center justify-center">
                            <EmptyState
                                variant="plain"
                                icon={Bot}
                                title="Pick a conversation"
                                description="You'll see the whole thread, what the bot decided, and the controls to take over."
                            />
                        </div>
                    ) : (
                        <>
                            <div className="bg-surface border-b border-ink-200 px-4 py-3 flex items-center gap-3 shrink-0">
                                <button
                                    type="button"
                                    onClick={() => setSelected(null)}
                                    aria-label="Back to the list"
                                    className="lg:hidden w-8 h-8 shrink-0 inline-flex items-center justify-center rounded-lg text-ink-500 hover:bg-ink-100 bg-transparent border-none cursor-pointer"
                                >
                                    <ArrowLeft size={17} />
                                </button>

                                <div className="flex-1 min-w-0">
                                    <p className="text-sm font-bold text-ink-900 m-0 truncate">
                                        {selected.contact_name || selected.contact_phone}
                                    </p>
                                    <p className="text-xs text-ink-500 m-0 flex items-center gap-1.5 flex-wrap">
                                        <Badge tone={STATE_TONE[selected.state]} size="xs">
                                            {STATE_LABEL[selected.state] || selected.state}
                                        </Badge>
                                        {selected.chatbot_name && <span>· {selected.chatbot_name}</span>}
                                        {selected.detected_language && selected.detected_language !== "AUTO" && (
                                            <span>· {selected.detected_language.toLowerCase()}</span>
                                        )}
                                        <span>· {selected.bot_replies} bot replies</span>
                                    </p>
                                </div>

                                <div className="flex items-center gap-1.5 shrink-0">
                                    {can("CHATBOT_ANALYTICS") && (
                                        <Button
                                            size="sm"
                                            variant={showTrace ? "dark" : "secondary"}
                                            icon={Bug}
                                            onClick={() => setShowTrace((value) => !value)}
                                            title="What the bot did with each message"
                                        >
                                            Debug
                                        </Button>
                                    )}
                                    {can("CONVERSATION_TAKEOVER") &&
                                        (selected.state === "HUMAN_ACTIVE" ? (
                                            <Button
                                                size="sm"
                                                icon={RotateCcw}
                                                loading={busy === "resume-bot"}
                                                onClick={() => act("resume-bot", "Handed back to the bot")}
                                            >
                                                Return to bot
                                            </Button>
                                        ) : (
                                            <Button
                                                size="sm"
                                                variant="secondary"
                                                icon={Hand}
                                                loading={busy === "takeover"}
                                                onClick={() => act("takeover", "You have this conversation")}
                                            >
                                                Take over
                                            </Button>
                                        ))}
                                    {can("CONVERSATION_REPLY") && selected.state !== "RESOLVED" && (
                                        <Button
                                            size="sm"
                                            variant="secondary"
                                            icon={CheckCircle2}
                                            loading={busy === "resolve"}
                                            onClick={() => act("resolve", "Marked resolved")}
                                            title="Mark this resolved"
                                        >
                                            Resolve
                                        </Button>
                                    )}
                                </div>
                            </div>

                            {selected.handoff_reason && selected.state === "HUMAN_ACTIVE" && (
                                <div className="bg-violet-50 border-b border-violet-200 px-4 py-2 text-xs text-violet-800 shrink-0">
                                    <UserCheck size={12} className="inline-block mr-1.5 -mt-0.5" aria-hidden="true" />
                                    Handed over: {selected.handoff_reason}
                                </div>
                            )}

                            {showTrace ? (
                                <ChatbotTrace sessionId={selected.id} onClose={() => setShowTrace(false)} />
                            ) : (
                                <>
                                    <div className="scroll-y flex-1 px-4 py-4 space-y-2">
                                        {loadingThread ? (
                                            <div className="flex justify-center py-10">
                                                <Spinner />
                                            </div>
                                        ) : (
                                            messages.map((message) => (
                                                <MessageBubble key={message.id} message={message} />
                                            ))
                                        )}
                                        <div ref={bottomRef} />
                                    </div>

                                    {can("CONVERSATION_REPLY") && (
                                        <div className="bg-surface border-t border-ink-200 p-3 shrink-0">
                                            {selected.state !== "HUMAN_ACTIVE" && (
                                                <p className="text-[11px] text-ink-500 m-0 mb-2">
                                                    Replying takes this conversation off the bot.
                                                </p>
                                            )}
                                            <div className="flex items-end gap-2">
                                                <Textarea
                                                    rows={1}
                                                    value={draft}
                                                    onChange={(event) => setDraft(event.target.value)}
                                                    onKeyDown={(event) => {
                                                        if (event.key === "Enter" && !event.shiftKey) {
                                                            event.preventDefault();
                                                            send();
                                                        }
                                                    }}
                                                    placeholder="Type a reply…"
                                                    className="min-h-[42px] max-h-40"
                                                />
                                                <Button
                                                    icon={Send}
                                                    loading={sending}
                                                    disabled={!draft.trim()}
                                                    onClick={send}
                                                >
                                                    Send
                                                </Button>
                                            </div>
                                        </div>
                                    )}
                                </>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function MessageBubble({ message }) {
    const outbound = message.direction === "OUTBOUND";
    const failed = message.status === "FAILED" || message.status === "SKIPPED";

    return (
        <div className={`flex ${outbound ? "justify-end" : "justify-start"}`}>
            <div
                className={`max-w-[85%] sm:max-w-[70%] rounded-2xl px-3.5 py-2 text-sm shadow-card ${
                    outbound
                        ? failed
                            ? "bg-red-50 text-red-900 border border-red-200 rounded-br-sm"
                            : "bg-brand-600 text-white rounded-br-sm"
                        : "bg-surface text-ink-800 border border-ink-200 rounded-bl-sm"
                }`}
            >
                {message.media_url && (
                    <a
                        href={message.media_url}
                        target="_blank"
                        rel="noreferrer"
                        className={`flex items-center gap-1.5 text-xs font-semibold mb-1 ${
                            outbound && !failed ? "text-brand-100" : "text-brand-700"
                        }`}
                    >
                        <Paperclip size={12} aria-hidden="true" />
                        {message.filename || message.message_type?.toLowerCase()}
                    </a>
                )}

                <p className="whitespace-pre-wrap m-0">{message.body || message.caption}</p>

                {message.buttons?.length > 0 && (
                    <div
                        className={`flex flex-col gap-1 mt-2 pt-2 border-t ${
                            outbound && !failed ? "border-white/25" : "border-ink-200"
                        }`}
                    >
                        {message.buttons.map((button, index) => (
                            <span
                                key={button.id || index}
                                className={`rounded-lg px-2.5 py-1 text-center text-xs font-semibold ${
                                    outbound && !failed ? "bg-white/15 text-white" : "bg-ink-100 text-brand-700"
                                }`}
                            >
                                {button.title}
                            </span>
                        ))}
                    </div>
                )}

                {failed && message.error_message && (
                    <p className="text-[11px] mt-1 mb-0 font-medium">{message.error_message}</p>
                )}

                <div
                    className={`flex items-center justify-end gap-1 mt-1 text-[10px] ${
                        outbound && !failed ? "text-brand-100" : "text-ink-400"
                    }`}
                >
                    {message.is_automated && <Bot size={11} aria-label="Sent by the chatbot" />}
                    <span className="tabular-nums">{shortTime(message.queued_at)}</span>
                    {outbound && <StatusTick status={message.status} />}
                </div>
            </div>
        </div>
    );
}

function StatusTick({ status }) {
    if (status === "READ") return <CheckCheck size={12} aria-label="Read" className="text-blue-200" />;
    if (status === "DELIVERED") return <CheckCheck size={12} aria-label="Delivered" />;
    if (status === "SENT") return <Check size={12} aria-label="Sent" />;
    if (status === "QUEUED" || status === "SENDING") return <Clock size={11} aria-label="Queued" />;
    if (status === "FAILED" || status === "SKIPPED") return <X size={12} aria-label="Failed" />;
    return null;
}
