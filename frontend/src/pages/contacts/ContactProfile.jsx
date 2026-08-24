import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
    Activity as ActivityIcon,
    BrainCircuit,
    Briefcase,
    Building2,
    CheckCircle2,
    Clock,
    Handshake,
    Mail,
    MessageSquare,
    Merge,
    NotebookPen,
    Phone,
    Plus,
    Send,
    Tag as TagIcon,
    User,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import Tabs from "../../components/ui/Tabs";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";
import Skeleton from "../../components/ui/Skeleton";
import { useToast } from "../../components/ui/Toast";
import {
    DealStatusBadge,
    LIFECYCLE_OPTIONS,
    LifecycleBadge,
    ScoreBadge,
    formatDate,
    formatDateTime,
    formatMoney,
    timeAgo,
    toLocalInput,
} from "../../components/crm/CrmBits";

/**
 * One customer, one screen.
 *
 * Everything a person needs to know about a contact and every action they can
 * take on them lives here: who they are, what they are worth, what was said,
 * what is owed, what happens next. The alternative -- a contacts page, a
 * separate deals page, a separate tasks page, and the inbox -- is the same
 * information behind four navigations, which is how a CRM stops being opened.
 *
 * The whole page is one request (`/contacts/{id}/profile/`), so it renders
 * complete or not at all rather than filling in over several seconds.
 */

const TIMELINE_ICON = {
    MESSAGE_SENT: Send,
    MESSAGE_RECEIVED: MessageSquare,
    CONTACT_CREATED: User,
    STATUS_CHANGED: ActivityIcon,
    OWNER_CHANGED: User,
    TAG_ADDED: TagIcon,
    TAG_REMOVED: TagIcon,
    NOTE_ADDED: NotebookPen,
    TASK_CREATED: Clock,
    TASK_COMPLETED: CheckCircle2,
    DEAL_CREATED: Handshake,
    DEAL_STAGE_CHANGED: Briefcase,
    DEAL_WON: Handshake,
    DEAL_LOST: Briefcase,
    CONSENT_CHANGED: ActivityIcon,
    AUTOMATION: ActivityIcon,
};

const TIMELINE_TONE = {
    MESSAGE_SENT: "text-brand-600 bg-brand-50",
    MESSAGE_RECEIVED: "text-blue-600 bg-blue-50",
    DEAL_WON: "text-brand-700 bg-brand-50",
    DEAL_LOST: "text-red-600 bg-red-50",
    CONSENT_CHANGED: "text-amber-700 bg-amber-50",
    TASK_COMPLETED: "text-brand-600 bg-brand-50",
};

