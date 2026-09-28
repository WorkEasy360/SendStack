import { useCallback, useEffect, useRef, useState } from "react";
import {
    ArrowLeft,
    Bot,
    Check,
    CheckCheck,
    ChevronUp,
    CheckCheck as MarkRead,
    MessageSquare,
    MousePointerClick,
    Paperclip,
    RefreshCw,
    Send,
    Sparkles,
    StickyNote,
    Tag,
    Trash2,
    UserCheck,
    X,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import { toMediaItems } from "../../utils/media";
import useSelection from "../../hooks/useSelection";
import useRealtime from "../../hooks/useRealtime";
import { useAuth } from "../../context/AuthContext";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import { StatusBadge } from "../../components/ui/Badge";
import PageHeader from "../../components/ui/PageHeader";
import SearchInput from "../../components/ui/SearchInput";
import Spinner from "../../components/ui/Spinner";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import { useToast } from "../../components/ui/Toast";
import BulkActionBar from "../../components/ui/BulkActionBar";
import MediaPicker from "../../components/ui/MediaPicker";
import ButtonBuilder from "../../components/ui/ButtonBuilder";
import { Checkbox, Field, Input, Textarea } from "../../components/ui/Field";

/**
 * How often the open inbox re-checks the API. There is no websocket layer yet,
 * so the conversation list and the open thread are kept fresh by polling.
 */
const POLL_MS = 15000;
// How long a "typing" bubble survives its last keystroke. The event carries no
// "stopped typing" counterpart -- by the time one could arrive it would often
// be wrong -- so the indicator expires on its own instead.
const TYPING_TTL_MS = 4000;
// While the stream is up the list is still refetched, just rarely: it is the
// backstop for anything the stream missed during a reconnect, not the way
// updates normally arrive.
const LIVE_POLL_MS = 120000;

const FILTERS = [
    { id: "all", label: "All", params: {} },
    { id: "unread", label: "Unread", params: { unread_count__gt: 0 } },
    { id: "open", label: "Open", params: { status: "OPEN" } },
    { id: "closed", label: "Closed", params: { status: "CLOSED" } },
];

function initials(value) {
    return (value || "?").trim().charAt(0).toUpperCase();
}

function formatTime(value) {
    if (!value) return "";
    return new Date(value).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function formatDay(value) {
    if (!value) return "";
    const date = new Date(value);
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** Compact stamp for the conversation list ("now", "3m", "2h", "Mon"). */
function formatRelative(value) {
    if (!value) return "";
    const then = new Date(value);
    const minutes = Math.round((Date.now() - then.getTime()) / 60000);
    if (minutes < 1) return "now";
    if (minutes < 60) return `${minutes}m`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
    if (minutes < 10080) return then.toLocaleDateString(undefined, { weekday: "short" });
    return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * Oldest-first, de-duplicated by id. The API returns the thread newest-first
 * (page 1 is the live end), and pages can overlap while new messages land.
 */
function mergeMessages(existing, incoming) {
    const byId = new Map(existing.map((message) => [message.id, message]));
    incoming.forEach((message) => byId.set(message.id, message));
    return [...byId.values()].sort(
        (a, b) => new Date(a.queued_at) - new Date(b.queued_at) || a.id - b.id
    );
}

function MessageStatusIcon({ status }) {
    if (status === "READ") return <CheckCheck size={13} className="text-white" aria-label="Read" />;
    if (status === "DELIVERED") return <CheckCheck size={13} aria-label="Delivered" />;
    if (status === "SENT") return <Check size={13} aria-label="Sent" />;
    return <span className="text-[10px] uppercase tracking-wide">{status}</span>;
}

export default function Inbox() {
    const toast = useToast();
    const { user } = useAuth();

    const [conversations, setConversations] = useState([]);
    // Named `listSelection` because `selected` here is the open thread.
    const listSelection = useSelection(conversations);
    const [nextPageUrl, setNextPageUrl] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState("");
    const [search, setSearch] = useState("");
    const [filter, setFilter] = useState("all");

    const [labels, setLabels] = useState([]);
    const [showLabelModal, setShowLabelModal] = useState(false);

    const [selected, setSelected] = useState(null);
    const [messages, setMessages] = useState([]);
    const [olderUrl, setOlderUrl] = useState(null);
    const [loadingOlder, setLoadingOlder] = useState(false);
    const [notes, setNotes] = useState([]);
    const [threadLoading, setThreadLoading] = useState(false);
    const [showNotes, setShowNotes] = useState(false);

    const [reply, setReply] = useState({ body: "", media_items: [], buttons: [] });
    // Which canned reply the arrow keys are on, in the picker below.
    const [cannedIndex, setCannedIndex] = useState(0);
    const [showAttach, setShowAttach] = useState(false);
    const [showButtons, setShowButtons] = useState(false);
    const [replyError, setReplyError] = useState("");
    const [sending, setSending] = useState(false);
    const [noteBody, setNoteBody] = useState("");
    // The AI's read of the thread, and its draft. Both are shown to the agent
    // and neither is sent -- a suggestion lands in the composer for them to
    // edit, exactly like something they typed.
    // conversation id -> { username, at }, pruned by the sweep below.
    const [typingBy, setTypingBy] = useState({});
    // Loaded once: a handful of rows the composer matches against locally, so
    // expanding a shortcut costs no request until one is actually chosen.
    const [cannedReplies, setCannedReplies] = useState([]);
    const [summary, setSummary] = useState(null);
    const [summarising, setSummarising] = useState(false);
    const [suggesting, setSuggesting] = useState(false);
    const [busyAction, setBusyAction] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);

    // Width of the conversation-list column, dragged against the thread pane.
    // Restored from localStorage so a reload doesn't reset a chosen layout.
    const [listWidth, setListWidth] = useState(() => {
        const stored = Number(localStorage.getItem("inbox:list-width"));
        return stored >= 260 && stored <= 560 ? stored : 340;
    });
    const splitRef = useRef(null);
    const resizingRef = useRef(false);

    const threadRef = useRef(null);
    // The poll reads these without having to re-subscribe on every render.
    const selectedIdRef = useRef(null);
    const filterRef = useRef(filter);
    const searchRef = useRef(search);
    const atBottomRef = useRef(true);

    selectedIdRef.current = selected?.id ?? null;
    filterRef.current = filter;
    searchRef.current = search;

    const listParams = useCallback((filterId = filterRef.current, searchTerm = searchRef.current) => {
        const params = { ordering: "-last_message_at", ...(FILTERS.find((f) => f.id === filterId)?.params || {}) };
        if (searchTerm) params.search = searchTerm;
        return params;
    }, []);

    const loadConversations = useCallback(
        async ({ filterId, searchTerm, silent = false } = {}) => {
            if (!silent) setLoading(true);
            try {
                const response = await api.get("/inbox/conversations/", { params: listParams(filterId, searchTerm) });
                setConversations(response.data.results || []);
                setNextPageUrl(response.data.next || null);
                setError("");
                return response.data.results || [];
            } catch (err) {
                setError(apiErrorMessage(err, "Unable to load the inbox."));
                return [];
            } finally {
                setLoading(false);
            }
        },
        [listParams]
    );

    const loadMoreConversations = async () => {
        if (!nextPageUrl) return;
        try {
            const response = await api.get(nextPageUrl);
            setConversations((prev) => [...prev, ...(response.data.results || [])]);
            setNextPageUrl(response.data.next || null);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load more conversations."));
        }
    };

    useEffect(() => {
        loadConversations();
        api.get("/inbox/labels/")
            .then((response) => setLabels(response.data.results || []))
            .catch(() => setLabels([]));
        // A tenant with none simply never sees the picker.
        api.get("/teams/canned-replies/")
            .then((response) => setCannedReplies(response.data.results || []))
            .catch(() => setCannedReplies([]));
    }, [loadConversations]);

    // Only auto-scroll when the reader is already at the newest message, so a
    // poll never yanks them away from history they are reading. Scrolling the
    // pane itself (rather than scrollIntoView on a sentinel) lands past the
    // container's bottom padding, so the newest bubble is fully visible.
    useEffect(() => {
        const pane = threadRef.current;
        if (pane && atBottomRef.current) pane.scrollTop = pane.scrollHeight;
    }, [messages]);

    const markRead = useCallback(async (conversationId) => {
        try {
            await api.post(`/inbox/conversations/${conversationId}/mark-read/`);
            setConversations((prev) => prev.map((c) => (c.id === conversationId ? { ...c, unread_count: 0 } : c)));
            setSelected((prev) => (prev?.id === conversationId ? { ...prev, unread_count: 0 } : prev));
        } catch {
            // Non-fatal: the badge just stays until the next refresh.
        }
    }, []);

    const openConversation = async (conversation) => {
        setSelected(conversation);
        setThreadLoading(true);
        setReplyError("");
        // A summary belongs to one thread; carrying it across would describe
        // the wrong customer.
        setSummary(null);
        atBottomRef.current = true;
        try {
            const [messagesRes, notesRes] = await Promise.all([
                api.get(`/inbox/conversations/${conversation.id}/messages/`),
                api.get(`/inbox/conversations/${conversation.id}/notes/`),
            ]);
            setMessages(mergeMessages([], messagesRes.data.results || messagesRes.data || []));
            setOlderUrl(messagesRes.data.next || null);
            setNotes(notesRes.data.results || notesRes.data || []);
            if (conversation.unread_count > 0) await markRead(conversation.id);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load this conversation."));
        } finally {
            setThreadLoading(false);
        }
    };

    const loadOlderMessages = async () => {
        if (!olderUrl) return;
        setLoadingOlder(true);
        atBottomRef.current = false;
        try {
            const response = await api.get(olderUrl);
            setMessages((prev) => mergeMessages(prev, response.data.results || []));
            setOlderUrl(response.data.next || null);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load earlier messages."));
        } finally {
            setLoadingOlder(false);
        }
    };

    const refreshThread = useCallback(async (conversationId) => {
        const response = await api.get(`/inbox/conversations/${conversationId}/messages/`);
        setMessages((prev) => mergeMessages(prev, response.data.results || []));
    }, []);

    /**
     * One event off the live stream.
     *
     * Deliberately light: a message for the thread on screen is merged in
     * directly, and everything else asks the API for the truth rather than
     * trying to patch a conversation row from a partial payload. The stream
     * says *that* something changed; the list endpoint stays the authority on
     * what it now looks like.
     */
    const handleRealtimeEvent = useCallback(
        (event) => {
            const { type, data } = event;

            if (type === "typing") {
                // Your own keystrokes, echoed back off the tenant channel.
                if (data.user === user?.id) return;
                setTypingBy((current) => ({
                    ...current,
                    [data.conversation]: { username: data.username, at: Date.now() },
                }));
                return;
            }

            if (type === "message.received" || type === "message.sent" || type === "message.updated") {
                if (data.conversation === selectedIdRef.current) {
                    // Refetch rather than append the payload: it is trimmed to
                    // 500 characters and carries no attachments or buttons.
                    refreshThread(data.conversation).catch(() => {});
                    if (type === "message.received") markRead(data.conversation).catch(() => {});
                }
                loadConversations({ silent: true });
                return;
            }

            if (type === "conversation.updated") {
                loadConversations({ silent: true });
            }
        },
        [loadConversations, refreshThread, markRead, user?.id]
    );

    const { connected: live } = useRealtime({ onEvent: handleRealtimeEvent });

    // Poll the list (and the open thread) so inbound replies appear on their
    // own. With the stream up this drops to a slow backstop, which is what
    // closes the gap left by a reconnect rather than leaving it open.
    useEffect(() => {
        const interval = live ? LIVE_POLL_MS : POLL_MS;
        const timer = setInterval(async () => {
            if (document.hidden) return;
            const fresh = await loadConversations({ silent: true });
            const conversationId = selectedIdRef.current;
            if (!conversationId) return;
            try {
                await refreshThread(conversationId);
                const updated = fresh.find((c) => c.id === conversationId);
                if (updated?.unread_count > 0) await markRead(conversationId);
            } catch {
                // Transient network errors are swallowed; the next tick retries.
            }
        }, interval);
        return () => clearInterval(timer);
    }, [live, loadConversations, refreshThread, markRead]);

    // Expire typing bubbles. One sweep for all of them, and only while
    // somebody is actually typing.
    useEffect(() => {
        if (Object.keys(typingBy).length === 0) return undefined;
        const timer = setInterval(() => {
            const cutoff = Date.now() - TYPING_TTL_MS;
            setTypingBy((current) => {
                const kept = Object.fromEntries(
                    Object.entries(current).filter(([, value]) => value.at > cutoff)
                );
                return Object.keys(kept).length === Object.keys(current).length ? current : kept;
            });
        }, 1000);
        return () => clearInterval(timer);
    }, [typingBy]);

    // Tracked on window (not the handle) so a fast drag that outruns the
    // cursor doesn't drop the resize when the pointer leaves the handle.
    useEffect(() => {
        const onMove = (event) => {
            if (!resizingRef.current || !splitRef.current) return;
            const rect = splitRef.current.getBoundingClientRect();
            const max = Math.min(560, rect.width - 320);
            setListWidth(Math.min(max, Math.max(260, event.clientX - rect.left)));
        };
        const onUp = () => {
            if (!resizingRef.current) return;
            resizingRef.current = false;
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            setListWidth((width) => {
                localStorage.setItem("inbox:list-width", String(width));
                return width;
            });
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
        return () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup", onUp);
        };
    }, []);

    const startResize = (event) => {
        event.preventDefault();
        resizingRef.current = true;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
    };

    const nudgeListWidth = (delta) => {
        setListWidth((width) => {
            const next = Math.min(560, Math.max(260, width + delta));
            localStorage.setItem("inbox:list-width", String(next));
            return next;
        });
    };

    const manualRefresh = async () => {
        setRefreshing(true);
        await loadConversations({ silent: true });
        if (selected) {
            await refreshThread(selected.id).catch(() => {});
        }
        setRefreshing(false);
    };

    const applyFilter = (filterId) => {
        setFilter(filterId);
        loadConversations({ filterId });
    };

    /**
     * The canned replies a half-typed shortcut could still become.
     *
     * Only while the box holds nothing but "/word" — once there is a space the
     * agent is writing a sentence, and a picker covering the composer would be
     * in the way rather than helpful.
     */
    const cannedMatches = (() => {
        const match = /^\/(\S*)$/.exec(reply.body);
        if (!match || cannedReplies.length === 0) return [];
        const query = match[1].toLowerCase();
        return cannedReplies
            .filter(
                (item) =>
                    item.shortcut.toLowerCase().startsWith(query) || item.title.toLowerCase().includes(query)
            )
            .slice(0, 6);
    })();

    const insertCannedReply = (item) => {
        setReply((current) => ({ ...current, body: item.body }));
        setCannedIndex(0);
        // Counted where it is actually used, so the tally means "inserted"
        // rather than "was on screen once".
        api.post(`/teams/canned-replies/${item.id}/use/`).catch(() => {});
    };

    /**
     * Tell everyone else this agent is typing.
     *
     * Throttled to one announcement per TTL rather than fired per keystroke:
     * the indicator only has to be kept alive, and a request per character
     * would be a lot of traffic to say one thing.
     */
    const lastTypingSentRef = useRef(0);
    const announceTyping = useCallback((conversationId) => {
        const now = Date.now();
        if (now - lastTypingSentRef.current < TYPING_TTL_MS / 2) return;
        lastTypingSentRef.current = now;
        api.post("/realtime/typing/", { conversation: conversationId }).catch(() => {});
    }, []);

    /**
     * A draft in the composer, never a sent message.
     *
     * It replaces whatever is typed, so it is offered only when the composer
     * is empty -- losing a half-written reply to a misclick is not a trade
     * worth making for one fewer click.
     */
    const suggestReply = async () => {
        setSuggesting(true);
        setReplyError("");
        try {
            const response = await api.post("/ai/suggest-reply/", { conversation: selected.id });
            setReply((current) => ({ ...current, body: response.data.suggestion }));
        } catch (err) {
            setReplyError(apiErrorMessage(err, "Unable to draft a reply."));
        } finally {
            setSuggesting(false);
        }
    };

    const summariseConversation = async () => {
        setSummarising(true);
        try {
            const response = await api.post("/ai/summarise/", { conversation: selected.id });
            setSummary(response.data.summary);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to summarise this conversation."));
        } finally {
            setSummarising(false);
        }
    };

    const sendReply = async (event) => {
        event.preventDefault();
        const attachments = reply.media_items;
        if (!reply.body.trim() && attachments.length === 0) return;
        setSending(true);
        setReplyError("");
        atBottomRef.current = true;
        try {
            // The API derives message_type and media_url from the first
            // attachment, so the composer only has to send the list.
            const payload = attachments.length
                ? { media_items: attachments, body: reply.body }
                : { message_type: "TEXT", body: reply.body };
            // Buttons turn the reply into an interactive message; the API
            // rejects them without body text, which the send button enforces.
            if (reply.buttons.length) payload.buttons = reply.buttons;
            const response = await api.post(`/inbox/conversations/${selected.id}/reply/`, payload);
            setMessages((prev) => mergeMessages(prev, [response.data]));
            setReply({ body: "", media_items: [], buttons: [] });
            setShowAttach(false);
            setShowButtons(false);
            loadConversations({ silent: true });
        } catch (err) {
            setReplyError(apiErrorMessage(err, "Unable to send this reply."));
        } finally {
            setSending(false);
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const ids = listSelection.selectedIds;
        const result = await runBulk("/inbox/conversations/bulk/", { action, ids });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            setError(
                describeBulkFailures(result, (id) => {
                    const conversation = conversations.find((entry) => entry.id === id);
                    return conversation ? conversation.contact_name || conversation.contact_phone : null;
                })
            );
        }
        // A deleted thread must not stay open in the reading pane.
        if (action === "delete" && selected && ids.includes(selected.id)) setSelected(null);
        listSelection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        await loadConversations({ silent: true });
    };

    const addNote = async (event) => {
        event.preventDefault();
        if (!noteBody.trim()) return;
        try {
            const response = await api.post(`/inbox/conversations/${selected.id}/notes/`, { body: noteBody });
            setNotes((prev) => [...prev, response.data]);
            setNoteBody("");
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to add this note."));
        }
    };

    /** Replace a conversation everywhere it is held after a server action. */
    const applyConversation = (updated) => {
        setSelected(updated);
        setConversations((prev) => prev.map((c) => (c.id === updated.id ? { ...c, ...updated } : c)));
    };

    const toggleStatus = async () => {
        const action = selected.status === "OPEN" ? "close" : "reopen";
        setBusyAction(true);
        try {
            const response = await api.post(`/inbox/conversations/${selected.id}/${action}/`);
            applyConversation(response.data);
            toast.success(action === "close" ? "Conversation closed." : "Conversation reopened.");
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to update this conversation."));
        } finally {
            setBusyAction(false);
        }
    };

    const toggleAssignment = async () => {
        const assignToMe = selected.assigned_agent !== user?.id;
        setBusyAction(true);
        try {
            const response = await api.post(
                `/inbox/conversations/${selected.id}/assign/`,
                assignToMe ? { agent_id: user.id } : {}
            );
            applyConversation(response.data);
            toast.success(assignToMe ? "Assigned to you." : "Assignment cleared.");
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to change the assignment."));
        } finally {
            setBusyAction(false);
        }
    };

    const setConversationLabels = async (labelIds) => {
        try {
            const response = await api.patch(`/inbox/conversations/${selected.id}/`, { labels: labelIds });
            applyConversation(response.data);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to update labels."));
        }
    };

    /** Deletes the whole thread; a later inbound message starts a fresh one. */
    const deleteConversation = async (conversation) => {
        try {
            await api.delete(`/inbox/conversations/${conversation.id}/`);
            setConversations((prev) => prev.filter((c) => c.id !== conversation.id));
            if (selectedIdRef.current === conversation.id) {
                setSelected(null);
                setMessages([]);
                setNotes([]);
                setOlderUrl(null);
            }
            toast.success("Conversation deleted.");
        } catch (err) {
            toast.error(apiErrorMessage(err, "Unable to delete this conversation."));
        } finally {
            setPendingDelete(null);
            loadConversations({ silent: true });
        }
    };

    const createLabel = async (name, color) => {
        const response = await api.post("/inbox/labels/", { name, color });
        setLabels((prev) => [...prev, response.data]);
        return response.data;
    };

    if (loading) return <Spinner />;

    const unreadTotal = conversations.reduce((sum, conversation) => sum + (conversation.unread_count || 0), 0);
    const labelsById = new Map(labels.map((label) => [label.id, label]));
    const isMine = selected?.assigned_agent === user?.id;

    return (
        <div>
            <PageHeader
                title="Inbox"
                description={
                    unreadTotal > 0
                        ? `${unreadTotal} unread message${unreadTotal === 1 ? "" : "s"} across ${conversations.length} conversation${conversations.length === 1 ? "" : "s"}`
                        : `${conversations.length} conversation${conversations.length === 1 ? "" : "s"}`
                }
                actions={
                    <>
                        <span
                            className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-500 mr-1"
                            title={
                                live
                                    ? "New messages arrive as they happen."
                                    : `Live updates are not connected — checking every ${POLL_MS / 1000}s instead.`
                            }
                        >
                            <span
                                className={`w-1.5 h-1.5 rounded-full ${live ? "bg-brand-500" : "bg-ink-300"}`}
                                aria-hidden="true"
                            />
                            {live ? "Live" : "Polling"}
                        </span>
                        <Button variant="secondary" icon={Tag} onClick={() => setShowLabelModal(true)}>
                            Labels
                        </Button>
                        <Button variant="secondary" icon={RefreshCw} loading={refreshing} onClick={manualRefresh}>
                            Refresh
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            <div
                ref={splitRef}
                style={{ "--list-w": `${listWidth}px` }}
                className="bg-surface border border-ink-200 rounded-card shadow-card overflow-hidden grid lg:grid-cols-[var(--list-w)_8px_1fr] h-[calc(100vh-15rem)] min-h-[28rem]"
            >
                {/* Conversation list — hidden on mobile once a thread is open. */}
                <div className={`flex flex-col border-ink-200 lg:border-r min-w-0 min-h-0 ${selected ? "hidden lg:flex" : "flex"}`}>
                    <div className="p-3 border-b border-ink-200">
                        <form
                            onSubmit={(event) => {
                                event.preventDefault();
                                loadConversations();
                            }}
                        >
                            <SearchInput
                                value={search}
                                onChange={(event) => setSearch(event.target.value)}
                                onClear={() => {
                                    setSearch("");
                                    loadConversations({ searchTerm: "" });
                                }}
                                placeholder="Search name or number..."
                            />
                        </form>
                        {/* Segmented filter rather than <Tabs>: this sits inside the
                            list pane, where the tab bar's own bottom border and
                            page-level margin would both be wrong. */}
                        <div className="flex gap-1 mt-2 p-0.5 rounded-lg bg-ink-100" role="tablist">
                            {FILTERS.map((entry) => (
                                <button
                                    key={entry.id}
                                    type="button"
                                    role="tab"
                                    aria-selected={filter === entry.id}
                                    onClick={() => applyFilter(entry.id)}
                                    className={`flex-1 rounded-md px-2 py-1.5 text-xs font-semibold cursor-pointer border-none transition-colors ${
                                        filter === entry.id
                                            ? "bg-surface text-ink-900 shadow-card"
                                            : "bg-transparent text-ink-500 hover:text-ink-800"
                                    }`}
                                >
                                    {entry.label}
                                </button>
                            ))}
                        </div>
                    </div>

                    {conversations.length > 0 && (
                        <label className="shrink-0 flex items-center gap-2 border-b border-ink-200 px-3.5 py-2 text-xs font-semibold text-ink-500 cursor-pointer select-none">
                            <Checkbox
                                checked={listSelection.allSelected}
                                indeterminate={listSelection.someSelected}
                                onChange={listSelection.toggleAll}
                                aria-label={
                                    listSelection.allSelected ? "Deselect all conversations" : "Select all conversations"
                                }
                            />
                            Select all {conversations.length}
                        </label>
                    )}

                    {listSelection.selectedCount > 0 && (
                        <div className="shrink-0 border-b border-ink-200 p-2">
                            <BulkActionBar
                                count={listSelection.selectedCount}
                                label="chat"
                                onClear={listSelection.clear}
                                className="static mb-0"
                            >
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={MarkRead}
                                    disabled={bulkBusy}
                                    onClick={() => runBulkAction("mark_read")}
                                >
                                    Mark read
                                </Button>
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    disabled={bulkBusy}
                                    onClick={() => runBulkAction("close")}
                                >
                                    Close
                                </Button>
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    disabled={bulkBusy}
                                    onClick={() => runBulkAction("reopen")}
                                >
                                    Reopen
                                </Button>
                                <Button
                                    size="sm"
                                    variant="danger"
                                    icon={Trash2}
                                    disabled={bulkBusy}
                                    onClick={() => setPendingBulk("delete")}
                                >
                                    Delete
                                </Button>
                            </BulkActionBar>
                        </div>
                    )}

                    <div className="scroll-y flex-1 min-h-0">
                        {conversations.length === 0 ? (
                            <p className="text-sm text-ink-500 p-4 text-center m-0">
                                {filter === "all" && !search
                                    ? "No conversations yet. One appears here as soon as somebody messages a connected number."
                                    : "No conversations match this filter."}
                            </p>
                        ) : (
                            conversations.map((conversation) => {
                                const isActive = selected?.id === conversation.id;
                                const isTicked = listSelection.isSelected(conversation.id);
                                return (
                                    // The checkbox sits outside the row button:
                                    // nesting an input inside a button makes it
                                    // unclickable, and ticking must never also
                                    // open the thread.
                                    <div
                                        key={conversation.id}
                                        className={`flex items-start border-b border-ink-100 transition-colors ${
                                            isActive ? "bg-brand-50" : isTicked ? "bg-brand-50/50" : "hover:bg-ink-50"
                                        }`}
                                    >
                                    <span className="shrink-0 pl-3.5 pt-4">
                                        <Checkbox
                                            checked={isTicked}
                                            onChange={() => listSelection.toggle(conversation.id)}
                                            aria-label={`Select conversation with ${
                                                conversation.contact_name || conversation.contact_phone
                                            }`}
                                        />
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => openConversation(conversation)}
                                        className="flex-1 min-w-0 text-left flex items-start gap-3 px-3 py-3 border-none bg-transparent cursor-pointer"
                                    >
                                        <span
                                            className={`shrink-0 w-9 h-9 mt-0.5 rounded-full flex items-center justify-center text-sm font-bold ${
                                                isActive ? "bg-brand-600 text-white" : "bg-ink-100 text-ink-600"
                                            }`}
                                        >
                                            {initials(conversation.contact_name || conversation.contact_phone)}
                                        </span>
                                        <span className="flex-1 min-w-0">
                                            <span className="flex items-baseline gap-2">
                                                <span className="flex-1 min-w-0 font-semibold text-sm text-ink-900 truncate">
                                                    {conversation.contact_name || conversation.contact_phone}
                                                </span>
                                                <span className="shrink-0 text-[11px] text-ink-400 tabular-nums">
                                                    {formatRelative(conversation.last_message_at)}
                                                </span>
                                            </span>
                                            <span className="block text-xs text-ink-500 truncate mt-0.5">
                                                {conversation.last_message_preview
                                                    ? `${conversation.last_message_direction === "OUTBOUND" ? "You: " : ""}${conversation.last_message_preview}`
                                                    : conversation.contact_phone}
                                            </span>
                                            {conversation.labels?.length > 0 && (
                                                <span className="flex flex-wrap gap-1 mt-1.5">
                                                    {conversation.labels.map((labelId) => {
                                                        const label = labelsById.get(labelId);
                                                        if (!label) return null;
                                                        return (
                                                            <span
                                                                key={labelId}
                                                                className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold text-white"
                                                                style={{ backgroundColor: label.color }}
                                                            >
                                                                {label.name}
                                                            </span>
                                                        );
                                                    })}
                                                </span>
                                            )}
                                        </span>
                                        {conversation.unread_count > 0 && (
                                            <span className="shrink-0 mt-1 bg-brand-600 text-white text-[11px] font-bold rounded-full min-w-5 h-5 px-1.5 flex items-center justify-center">
                                                {conversation.unread_count}
                                            </span>
                                        )}
                                    </button>
                                    </div>
                                );
                            })
                        )}
                        {nextPageUrl && (
                            <div className="p-3">
                                <Button variant="secondary" size="sm" className="w-full" onClick={loadMoreConversations}>
                                    Load more conversations
                                </Button>
                            </div>
                        )}
                    </div>
                </div>

                {/* Drag to resize the list column; arrow keys nudge it when focused. */}
                <div
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize conversation list"
                    aria-valuenow={listWidth}
                    aria-valuemin={260}
                    aria-valuemax={560}
                    tabIndex={0}
                    onMouseDown={startResize}
                    onKeyDown={(event) => {
                        if (event.key === "ArrowLeft") {
                            event.preventDefault();
                            nudgeListWidth(-16);
                        } else if (event.key === "ArrowRight") {
                            event.preventDefault();
                            nudgeListWidth(16);
                        }
                    }}
                    className="hidden lg:flex w-2 shrink-0 cursor-col-resize items-center justify-center bg-transparent hover:bg-ink-100 focus:bg-ink-100 focus:outline-none transition-colors group"
                >
                    <span className="w-1 h-8 rounded-full bg-ink-200 group-hover:bg-ink-300" aria-hidden="true" />
                </div>

                {/* Thread */}
                <div className={`flex-col overflow-hidden min-w-0 min-h-0 ${selected ? "flex" : "hidden lg:flex"}`}>
                    {!selected ? (
                        <div className="flex-1 flex items-center justify-center p-6">
                            <EmptyState
                                variant="plain"
                                icon={MessageSquare}
                                title="No conversation selected"
                                description="Pick a conversation from the list to read the thread and reply."
                            />
                        </div>
                    ) : threadLoading ? (
                        <Spinner />
                    ) : (
                        <>
                            <div className="shrink-0 border-b border-ink-200 px-3.5 py-3 flex items-center gap-2 flex-wrap">
                                <button
                                    type="button"
                                    onClick={() => setSelected(null)}
                                    aria-label="Back to conversations"
                                    className="lg:hidden shrink-0 w-8 h-8 inline-flex items-center justify-center rounded-lg text-ink-500 hover:bg-ink-100 cursor-pointer bg-transparent border-none"
                                >
                                    <ArrowLeft size={17} />
                                </button>
                                <div className="min-w-0 flex-1">
                                    <p className="font-semibold text-sm text-ink-900 truncate m-0">
                                        {selected.contact_name || selected.contact_phone}
                                    </p>
                                    <p className="text-xs text-ink-500 truncate m-0 tabular-nums">
                                        {selected.contact_phone}
                                        {selected.assigned_agent_username &&
                                            ` · assigned to ${selected.assigned_agent_username}`}
                                    </p>
                                </div>
                                <StatusBadge status={selected.status} />
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={UserCheck}
                                    loading={busyAction}
                                    onClick={toggleAssignment}
                                >
                                    {isMine ? "Unassign" : "Assign to me"}
                                </Button>
                                <Button size="sm" variant="secondary" loading={busyAction} onClick={toggleStatus}>
                                    {selected.status === "OPEN" ? "Close" : "Reopen"}
                                </Button>
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={Sparkles}
                                    loading={summarising}
                                    onClick={summariseConversation}
                                    aria-label="Summarise this conversation"
                                >
                                    <span className="hidden sm:inline">Summarise</span>
                                </Button>
                                <Button
                                    size="sm"
                                    variant="ghost"
                                    icon={Trash2}
                                    className="text-red-600 hover:bg-red-50 hover:text-red-700"
                                    aria-label="Delete conversation"
                                    onClick={() => setPendingDelete(selected)}
                                />
                            </div>

                            {summary && (
                                <div className="shrink-0 border-b border-ink-200 bg-violet-50 px-3.5 py-2.5 flex items-start gap-2">
                                    <Sparkles size={14} className="text-violet-500 shrink-0 mt-0.5" aria-hidden="true" />
                                    <p className="text-xs text-violet-900 m-0 flex-1 whitespace-pre-wrap">{summary}</p>
                                    <button
                                        type="button"
                                        onClick={() => setSummary(null)}
                                        aria-label="Dismiss summary"
                                        className="shrink-0 cursor-pointer border-none bg-transparent p-0.5 text-violet-400 hover:text-violet-700"
                                    >
                                        <X size={13} aria-hidden="true" />
                                    </button>
                                </div>
                            )}

                            {labels.length > 0 && (
                                <div className="shrink-0 border-b border-ink-200 px-3.5 py-2 flex items-center gap-1.5 flex-wrap">
                                    <Tag size={13} className="text-ink-400 shrink-0" aria-hidden="true" />
                                    {labels.map((label) => {
                                        const applied = selected.labels?.includes(label.id);
                                        return (
                                            <button
                                                key={label.id}
                                                type="button"
                                                onClick={() =>
                                                    setConversationLabels(
                                                        applied
                                                            ? selected.labels.filter((id) => id !== label.id)
                                                            : [...(selected.labels || []), label.id]
                                                    )
                                                }
                                                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold cursor-pointer transition-colors ${
                                                    applied
                                                        ? "text-white border-transparent"
                                                        : "bg-surface text-ink-500 border-ink-200 hover:border-ink-300"
                                                }`}
                                                style={applied ? { backgroundColor: label.color } : undefined}
                                            >
                                                {label.name}
                                                {applied && <X size={11} aria-hidden="true" />}
                                            </button>
                                        );
                                    })}
                                </div>
                            )}

                            <div
                                ref={threadRef}
                                className="scroll-y flex-1 p-4 space-y-2.5 bg-ink-50"
                                onScroll={(event) => {
                                    const element = event.currentTarget;
                                    atBottomRef.current =
                                        element.scrollHeight - element.scrollTop - element.clientHeight < 80;
                                }}
                            >
                                {olderUrl && (
                                    <div className="text-center pb-1">
                                        <Button
                                            size="sm"
                                            variant="secondary"
                                            icon={ChevronUp}
                                            loading={loadingOlder}
                                            onClick={loadOlderMessages}
                                        >
                                            Load earlier messages
                                        </Button>
                                    </div>
                                )}
                                {messages.map((message, index) => {
                                    const outbound = message.direction === "OUTBOUND";
                                    const previous = messages[index - 1];
                                    const showDay =
                                        !previous ||
                                        new Date(previous.queued_at).toDateString() !==
                                            new Date(message.queued_at).toDateString();
                                    return (
                                        <div key={message.id}>
                                            {showDay && (
                                                <p className="text-center text-[11px] font-semibold text-ink-400 uppercase tracking-wide my-3">
                                                    {formatDay(message.queued_at)}
                                                </p>
                                            )}
                                            <div className={`flex ${outbound ? "justify-end" : "justify-start"}`}>
                                                <div
                                                    className={`max-w-[85%] sm:max-w-[70%] rounded-2xl px-3.5 py-2 text-sm shadow-card ${
                                                        outbound
                                                            ? "bg-brand-600 text-white rounded-br-sm"
                                                            : "bg-surface text-ink-800 border border-ink-200 rounded-bl-sm"
                                                    }`}
                                                >
                                                    {/* One line per attachment: a message can now
                                                        carry several, and an older single-media
                                                        message resolves to exactly one. */}
                                                    {toMediaItems(message).map((item, position) => (
                                                        <a
                                                            key={`${item.url}-${position}`}
                                                            href={item.url}
                                                            target="_blank"
                                                            rel="noreferrer"
                                                            className={`flex items-center gap-1.5 text-xs font-semibold mb-1 ${
                                                                outbound ? "text-brand-100" : "text-brand-700"
                                                            }`}
                                                        >
                                                            <Paperclip size={12} aria-hidden="true" />
                                                            {item.filename || item.type.toLowerCase()}
                                                            {item.caption ? ` — ${item.caption}` : ""}
                                                        </a>
                                                    ))}
                                                    <p className="whitespace-pre-wrap m-0">
                                                        {message.replied_button_id && (
                                                            <MousePointerClick
                                                                size={12}
                                                                aria-label="Tapped a button"
                                                                className="inline-block mr-1.5 -mt-0.5 text-brand-600"
                                                            />
                                                        )}
                                                        {message.body || message.caption}
                                                    </p>
                                                    {/* The buttons the contact saw under this
                                                        message, as WhatsApp shows them. */}
                                                    {message.buttons?.length > 0 && (
                                                        <div
                                                            className={`flex flex-col gap-1 mt-2 pt-2 border-t ${
                                                                outbound ? "border-white/25" : "border-ink-200"
                                                            }`}
                                                        >
                                                            {message.buttons.map((button, position) => (
                                                                <span
                                                                    key={button.id || position}
                                                                    className={`rounded-lg px-2.5 py-1 text-center text-xs font-semibold ${
                                                                        outbound
                                                                            ? "bg-white/15 text-white"
                                                                            : "bg-ink-100 text-brand-700"
                                                                    }`}
                                                                >
                                                                    {button.title}
                                                                </span>
                                                            ))}
                                                        </div>
                                                    )}
                                                    {message.status === "FAILED" && message.error_message && (
                                                        <p
                                                            className={`text-[11px] mt-1 mb-0 ${
                                                                outbound ? "text-red-100" : "text-red-600"
                                                            }`}
                                                        >
                                                            {message.error_message}
                                                        </p>
                                                    )}
                                                    <div
                                                        className={`flex items-center justify-end gap-1 mt-1 text-[10px] ${
                                                            outbound ? "text-brand-100" : "text-ink-400"
                                                        }`}
                                                    >
                                                        {message.is_automated && (
                                                            <Bot size={11} aria-label="Sent by automation" />
                                                        )}
                                                        <span className="tabular-nums">{formatTime(message.queued_at)}</span>
                                                        {outbound && <MessageStatusIcon status={message.status} />}
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}

                                {typingBy[selected.id] && (
                                    <div className="flex justify-start">
                                        <div className="rounded-2xl rounded-bl-sm bg-surface border border-ink-200 px-3.5 py-2 shadow-card">
                                            <span className="text-xs text-ink-500">
                                                {typingBy[selected.id].username} is typing
                                            </span>
                                            <span className="inline-flex gap-0.5 ml-1.5 align-middle">
                                                <span className="w-1 h-1 rounded-full bg-ink-400 animate-pulse" />
                                                <span
                                                    className="w-1 h-1 rounded-full bg-ink-400 animate-pulse"
                                                    style={{ animationDelay: "150ms" }}
                                                />
                                                <span
                                                    className="w-1 h-1 rounded-full bg-ink-400 animate-pulse"
                                                    style={{ animationDelay: "300ms" }}
                                                />
                                            </span>
                                        </div>
                                    </div>
                                )}
                            </div>

                            <form onSubmit={sendReply} className="shrink-0 border-t border-ink-200 p-3 bg-surface">
                                <Alert>{replyError}</Alert>
                                {showAttach && (
                                    <div className="mb-2 p-3 rounded-lg bg-ink-50 border border-ink-200">
                                        <MediaPicker
                                            value={reply.media_items}
                                            onChange={(media_items) => setReply({ ...reply, media_items })}
                                            label="Attachments"
                                            hint="Each file is sent as its own WhatsApp message, in this order. One credit covers the whole reply."
                                        />
                                    </div>
                                )}
                                {showButtons && (
                                    <div className="mb-2 p-3 rounded-lg bg-ink-50 border border-ink-200">
                                        <ButtonBuilder
                                            value={reply.buttons}
                                            onChange={(buttons) => setReply({ ...reply, buttons })}
                                            hint="The contact taps instead of typing. A tap can trigger an automation rule matched on the button id."
                                        />
                                    </div>
                                )}
                                {cannedMatches.length > 0 && (
                                    <ul className="list-none p-0 m-0 mb-2 rounded-lg border border-ink-200 bg-surface shadow-overlay overflow-hidden">
                                        {cannedMatches.map((item, index) => (
                                            <li key={item.id}>
                                                <button
                                                    type="button"
                                                    onMouseEnter={() => setCannedIndex(index)}
                                                    onClick={() => insertCannedReply(item)}
                                                    className={`w-full text-left px-3 py-2 cursor-pointer border-none transition-colors ${
                                                        index === cannedIndex ? "bg-ink-100" : "bg-transparent"
                                                    }`}
                                                >
                                                    <span className="text-xs font-bold text-brand-700">
                                                        /{item.shortcut}
                                                    </span>
                                                    <span className="text-xs font-semibold text-ink-800 ml-2">
                                                        {item.title}
                                                    </span>
                                                    <span className="block text-xs text-ink-500 truncate">
                                                        {item.body}
                                                    </span>
                                                </button>
                                            </li>
                                        ))}
                                    </ul>
                                )}

                                <div className="flex gap-2 items-end">
                                    <Button
                                        type="button"
                                        variant="secondary"
                                        icon={Paperclip}
                                        aria-label={
                                            reply.media_items.length
                                                ? `Attachments (${reply.media_items.length})`
                                                : "Attach media"
                                        }
                                        onClick={() => setShowAttach((value) => !value)}
                                        className="shrink-0"
                                    >
                                        {reply.media_items.length > 0 && (
                                            <span className="tabular-nums">{reply.media_items.length}</span>
                                        )}
                                    </Button>
                                    <Button
                                        type="button"
                                        variant="secondary"
                                        icon={MousePointerClick}
                                        aria-label={
                                            reply.buttons.length
                                                ? `Buttons (${reply.buttons.length})`
                                                : "Add buttons"
                                        }
                                        onClick={() => setShowButtons((value) => !value)}
                                        className="shrink-0"
                                    >
                                        {reply.buttons.length > 0 && (
                                            <span className="tabular-nums">{reply.buttons.length}</span>
                                        )}
                                    </Button>
                                    <Button
                                        type="button"
                                        variant="secondary"
                                        icon={Sparkles}
                                        loading={suggesting}
                                        disabled={reply.body.trim().length > 0}
                                        onClick={suggestReply}
                                        aria-label="Draft a reply with AI"
                                        title={
                                            reply.body.trim()
                                                ? "Clear the box first — a draft would replace what you have typed."
                                                : "Draft a reply with AI"
                                        }
                                        className="shrink-0"
                                    />
                                    <Textarea
                                        value={reply.body}
                                        onChange={(event) => {
                                            setReply({ ...reply, body: event.target.value });
                                            setCannedIndex(0);
                                            announceTyping(selected.id);
                                        }}
                                        onKeyDown={(event) => {
                                            // Only while the picker is open, so
                                            // Enter still means newline when
                                            // writing an ordinary reply.
                                            if (cannedMatches.length === 0) return;
                                            if (event.key === "ArrowDown") {
                                                event.preventDefault();
                                                setCannedIndex((index) => (index + 1) % cannedMatches.length);
                                            } else if (event.key === "ArrowUp") {
                                                event.preventDefault();
                                                setCannedIndex(
                                                    (index) => (index - 1 + cannedMatches.length) % cannedMatches.length
                                                );
                                            } else if (event.key === "Enter" || event.key === "Tab") {
                                                event.preventDefault();
                                                insertCannedReply(cannedMatches[cannedIndex] || cannedMatches[0]);
                                            } else if (event.key === "Escape") {
                                                event.preventDefault();
                                                setReply((current) => ({ ...current, body: "" }));
                                            }
                                        }}
                                        rows={2}
                                        placeholder="Type a reply..."
                                        className="mb-0"
                                    />
                                    <Button
                                        type="submit"
                                        icon={Send}
                                        loading={sending}
                                        disabled={
                                            reply.buttons.length
                                                ? !reply.body.trim()
                                                : !reply.body.trim() && reply.media_items.length === 0
                                        }
                                        className="shrink-0"
                                    >
                                        <span className="hidden sm:inline">Send</span>
                                    </Button>
                                </div>
                                <p className="text-[11px] text-ink-400 mt-1.5 mb-0">
                                    Each reply reserves 1 credit and is sent from the number this conversation arrived on.
                                </p>
                            </form>

                            <div className="shrink-0 border-t border-ink-200 bg-surface">
                                <button
                                    type="button"
                                    onClick={() => setShowNotes((value) => !value)}
                                    className="w-full flex items-center gap-2 px-3.5 py-2.5 text-xs font-bold uppercase tracking-wide text-ink-500 hover:text-ink-800 cursor-pointer bg-transparent border-none transition-colors"
                                >
                                    <StickyNote size={14} aria-hidden="true" />
                                    Internal notes
                                    <span className="ml-auto text-ink-400 normal-case font-medium tracking-normal">
                                        {notes.length} · {showNotes ? "Hide" : "Show"}
                                    </span>
                                </button>
                                {showNotes && (
                                    <div className="px-3.5 pb-3">
                                        <div className="scroll-y max-h-28 mb-2">
                                            {notes.length === 0 ? (
                                                <p className="text-xs text-ink-400 m-0">No notes on this conversation.</p>
                                            ) : (
                                                notes.map((note) => (
                                                    <p key={note.id} className="text-xs text-ink-600 mt-0 mb-1.5">
                                                        <strong className="text-ink-800">{note.author_username}:</strong>{" "}
                                                        {note.body}
                                                        <span className="text-ink-400"> · {formatDay(note.created_at)}</span>
                                                    </p>
                                                ))
                                            )}
                                        </div>
                                        <form onSubmit={addNote} className="flex gap-2">
                                            <input
                                                value={noteBody}
                                                onChange={(event) => setNoteBody(event.target.value)}
                                                placeholder="Add a note (only your team sees this)..."
                                                className="flex-1 min-w-0 text-xs border border-ink-200 rounded-lg px-2.5 py-1.5 focus:outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-500/15"
                                            />
                                            <Button type="submit" size="sm" variant="secondary" disabled={!noteBody.trim()}>
                                                Add
                                            </Button>
                                        </form>
                                    </div>
                                )}
                            </div>
                        </>
                    )}
                </div>
            </div>

            {showLabelModal && (
                <LabelManager labels={labels} onCreate={createLabel} onClose={() => setShowLabelModal(false)} />
            )}

            {pendingBulk === "delete" && (
                <ConfirmDialog
                    title={`Delete ${listSelection.selectedCount} conversation${
                        listSelection.selectedCount === 1 ? "" : "s"
                    }`}
                    message={`Delete the ${listSelection.selectedCount} selected conversation${
                        listSelection.selectedCount === 1 ? "" : "s"
                    }? Their messages and internal notes go with them. A new message from the same contact starts a fresh thread.`}
                    confirmLabel={`Delete ${listSelection.selectedCount}`}
                    onConfirm={() => runBulkAction("delete")}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete conversation"
                    message={`Delete the chat with ${pendingDelete.contact_name || pendingDelete.contact_phone}? Its messages and internal notes are removed for the whole team. The contact is kept, and a new message from them starts a fresh conversation.`}
                    confirmLabel="Delete conversation"
                    onConfirm={() => deleteConversation(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

/** Creates the tenant's conversation labels; applying one happens in the thread. */
function LabelManager({ labels, onCreate, onClose }) {
    const [name, setName] = useState("");
    const [color, setColor] = useState("#2563eb");
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        if (!name.trim()) return;
        setSaving(true);
        setError("");
        try {
            await onCreate(name.trim(), color);
            setName("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to create this label."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title="Conversation labels" description="Labels are shared across your team." onClose={onClose}>
            <div className="flex flex-wrap gap-2 mb-5">
                {labels.length === 0 ? (
                    <p className="text-sm text-ink-500 m-0">No labels yet.</p>
                ) : (
                    labels.map((label) => (
                        <span
                            key={label.id}
                            className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-xs font-semibold bg-ink-100 text-ink-700"
                        >
                            <span className="w-2 h-2 rounded-full" style={{ backgroundColor: label.color }} />
                            {label.name}
                        </span>
                    ))
                )}
            </div>

            <form onSubmit={submit}>
                <Alert>{error}</Alert>
                <div className="flex gap-3 items-end">
                    <Field label="New label" className="mb-0 flex-1">
                        <Input
                            value={name}
                            onChange={(event) => setName(event.target.value)}
                            placeholder="e.g. Priority"
                        />
                    </Field>
                    <Field label="Colour" className="mb-0">
                        <input
                            type="color"
                            value={color}
                            onChange={(event) => setColor(event.target.value)}
                            aria-label="Label colour"
                            className="h-11 w-14 rounded-lg border border-ink-200 bg-surface p-1 cursor-pointer"
                        />
                    </Field>
                    <Button type="submit" loading={saving} disabled={!name.trim()}>
                        Add
                    </Button>
                </div>
            </form>
        </Modal>
    );
}
