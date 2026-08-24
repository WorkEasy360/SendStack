import { useEffect, useState } from "react";
import { BarChart3, CalendarClock, MessageSquareQuote, Pencil, Plus, Route, Trash2, Users } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useAuth } from "../../context/AuthContext";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import Tabs from "../../components/ui/Tabs";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * Who is on shift, how work reaches them, and what they got through.
 *
 * The agents tab is the one an agent opens (to go on or off shift); the rest
 * is manager territory. They live together because "the team" is one idea,
 * and splitting availability away from the routing rules that consume it
 * makes neither easier to reason about.
 */

const TABS = [
    { id: "agents", label: "Agents", icon: Users },
    { id: "routing", label: "Routing", icon: Route },
    { id: "hours", label: "Working hours", icon: CalendarClock },
    { id: "replies", label: "Canned replies", icon: MessageSquareQuote },
    { id: "performance", label: "Performance", icon: BarChart3 },
];

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Seconds -> the shortest honest phrase. */
function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
}

export default function Team() {
    const [tab, setTab] = useState("agents");

    return (
        <div>
            <PageHeader
                title="Team"
                description="Who is on shift, how conversations reach them, and how the queue is being answered."
            />

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "agents" && <AgentsTab />}
            {tab === "routing" && <RoutingTab />}
            {tab === "hours" && <WorkingHoursTab onGoToRouting={() => setTab("routing")} />}
            {tab === "replies" && <CannedRepliesTab />}
            {tab === "performance" && <PerformanceTab />}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

function AgentsTab() {
    const toast = useToast();
    const { user } = useAuth();
    const [members, setMembers] = useState(null);
    const [error, setError] = useState("");
    const [savingId, setSavingId] = useState(null);

    const load = async () => {
        try {
            // The list endpoint materialises a profile for any user who lacks
            // one, so this is also what makes a newly invited colleague appear.
            const response = await api.get("/teams/members/");
            setMembers(response.data.results || response.data || []);
            setError("");
        } catch (err) {
            setMembers([]);
            setError(apiErrorMessage(err, "Unable to load the team."));
        }
    };

    useEffect(() => {
        load();
    }, []);

    const patch = async (member, changes) => {
        setSavingId(member.id);
        try {
            const response = await api.patch(`/teams/members/${member.id}/`, changes);
            setMembers((current) =>
                current.map((row) => (row.id === member.id ? { ...row, ...response.data } : row))
            );
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this agent."));
        } finally {
            setSavingId(null);
        }
    };

    if (members === null) return <Spinner />;

    const me = members.find((member) => member.user === user?.id);

    return (
        <div>
            <Alert>{error}</Alert>

            {me && (
                <Card className="mb-6">
                    <CardHeader
                        title="Your shift"
                        description="Turning this off stops the queue routing new conversations to you. It does not touch what you can already see."
                        className="mb-4"
                    />
                    <Toggle
                        checked={me.is_available}
                        onChange={(event) => {
                            patch(me, { is_available: event.target.checked });
                            toast.success(event.target.checked ? "You are on shift." : "You are off shift.");
                        }}
                        label={me.is_available ? "On shift" : "Off shift"}
                        description={`You are holding ${me.open_conversations} open conversation${
                            me.open_conversations === 1 ? "" : "s"
                        }.`}
                    />
                </Card>
            )}

            <div className="space-y-3">
                {members.map((member) => (
                    <Card key={member.id}>
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                            <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="text-sm font-bold text-ink-900">{member.username}</span>
                                    {member.user === user?.id && <Badge tone="blue">You</Badge>}
                                    <Badge tone={member.role === "MANAGER" ? "violet" : "gray"}>
                                        {member.role === "MANAGER" ? "Manager" : "Agent"}
                                    </Badge>
                                    {member.is_available ? (
                                        <Badge tone="green">On shift</Badge>
                                    ) : (
                                        <Badge tone="amber">Away</Badge>
                                    )}
                                </div>
                                <p className="text-xs text-ink-500 mt-0.5 mb-0 truncate">{member.email || "—"}</p>
                            </div>

                            <div className="text-xs text-ink-600 tabular-nums shrink-0">
                                <strong className="text-ink-900">{member.open_conversations}</strong> open
                                {member.max_open_conversations > 0 && ` of ${member.max_open_conversations}`}
                            </div>

                            <div className="flex flex-wrap items-center gap-3 shrink-0">
                                <Select
                                    value={member.role}
                                    disabled={savingId === member.id}
                                    onChange={(event) => patch(member, { role: event.target.value })}
                                    className="w-auto"
                                    aria-label={`Role for ${member.username}`}
                                >
                                    <option value="AGENT">Agent</option>
                                    <option value="MANAGER">Manager</option>
                                </Select>

                                <label className="flex items-center gap-1.5 text-xs text-ink-600 whitespace-nowrap">
                                    Cap
                                    <Input
                                        type="number"
                                        min="0"
                                        value={member.max_open_conversations}
                                        disabled={savingId === member.id}
                                        onChange={(event) =>
                                            setMembers((current) =>
                                                current.map((row) =>
                                                    row.id === member.id
                                                        ? { ...row, max_open_conversations: Number(event.target.value) }
                                                        : row
                                                )
                                            )
                                        }
                                        onBlur={(event) =>
                                            patch(member, { max_open_conversations: Number(event.target.value) })
                                        }
                                        className="w-20"
                                        aria-label={`Capacity for ${member.username}`}
                                    />
                                </label>

                                <Toggle
                                    checked={member.auto_assign}
                                    disabled={savingId === member.id}
                                    onChange={(event) => patch(member, { auto_assign: event.target.checked })}
                                    label="Auto-assign"
                                />
                            </div>
                        </div>
                    </Card>
                ))}
            </div>

            <p className="text-xs text-ink-400 mt-4 mb-0">
                A capacity of 0 means no ceiling. Turning off auto-assign keeps someone out of the rotation without
                marking them away — useful for a manager who takes chats only by hand.
            </p>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function RoutingTab() {
    const toast = useToast();
    const [policy, setPolicy] = useState(null);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        api.get("/teams/assignment-policy/")
            .then((response) => setPolicy(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your routing rules.")));
    }, []);

    const update = (patch) => setPolicy((current) => ({ ...current, ...patch }));

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const response = await api.patch("/teams/assignment-policy/", policy);
            setPolicy(response.data);
            toast.success("Routing rules saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these rules."));
        } finally {
            setSaving(false);
        }
    };

    if (!policy) return error ? <Alert>{error}</Alert> : <Spinner />;

    return (
        <div className="max-w-3xl">
            <Alert>{error}</Alert>

            <Card className="mb-6">
                <CardHeader
                    title="How conversations are handed out"
                    description="Applied when a contact writes in. An agent can always take or pass a chat by hand."
                    className="mb-4"
                />

                <Field label="Assignment">
                    <Select value={policy.mode} onChange={(event) => update({ mode: event.target.value })}>
                        <option value="OFF">Leave unassigned — anyone can pick it up</option>
                        <option value="ROUND_ROBIN">Round robin — take it in turns</option>
                        <option value="LEAST_BUSY">Least busy — whoever holds the fewest open chats</option>
                    </Select>
                </Field>

                {policy.mode !== "OFF" && (
                    <>
                        <div className="mb-4">
                            <Toggle
                                checked={policy.reassign_when_unassigned}
                                onChange={(event) => update({ reassign_when_unassigned: event.target.checked })}
                                label="Assign an ownerless conversation when the contact writes again"
                                description="A conversation someone already owns is never taken away from them."
                            />
                        </div>
                        <div className="mb-4">
                            <Toggle
                                checked={policy.respect_availability}
                                onChange={(event) => update({ respect_availability: event.target.checked })}
                                label="Only route to agents who are on shift"
                                description="Off, work is handed out regardless of who is at their desk."
                            />
                        </div>
                    </>
                )}
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Response targets"
                    description="Measured from the contact's message to the first reply typed by a person — an auto-reply is not somebody answering."
                    className="mb-4"
                />
                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="First response (minutes)" hint="0 turns the target off.">
                        <Input
                            type="number"
                            min="0"
                            value={policy.first_response_target_minutes}
                            onChange={(event) =>
                                update({ first_response_target_minutes: Number(event.target.value) })
                            }
                        />
                    </Field>
                    <Field label="Resolution (minutes)" hint="0 turns the target off.">
                        <Input
                            type="number"
                            min="0"
                            value={policy.resolution_target_minutes}
                            onChange={(event) => update({ resolution_target_minutes: Number(event.target.value) })}
                        />
                    </Field>
                </div>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Outside working hours"
                    description="Set the hours themselves on the Working hours tab."
                    className="mb-4"
                />
                <div className="mb-4">
                    <Toggle
                        checked={policy.working_hours_enabled}
                        onChange={(event) => update({ working_hours_enabled: event.target.checked })}
                        label="Use working hours"
                        description="Off, the team counts as open around the clock."
                    />
                </div>
                <div className="mb-4">
                    <Toggle
                        checked={policy.send_away_message}
                        onChange={(event) => update({ send_away_message: event.target.checked })}
                        label="Reply automatically when closed"
                        description="Needs working hours turned on — otherwise the team is never closed and this would never fire."
                    />
                </div>
                {policy.send_away_message && (
                    <Field label="Away message" required>
                        <Textarea
                            rows={3}
                            value={policy.away_message}
                            onChange={(event) => update({ away_message: event.target.value })}
                        />
                    </Field>
                )}
            </Card>

            <div className="flex justify-end">
                <Button onClick={save} loading={saving}>
                    Save routing rules
                </Button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Working hours
// ---------------------------------------------------------------------------

function WorkingHoursTab({ onGoToRouting }) {
    const toast = useToast();
    const [days, setDays] = useState(null);
    const [openNow, setOpenNow] = useState(null);
    // Whether these hours are consulted at all. Without it the page would show
    // seven closed days beside a green "Open now" and look broken -- the
    // server is right (hours it never reads cannot close anybody), but the
    // reason has to be on screen.
    const [hoursEnabled, setHoursEnabled] = useState(null);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        api.get("/teams/working-hours/")
            .then((response) => {
                setDays(response.data.days || []);
                setOpenNow(response.data.open_now);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your working hours.")));
        api.get("/teams/assignment-policy/")
            .then((response) => setHoursEnabled(response.data.working_hours_enabled))
            .catch(() => setHoursEnabled(null));
    }, []);

    const updateDay = (weekday, changes) =>
        setDays((current) => current.map((day) => (day.weekday === weekday ? { ...day, ...changes } : day)));

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            // The week is written whole, so an open day that lost its hours
            // cannot be saved as a half-row the reader has to guess at.
            const response = await api.put("/teams/working-hours/", {
                days: days.map(({ weekday, is_open, opens_at, closes_at }) => ({
                    weekday,
                    is_open,
                    opens_at,
                    closes_at,
                })),
            });
            setDays(response.data.days || []);
            setOpenNow(response.data.open_now);
            toast.success("Working hours saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these hours."));
        } finally {
            setSaving(false);
        }
    };

    if (days === null) return error ? <Alert>{error}</Alert> : <Spinner />;

    return (
        <div className="max-w-3xl">
            <Alert>{error}</Alert>

            <Card className="mb-6">
                <CardHeader
                    title="When the team is open"
                    description="A day left closed means nobody is expected — the away reply, if you turned it on, answers instead."
                    action={
                        hoursEnabled === false ? (
                            <Badge tone="gray">Not in use</Badge>
                        ) : openNow === null ? null : openNow ? (
                            <Badge tone="green">Open now</Badge>
                        ) : (
                            <Badge tone="gray">Closed now</Badge>
                        )
                    }
                    className="mb-4"
                />

                {hoursEnabled === false && (
                    <Alert tone="info" title="These hours are not being applied">
                        Working hours are switched off, so the team counts as open around the clock no matter what is
                        set below. Turn them on under{" "}
                        <button
                            type="button"
                            onClick={onGoToRouting}
                            className="cursor-pointer border-none bg-transparent p-0 font-semibold text-brand-700 underline"
                        >
                            Routing
                        </button>
                        .
                    </Alert>
                )}

                <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                    {days.map((day) => (
                        <li key={day.weekday} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                            <span className="w-24 shrink-0 text-sm font-semibold text-ink-800">
                                {WEEKDAYS[day.weekday]}
                            </span>
                            <Toggle
                                checked={day.is_open}
                                onChange={(event) => updateDay(day.weekday, { is_open: event.target.checked })}
                                label={day.is_open ? "Open" : "Closed"}
                            />
                            {day.is_open && (
                                <div className="flex items-center gap-2 ml-auto">
                                    <Input
                                        type="time"
                                        value={(day.opens_at || "09:00:00").slice(0, 5)}
                                        onChange={(event) =>
                                            updateDay(day.weekday, { opens_at: `${event.target.value}:00` })
                                        }
                                        className="w-32"
                                        aria-label={`${WEEKDAYS[day.weekday]} opens at`}
                                    />
                                    <span className="text-xs text-ink-400">to</span>
                                    <Input
                                        type="time"
                                        value={(day.closes_at || "18:00:00").slice(0, 5)}
                                        onChange={(event) =>
                                            updateDay(day.weekday, { closes_at: `${event.target.value}:00` })
                                        }
                                        className="w-32"
                                        aria-label={`${WEEKDAYS[day.weekday]} closes at`}
                                    />
                                </div>
                            )}
                        </li>
                    ))}
                </ul>
            </Card>

            <div className="flex justify-end">
                <Button onClick={save} loading={saving}>
                    Save working hours
                </Button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Canned replies
// ---------------------------------------------------------------------------

function CannedRepliesTab() {
    const toast = useToast();
    const [replies, setReplies] = useState(null);
    const [error, setError] = useState("");
    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);

    const load = async () => {
        try {
            const response = await api.get("/teams/canned-replies/");
            setReplies(response.data.results || []);
            setError("");
        } catch (err) {
            setReplies([]);
            setError(apiErrorMessage(err, "Unable to load your canned replies."));
        }
    };

    useEffect(() => {
        load();
    }, []);

    const submit = async (form) => {
        const payload = { shortcut: form.shortcut, title: form.title, body: form.body };
        if (form.id) await api.patch(`/teams/canned-replies/${form.id}/`, payload);
        else await api.post("/teams/canned-replies/", payload);
        await load();
        toast.success(form.id ? "Reply updated." : "Reply saved.");
        setEditing(null);
    };

    const confirmDelete = async () => {
        try {
            await api.delete(`/teams/canned-replies/${pendingDelete.id}/`);
            await load();
            toast.success("Reply deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this reply."));
        } finally {
            setPendingDelete(null);
        }
    };

    if (replies === null) return <Spinner />;

    return (
        <div>
            <Alert tone="info" title="Typed as /shortcut in the inbox">
                In the reply box, type <code>/</code> followed by the shortcut and pick from the list. The saved text
                replaces what you typed, ready to edit before you send it.
            </Alert>

            <Alert>{error}</Alert>

            <div className="flex justify-end mb-4">
                <Button icon={Plus} onClick={() => setEditing({ shortcut: "", title: "", body: "" })}>
                    New reply
                </Button>
            </div>

            {replies.length === 0 ? (
                <EmptyState
                    icon={MessageSquareQuote}
                    title="No canned replies yet"
                    description="Save the answers your team types over and over — opening hours, delivery times, how to return something."
                    action={
                        <Button icon={Plus} onClick={() => setEditing({ shortcut: "", title: "", body: "" })}>
                            New reply
                        </Button>
                    }
                />
            ) : (
                <div className="space-y-3">
                    {replies.map((reply) => (
                        <Card key={reply.id}>
                            <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <code className="text-sm font-bold text-brand-700">/{reply.shortcut}</code>
                                        <span className="text-sm font-semibold text-ink-900">{reply.title}</span>
                                        <span className="text-xs text-ink-400 tabular-nums">
                                            used {reply.usage_count}×
                                        </span>
                                    </div>
                                    <p className="text-sm text-ink-500 mt-1 mb-0 whitespace-pre-wrap line-clamp-3">
                                        {reply.body}
                                    </p>
                                </div>
                                <div className="flex gap-1 shrink-0">
                                    <Button size="xs" variant="ghost" icon={Pencil} onClick={() => setEditing(reply)}>
                                        Edit
                                    </Button>
                                    <Button
                                        size="xs"
                                        variant="ghost"
                                        icon={Trash2}
                                        onClick={() => setPendingDelete(reply)}
                                    >
                                        Delete
                                    </Button>
                                </div>
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            {editing && <ReplyModal initial={editing} onSubmit={submit} onClose={() => setEditing(null)} />}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete this reply"
                    message={`Delete “/${pendingDelete.shortcut}”? Anyone typing it will simply get nothing.`}
                    confirmLabel="Delete"
                    onConfirm={confirmDelete}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

function ReplyModal({ initial, onSubmit, onClose }) {
    const [form, setForm] = useState(initial);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            await onSubmit(form);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save this reply."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title={form.id ? "Edit reply" : "New reply"} onClose={onClose} width="lg">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Shortcut" required hint="Typed as /shortcut. No spaces.">
                    <Input
                        value={form.shortcut}
                        onChange={(event) => setForm({ ...form, shortcut: event.target.value })}
                        placeholder="hours"
                        autoFocus
                        required
                    />
                </Field>

                <Field label="Title" required hint="How it is listed when picking one.">
                    <Input
                        value={form.title}
                        onChange={(event) => setForm({ ...form, title: event.target.value })}
                        placeholder="Opening hours"
                        required
                    />
                </Field>

                <Field label="Message" required>
                    <Textarea
                        rows={6}
                        value={form.body}
                        onChange={(event) => setForm({ ...form, body: event.target.value })}
                        required
                    />
                </Field>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Save reply"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Performance
// ---------------------------------------------------------------------------

function PerformanceTab() {
    const [agents, setAgents] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/teams/performance/")
            .then((response) => setAgents(response.data.agents || []))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load performance.")));
    }, []);

    if (error) return <Alert>{error}</Alert>;
    if (agents === null) return <Spinner />;

    const columns = [
        {
            key: "username",
            header: "Agent",
            render: (row) => (
                <div className="flex items-center gap-2">
                    <span className="font-semibold text-ink-900">{row.username}</span>
                    {row.role === "MANAGER" && <Badge tone="violet">Manager</Badge>}
                    {!row.is_available && <Badge tone="amber">Away</Badge>}
                </div>
            ),
        },
        { key: "assigned", header: "Assigned", render: (row) => <span className="tabular-nums">{row.assigned}</span> },
        { key: "open", header: "Open", render: (row) => <span className="tabular-nums">{row.open}</span> },
        { key: "closed", header: "Closed", render: (row) => <span className="tabular-nums">{row.closed}</span> },
        {
            key: "replies_sent",
            header: "Replies",
            render: (row) => <span className="tabular-nums">{row.replies_sent}</span>,
        },
        {
            key: "avg_first_response_seconds",
            header: "Avg first response",
            render: (row) => (
                <span className="tabular-nums">{formatDuration(row.avg_first_response_seconds)}</span>
            ),
        },
    ];

    return (
        <div>
            <p className="text-sm text-ink-500 mt-0 mb-4">
                Counted from the conversations themselves rather than a stored tally, so these always agree with the
                inbox. Replies exclude anything sent by automation.
            </p>

            <DataTable
                columns={columns}
                rows={agents}
                rowKey={(row) => row.user_id}
                empty={
                    <EmptyState
                        icon={BarChart3}
                        title="Nothing to measure yet"
                        description="Once conversations are assigned and answered, per-agent figures appear here."
                    />
                }
            />
        </div>
    );
}