function TimelineRow({ entry }) {
    const Icon = TIMELINE_ICON[entry.kind] || ActivityIcon;
    const tone = TIMELINE_TONE[entry.kind] || "text-ink-500 bg-ink-100";
    const meta = entry.metadata || {};

    return (
        <li className="flex gap-3">
            <div className="flex flex-col items-center">
                <span className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${tone}`}>
                    <Icon size={15} aria-hidden="true" />
                </span>
                <span className="flex-1 w-px bg-ink-200 my-1" />
            </div>
            <div className="min-w-0 flex-1 pb-5">
                <div className="flex items-baseline justify-between gap-3">
                    <p className="text-sm text-ink-800 m-0 break-words">{entry.summary}</p>
                    <span className="text-xs text-ink-400 shrink-0 whitespace-nowrap">{timeAgo(entry.at)}</span>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 mt-1">
                    {entry.actor && <span className="text-xs text-ink-500">by {entry.actor}</span>}
                    {meta.campaign && <Badge tone="violet">{meta.campaign}</Badge>}
                    {meta.template && <Badge tone="blue">{meta.template}</Badge>}
                    {meta.automated && <Badge tone="gray">Automated</Badge>}
                    {entry.source === "message" && meta.status && meta.status !== "SENT" && (
                        <Badge tone={meta.status === "FAILED" ? "red" : "gray"}>{meta.status}</Badge>
                    )}
                </div>
            </div>
        </li>
    );
}

function Detail({ icon: Icon, label, children }) {
    return (
        <div className="flex items-start gap-2.5 py-2">
            <Icon size={15} className="text-ink-400 mt-0.5 shrink-0" aria-hidden="true" />
            <div className="min-w-0">
                <p className="text-[11px] font-bold uppercase tracking-wider text-ink-400 m-0">{label}</p>
                <div className="text-sm text-ink-800 mt-0.5 break-words">{children}</div>
            </div>
        </div>
    );
}

export default function ContactProfile() {
    const { id } = useParams();
    const toast = useToast();

    const [profile, setProfile] = useState(null);
    const [timeline, setTimeline] = useState([]);
    const [timelineCursor, setTimelineCursor] = useState(null);
    const [team, setTeam] = useState([]);
    const [stages, setStages] = useState([]);
    const [tags, setTags] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [tab, setTab] = useState("timeline");
    // Fetched only when the tab is opened. It is a debugging view, not
    // something worth a request on every profile load.
    const [aiContext, setAiContext] = useState(null);
    const [aiContextError, setAiContextError] = useState("");
    const [busy, setBusy] = useState(false);

    const [noteBody, setNoteBody] = useState("");
    const [dealForm, setDealForm] = useState(null);
    const [taskForm, setTaskForm] = useState(null);
    const [mergeOpen, setMergeOpen] = useState(false);
    const [duplicates, setDuplicates] = useState([]);
    const [pendingMerge, setPendingMerge] = useState(null);

    const contact = profile?.contact;

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [profileRes, timelineRes] = await Promise.all([
                api.get(`/contacts/contacts/${id}/profile/`),
                api.get(`/crm/contacts/${id}/timeline/`),
            ]);
            setProfile(profileRes.data);
            setTimeline(timelineRes.data.results || []);
            setTimelineCursor(timelineRes.data.next_before || null);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load this contact."));
        } finally {
            setLoading(false);
        }
    }, [id]);

    useEffect(() => {
        load();
    }, [load]);

    // The pickers. Loaded once and separately from the profile: they are the
    // same for every contact, and a failure here should cost a dropdown, not
    // the page.
    useEffect(() => {
        Promise.all([
            api.get("/crm/team/").catch(() => ({ data: { results: [] } })),
            api.get("/crm/stages/", { params: { page_size: 100 } }).catch(() => ({ data: { results: [] } })),
            api.get("/contacts/tags/").catch(() => ({ data: { results: [] } })),
        ]).then(([teamRes, stagesRes, tagsRes]) => {
            setTeam(teamRes.data.results || []);
            setStages(stagesRes.data.results || []);
            setTags(tagsRes.data.results || []);
        });
    }, []);

    const tagNames = useMemo(() => {
        const byId = new Map(tags.map((tag) => [tag.id, tag]));
        return (contact?.tags || []).map((tagId) => byId.get(tagId)).filter(Boolean);
    }, [contact, tags]);

    const patchContact = async (payload, successMessage) => {
        setBusy(true);
        try {
            await api.patch(`/contacts/contacts/${id}/`, payload);
            toast.success(successMessage);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that."));
        } finally {
            setBusy(false);
        }
    };

    const loadMoreTimeline = async () => {
        if (!timelineCursor) return;
        try {
            const response = await api.get(`/crm/contacts/${id}/timeline/`, {
                params: { before: timelineCursor },
            });
            setTimeline((current) => [...current, ...(response.data.results || [])]);
            setTimelineCursor(response.data.next_before || null);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not load more history."));
        }
    };

    const addNote = async (event) => {
        event.preventDefault();
        if (!noteBody.trim()) return;
        setBusy(true);
        try {
            await api.post("/crm/notes/", { contact: Number(id), body: noteBody.trim() });
            setNoteBody("");
            toast.success("Note added.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not add that note."));
        } finally {
            setBusy(false);
        }
    };

    const saveDeal = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            const payload = {
                contact: Number(id),
                name: dealForm.name,
                value: dealForm.value || 0,
                currency: dealForm.currency || "INR",
                expected_close_date: dealForm.expected_close_date || null,
                notes: dealForm.notes || "",
            };
            if (dealForm.stage) payload.stage = Number(dealForm.stage);
            if (dealForm.id) {
                await api.patch(`/crm/deals/${dealForm.id}/`, payload);
            } else {
                await api.post("/crm/deals/", payload);
            }
            setDealForm(null);
            toast.success(dealForm.id ? "Deal updated." : "Deal created.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that deal."));
        } finally {
            setBusy(false);
        }
    };

    const moveDeal = async (deal, stageId) => {
        try {
            await api.post(`/crm/deals/${deal.id}/move/`, { stage: Number(stageId) });
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not move that deal."));
        }
    };

    const saveTask = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            await api.post("/crm/tasks/", {
                contact: Number(id),
                title: taskForm.title,
                notes: taskForm.notes || "",
                kind: taskForm.kind || "FOLLOW_UP",
                due_at: new Date(taskForm.due_at).toISOString(),
                assigned_to: taskForm.assigned_to ? Number(taskForm.assigned_to) : null,
            });
            setTaskForm(null);
            toast.success("Task added.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that task."));
        } finally {
            setBusy(false);
        }
    };

    const completeTask = async (task) => {
        try {
            await api.post(`/crm/tasks/${task.id}/complete/`);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not complete that task."));
        }
    };

    const openMerge = async () => {
        setMergeOpen(true);
        try {
            const response = await api.get(`/contacts/contacts/${id}/duplicates/`);
            setDuplicates(response.data.results || []);
        } catch {
            setDuplicates([]);
        }
    };

    const runMerge = async () => {
        setBusy(true);
        try {
            await api.post(`/contacts/contacts/${id}/merge/`, { duplicate_id: pendingMerge.id });
            toast.success("Contacts merged.");
            setPendingMerge(null);
            setMergeOpen(false);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not merge those contacts."));
        } finally {
            setBusy(false);
        }
    };

    if (loading) {
        return (
            <div className="py-20 flex justify-center">
                <Spinner />
            </div>
        );
    }
    if (error || !contact) {
        return (
            <div>
                <PageHeader title="Contact" backTo="/contacts" backLabel="Back to contacts" />
                <Alert>{error || "That contact no longer exists."}</Alert>
            </div>
        );
    }

    const openDeals = (profile.deals || []).filter((deal) => deal.status === "OPEN");
    const openTasks = (profile.tasks || []).filter((task) => task.status === "OPEN");
    const conversation = (profile.conversations || [])[0];

    // The AI-context tab is a read-only window onto what the agents are given.
    // Fetched on open rather than on page load: nobody opens a customer to
    // debug a prompt, so it should cost nothing until they ask.
    useEffect(() => {
        if (tab !== "ai" || aiContext) return;
        let cancelled = false;
        api.get(`/crm/contacts/${id}/context/`)
            .then(({ data }) => {
                if (!cancelled) setAiContext(data);
            })
            .catch((err) => {
                if (!cancelled) setAiContextError(apiErrorMessage(err, "Could not load what the AI knows."));
            });
        return () => {
            cancelled = true;
        };
    }, [tab, aiContext, id]);

    const tabs = [
        { id: "timeline", label: "Timeline", icon: ActivityIcon },
        { id: "deals", label: "Deals", icon: Handshake, count: openDeals.length || undefined },
        { id: "tasks", label: "Tasks", icon: Clock, count: openTasks.length || undefined },
        { id: "notes", label: "Notes", icon: NotebookPen, count: profile.notes?.length || undefined },
        { id: "ai", label: "What the AI knows", icon: BrainCircuit },
    ];

    return (
        <div>
            <PageHeader
                title={contact.name || contact.phone}
                description={[contact.company, contact.phone].filter(Boolean).join(" · ")}
                backTo="/contacts"
                backLabel="Back to contacts"
                actions={
                    <>
                        <Button variant="secondary" icon={Merge} onClick={openMerge}>
                            Find duplicates
                        </Button>
                        <Button
                            variant="secondary"
                            icon={Handshake}
                            onClick={() =>
                                setDealForm({ name: "", value: "", currency: "INR", expected_close_date: "", notes: "" })
                            }
                        >
                            New deal
                        </Button>
                        {conversation ? (
                            <Link to={`/inbox?conversation=${conversation.id}`} className="no-underline">
                                <Button icon={MessageSquare}>Open chat</Button>
                            </Link>
                        ) : (
                            <Link to="/campaigns/new" state={{ contactIds: [contact.id] }} className="no-underline">
                                <Button icon={Send}>Send a message</Button>
                            </Link>
                        )}
                    </>
                }
            />

            <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6 items-start">
                {/* --- Who they are, and everything you can change about them --- */}
                <div className="space-y-6">
                    <Card>
                        <div className="flex items-center gap-2 flex-wrap mb-4">
                            <ScoreBadge band={contact.score_band} score={contact.lead_score} showScore />
                            <LifecycleBadge status={contact.lifecycle_status} />
                            {!contact.subscribed && <Badge tone="red">Unsubscribed</Badge>}
                        </div>

                        <div className="divide-y divide-ink-100">
                            <Detail icon={Phone} label="Phone">
                                <span className="tabular-nums">{contact.phone}</span>
                            </Detail>
                            {contact.email && (
                                <Detail icon={Mail} label="Email">
                                    {contact.email}
                                </Detail>
                            )}
                            {contact.company && (
                                <Detail icon={Building2} label="Company">
                                    {contact.company}
                                </Detail>
                            )}
                            <Detail icon={MessageSquare} label="Last reply from them">
                                {timeAgo(contact.last_inbound_at)}
                            </Detail>
                            <Detail icon={Send} label="Last message from us">
                                {timeAgo(contact.last_outbound_at)}
                            </Detail>
                            {profile.next_action && (
                                <Detail icon={Clock} label="Next action">
                                    {profile.next_action}
                                    <span className="text-ink-400"> · {timeAgo(profile.next_action_at)}</span>
                                </Detail>
                            )}
                        </div>

                        {tagNames.length > 0 && (
                            <div className="flex flex-wrap gap-1.5 mt-4 pt-4 border-t border-ink-100">
                                {tagNames.map((tag) => (
                                    <Badge key={tag.id} tone="blue">
                                        {tag.name}
                                    </Badge>
                                ))}
                            </div>
                        )}
                    </Card>

                    <Card>
                        <CardHeader title="CRM" description="Changes here are saved immediately." className="mb-4" />
                        <div className="space-y-4">
                            <Field label="Status">
                                <Select
                                    value={contact.lifecycle_status}
                                    disabled={busy}
                                    onChange={(event) =>
                                        patchContact({ lifecycle_status: event.target.value }, "Status updated.")
                                    }
                                >
                                    {LIFECYCLE_OPTIONS.map((option) => (
                                        <option key={option.value} value={option.value}>
                                            {option.label}
                                        </option>
                                    ))}
                                </Select>
                            </Field>
                            <Field label="Owner" hint="Who is responsible for following this person up.">
                                <Select
                                    value={contact.owner || ""}
                                    disabled={busy}
                                    onChange={(event) =>
                                        patchContact(
                                            { owner: event.target.value ? Number(event.target.value) : null },
                                            "Owner updated.",
                                        )
                                    }
                                >
                                    <option value="">Nobody yet</option>
                                    {team.map((member) => (
                                        <option key={member.id} value={member.id}>
                                            {member.username}
                                        </option>
                                    ))}
                                </Select>
                            </Field>
                            {(openDeals.length > 0 || profile.won_deal_value > 0) && (
                                <div className="pt-2 border-t border-ink-100 grid grid-cols-2 gap-3">
                                    <div>
                                        <p className="text-[11px] font-bold uppercase tracking-wider text-ink-400 m-0">
                                            In play
                                        </p>
                                        <p className="text-sm font-bold text-ink-900 mt-0.5 mb-0 tabular-nums">
                                            {formatMoney(profile.open_deal_value, openDeals[0]?.currency)}
                                        </p>
                                    </div>
                                    <div>
                                        <p className="text-[11px] font-bold uppercase tracking-wider text-ink-400 m-0">
                                            Won
                                        </p>
                                        <p className="text-sm font-bold text-brand-700 mt-0.5 mb-0 tabular-nums">
                                            {formatMoney(profile.won_deal_value, openDeals[0]?.currency)}
                                        </p>
                                    </div>
                                </div>
                            )}
                        </div>
                    </Card>
                </div>

                {/* --- What has happened, and what happens next --- */}
                <div>
                    <Tabs tabs={tabs} active={tab} onChange={setTab} />

                    {tab === "timeline" && (
                        <Card>
                            {timeline.length === 0 ? (
                                <EmptyState
                                    variant="plain"
                                    icon={ActivityIcon}
                                    title="Nothing has happened yet"
                                    description="Messages, notes, tasks and deals will all appear here as they happen."
                                />
                            ) : (
                                <>
                                    <ul className="list-none p-0 m-0">
                                        {timeline.map((entry) => (
                                            <TimelineRow key={entry.id} entry={entry} />
                                        ))}
                                    </ul>
                                    {timelineCursor && (
                                        <div className="flex justify-center pt-2">
                                            <Button variant="secondary" size="sm" onClick={loadMoreTimeline}>
                                                Show older
                                            </Button>
                                        </div>
                                    )}
                                </>
                            )}
                        </Card>
                    )}

                    {tab === "deals" && (
                        <Card>
                            <CardHeader
                                title="Deals"
                                description="What this customer is worth, and where each sale has got to."
                                action={
                                    <Button
                                        size="sm"
                                        icon={Plus}
                                        onClick={() =>
                                            setDealForm({
                                                name: "", value: "", currency: "INR",
                                                expected_close_date: "", notes: "",
                                            })
                                        }
                                    >
                                        New deal
                                    </Button>
                                }
                                className="mb-4"
                            />
                            {profile.deals?.length ? (
                                <ul className="list-none p-0 m-0 space-y-3">
                                    {profile.deals.map((deal) => (
                                        <li
                                            key={deal.id}
                                            className="border border-ink-200 rounded-card p-4 flex flex-col sm:flex-row sm:items-center gap-3"
                                        >
                                            <div className="min-w-0 flex-1">
                                                <div className="flex items-center gap-2 flex-wrap">
                                                    <button
                                                        type="button"
                                                        onClick={() =>
                                                            setDealForm({
                                                                id: deal.id,
                                                                name: deal.name,
                                                                value: deal.value,
                                                                currency: deal.currency,
                                                                stage: deal.stage,
                                                                expected_close_date: deal.expected_close_date || "",
                                                                notes: deal.notes || "",
                                                            })
                                                        }
                                                        className="text-sm font-bold text-ink-900 bg-transparent border-0 p-0 cursor-pointer hover:text-brand-700"
                                                    >
                                                        {deal.name}
                                                    </button>
                                                    <DealStatusBadge status={deal.status} />
                                                </div>
                                                <p className="text-xs text-ink-500 mt-1 mb-0">
                                                    {formatMoney(deal.value, deal.currency)}
                                                    {deal.expected_close_date &&
                                                        ` · closes ${formatDate(deal.expected_close_date)}`}
                                                    {deal.owner_username && ` · ${deal.owner_username}`}
                                                </p>
                                            </div>
                                            <Select
                                                value={deal.stage}
                                                className="sm:w-44"
                                                aria-label={`Move ${deal.name} to another stage`}
                                                onChange={(event) => moveDeal(deal, event.target.value)}
                                            >
                                                {stages
                                                    .filter((stage) => stage.pipeline === deal.pipeline)
                                                    .map((stage) => (
                                                        <option key={stage.id} value={stage.id}>
                                                            {stage.name}
                                                        </option>
                                                    ))}
                                            </Select>
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <EmptyState
                                    variant="plain"
                                    icon={Handshake}
                                    title="No deals yet"
                                    description="Open a deal when this conversation turns into a real sale, so it stops being something you have to remember."
                                />
                            )}
                        </Card>
                    )}

                    {tab === "tasks" && (
                        <Card>
                            <CardHeader
                                title="Tasks"
                                description="Anything you have promised to do, with a time it has to happen by."
                                action={
                                    <Button
                                        size="sm"
                                        icon={Plus}
                                        onClick={() =>
                                            setTaskForm({
                                                title: "", notes: "", kind: "FOLLOW_UP",
                                                due_at: toLocalInput(Date.now() + 86400000), assigned_to: "",
                                            })
                                        }
                                    >
                                        Add task
                                    </Button>
                                }
                                className="mb-4"
                            />
                            {profile.tasks?.length ? (
                                <ul className="list-none p-0 m-0 space-y-2">
                                    {profile.tasks.map((task) => (
                                        <li
                                            key={task.id}
                                            className={`border rounded-card p-3 flex items-center gap-3 ${
                                                task.is_overdue ? "border-red-200 bg-red-50/50" : "border-ink-200"
                                            }`}
                                        >
                                            <button
                                                type="button"
                                                aria-label={`Mark "${task.title}" done`}
                                                disabled={task.status !== "OPEN"}
                                                onClick={() => completeTask(task)}
                                                className={`shrink-0 w-5 h-5 rounded border-2 flex items-center justify-center bg-transparent cursor-pointer ${
                                                    task.status === "DONE"
                                                        ? "border-brand-600 text-brand-600 cursor-default"
                                                        : "border-ink-300 hover:border-brand-500"
                                                }`}
                                            >
                                                {task.status === "DONE" && <CheckCircle2 size={13} />}
                                            </button>
                                            <div className="min-w-0 flex-1">
                                                <p
                                                    className={`text-sm m-0 ${
                                                        task.status === "DONE"
                                                            ? "text-ink-400 line-through"
                                                            : "text-ink-900 font-medium"
                                                    }`}
                                                >
                                                    {task.title}
                                                </p>
                                                <p className="text-xs text-ink-500 mt-0.5 mb-0">
                                                    {formatDateTime(task.due_at)}
                                                    {task.assigned_to_username && ` · ${task.assigned_to_username}`}
                                                </p>
                                            </div>
                                            {task.is_overdue && <Badge tone="red">Overdue</Badge>}
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <EmptyState
                                    variant="plain"
                                    icon={Clock}
                                    title="Nothing scheduled"
                                    description="Add a task so the next step on this customer shows up on your dashboard instead of in your head."
                                />
                            )}
                        </Card>
                    )}

                    {tab === "ai" && (
                        <Card>
                            <CardHeader
                                title="What the AI knows about this customer"
                                description="The exact text both the WhatsApp assistant and the calling agent are given before they answer."
                                className="mb-4"
                            />
                            <Alert>{aiContextError}</Alert>
                            {!aiContext && !aiContextError && <Skeleton className="h-24 w-full" />}
                            {aiContext && (
                                <>
                                    <pre className="text-xs leading-relaxed text-ink-700 bg-ink-50 border border-ink-200 rounded-lg p-3 m-0 whitespace-pre-wrap break-words">
                                        {aiContext.prompt}
                                    </pre>
                                    <p className="text-xs text-ink-500 mt-3 mb-0">
                                        Assembled from this customer's record every time an agent replies. It is
                                        never read back to the customer as a list, and scores, statuses and owners
                                        are never mentioned to them.
                                    </p>
                                </>
                            )}
                        </Card>
                    )}

                    {tab === "notes" && (
                        <Card>
                            <CardHeader
                                title="Notes"
                                description="Private to your team — never sent to the customer."
                                className="mb-4"
                            />
                            <form onSubmit={addNote} className="mb-5">
                                <Textarea
                                    rows={3}
                                    value={noteBody}
                                    onChange={(event) => setNoteBody(event.target.value)}
                                    placeholder="Prefers a call after 6pm. Asked about bulk pricing."
                                />
                                <div className="flex justify-end mt-2">
                                    <Button type="submit" size="sm" icon={NotebookPen} loading={busy} disabled={!noteBody.trim()}>
                                        Add note
                                    </Button>
                                </div>
                            </form>
                            {profile.notes?.length ? (
                                <ul className="list-none p-0 m-0 space-y-3">
                                    {profile.notes.map((note) => (
                                        <li key={note.id} className="border border-ink-200 rounded-card p-3">
                                            <p className="text-sm text-ink-800 m-0 whitespace-pre-wrap break-words">
                                                {note.body}
                                            </p>
                                            <p className="text-xs text-ink-400 mt-2 mb-0">
                                                {note.author_username || "Someone"} · {timeAgo(note.created_at)}
                                            </p>
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <p className="text-sm text-ink-500 m-0">No notes yet.</p>
                            )}
                        </Card>
                    )}
                </div>
            </div>

            {dealForm && (
                <Modal
                    title={dealForm.id ? "Edit deal" : "New deal"}
                    description={`For ${contact.name || contact.phone}.`}
                    onClose={() => setDealForm(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setDealForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="deal-form" loading={busy}>
                                {dealForm.id ? "Save deal" : "Create deal"}
                            </Button>
                        </>
                    }
                >
                    <form id="deal-form" onSubmit={saveDeal} className="space-y-4">
                        <Field label="What is the sale?" required>
                            <Input
                                autoFocus
                                required
                                value={dealForm.name}
                                onChange={(event) => setDealForm({ ...dealForm, name: event.target.value })}
                                placeholder="500 units, monthly"
                            />
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="Value">
                                <Input
                                    type="number"
                                    min="0"
                                    step="0.01"
                                    value={dealForm.value}
                                    onChange={(event) => setDealForm({ ...dealForm, value: event.target.value })}
                                />
                            </Field>
                            <Field label="Currency">
                                <Input
                                    maxLength={3}
                                    value={dealForm.currency}
                                    onChange={(event) =>
                                        setDealForm({ ...dealForm, currency: event.target.value.toUpperCase() })
                                    }
                                />
                            </Field>
                        </div>
                        <Field label="Expected to close" hint="Optional — used for the closing-this-week count.">
                            <Input
                                type="date"
                                value={dealForm.expected_close_date || ""}
                                onChange={(event) =>
                                    setDealForm({ ...dealForm, expected_close_date: event.target.value })
                                }
                            />
                        </Field>
                        <Field label="Notes">
                            <Textarea
                                rows={3}
                                value={dealForm.notes}
                                onChange={(event) => setDealForm({ ...dealForm, notes: event.target.value })}
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {taskForm && (
                <Modal
                    title="Add task"
                    description={`For ${contact.name || contact.phone}.`}
                    onClose={() => setTaskForm(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setTaskForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="task-form" loading={busy}>
                                Add task
                            </Button>
                        </>
                    }
                >
                    <form id="task-form" onSubmit={saveTask} className="space-y-4">
                        <Field label="What needs doing?" required>
                            <Input
                                autoFocus
                                required
                                value={taskForm.title}
                                onChange={(event) => setTaskForm({ ...taskForm, title: event.target.value })}
                                placeholder="Call about the bulk quote"
                            />
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="When" required>
                                <Input
                                    type="datetime-local"
                                    required
                                    value={taskForm.due_at}
                                    onChange={(event) => setTaskForm({ ...taskForm, due_at: event.target.value })}
                                />
                            </Field>
                            <Field label="Kind">
                                <Select
                                    value={taskForm.kind}
                                    onChange={(event) => setTaskForm({ ...taskForm, kind: event.target.value })}
                                >
                                    <option value="FOLLOW_UP">Follow up</option>
                                    <option value="CALL">Call</option>
                                    <option value="MEETING">Meeting</option>
                                    <option value="OTHER">Other</option>
                                </Select>
                            </Field>
                        </div>
                        <Field label="Who">
                            <Select
                                value={taskForm.assigned_to}
                                onChange={(event) => setTaskForm({ ...taskForm, assigned_to: event.target.value })}
                            >
                                <option value="">Me</option>
                                {team.map((member) => (
                                    <option key={member.id} value={member.id}>
                                        {member.username}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field label="Notes">
                            <Textarea
                                rows={2}
                                value={taskForm.notes}
                                onChange={(event) => setTaskForm({ ...taskForm, notes: event.target.value })}
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {mergeOpen && (
                <Modal
                    title="Possible duplicates"
                    description="Records that look like the same person. Merging keeps this contact and folds the other one into it."
                    onClose={() => setMergeOpen(false)}
                    footer={
                        <Button variant="secondary" onClick={() => setMergeOpen(false)}>
                            Close
                        </Button>
                    }
                >
                    {duplicates.length === 0 ? (
                        <p className="text-sm text-ink-500 m-0">
                            Nothing else looks like this person. Phone numbers are already unique here, so exact
                            duplicates cannot exist — this checks for the same number written differently.
                        </p>
                    ) : (
                        <ul className="list-none p-0 m-0 space-y-2">
                            {duplicates.map((duplicate) => (
                                <li
                                    key={duplicate.id}
                                    className="border border-ink-200 rounded-card p-3 flex items-center gap-3"
                                >
                                    <div className="min-w-0 flex-1">
                                        <p className="text-sm font-semibold text-ink-900 m-0 truncate">
                                            {duplicate.name || "No name"}
                                        </p>
                                        <p className="text-xs text-ink-500 mt-0.5 mb-0 tabular-nums">
                                            {duplicate.phone}
                                            {duplicate.email && ` · ${duplicate.email}`}
                                        </p>
                                    </div>
                                    <Button
                                        size="sm"
                                        variant="secondary"
                                        icon={Merge}
                                        onClick={() => setPendingMerge(duplicate)}
                                    >
                                        Merge in
                                    </Button>
                                </li>
                            ))}
                        </ul>
                    )}
                </Modal>
            )}

            {pendingMerge && (
                <ConfirmDialog
                    title="Merge these contacts?"
                    message={`Every message, deal, task and note on ${
                        pendingMerge.name || pendingMerge.phone
                    } moves to ${
                        contact.name || contact.phone
                    }, and that record is then removed. This cannot be undone.`}
                    confirmLabel="Merge"
                    onConfirm={runMerge}
                    onClose={() => setPendingMerge(null)}
                />
            )}
        </div>
    );
}
