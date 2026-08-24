import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Clock, ListChecks, Plus, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import useSelection from "../../hooks/useSelection";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import BulkActionBar from "../../components/ui/BulkActionBar";
import Button from "../../components/ui/Button";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Tabs from "../../components/ui/Tabs";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { formatDateTime, timeAgo, toLocalInput } from "../../components/crm/CrmBits";

/**
 * The work list.
 *
 * Deliberately the plainest screen in the CRM: it is opened to find the next
 * thing to do and closed again, so it is a list with a tick box, not a
 * dashboard. Everything that needs explaining lives on the contact profile
 * this links back to.
 */

const TABS = [
    { id: "today", label: "Due today", icon: Clock },
    { id: "overdue", label: "Overdue", icon: Clock },
    { id: "mine", label: "Mine", icon: ListChecks },
    { id: "all", label: "Everything", icon: ListChecks },
    { id: "done", label: "Done", icon: CheckCircle2 },
];

const KIND_LABEL = { FOLLOW_UP: "Follow up", CALL: "Call", MEETING: "Meeting", OTHER: "Other" };

export default function Tasks() {
    const toast = useToast();
    const [tasks, setTasks] = useState([]);
    const [team, setTeam] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [scope, setScope] = useState("today");
    const [form, setForm] = useState(null);
    const [busy, setBusy] = useState(false);
    const [bulkBusy, setBulkBusy] = useState(false);
    const selection = useSelection(tasks);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const params = { ordering: "due_at", page_size: 100 };
            if (scope === "done") {
                params.status = "DONE";
                params.ordering = "-due_at";
            } else {
                params.status = "OPEN";
                if (scope !== "all") params.scope = scope;
            }
            const response = await api.get("/crm/tasks/", { params });
            setTasks(response.data.results || []);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load your tasks."));
        } finally {
            setLoading(false);
        }
    }, [scope]);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        api.get("/crm/team/")
            .then((response) => setTeam(response.data.results || []))
            .catch(() => setTeam([]));
    }, []);

    const complete = async (task) => {
        try {
            await api.post(`/crm/tasks/${task.id}/complete/`);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not complete that task."));
        }
    };

    const reopen = async (task) => {
        try {
            await api.post(`/crm/tasks/${task.id}/reopen/`);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not reopen that task."));
        }
    };

    const save = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            const payload = {
                title: form.title,
                notes: form.notes || "",
                kind: form.kind,
                due_at: new Date(form.due_at).toISOString(),
                assigned_to: form.assigned_to ? Number(form.assigned_to) : null,
            };
            if (form.id) {
                await api.patch(`/crm/tasks/${form.id}/`, payload);
            } else {
                await api.post("/crm/tasks/", payload);
            }
            setForm(null);
            toast.success(form.id ? "Task updated." : "Task added.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that task."));
        } finally {
            setBusy(false);
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const result = await runBulk("/crm/tasks/bulk/", { action, ids: selection.selectedIds });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            setError(describeBulkFailures(result, (id) => tasks.find((task) => task.id === id)?.title));
        }
        selection.clear();
        setBulkBusy(false);
        await load();
    };

    const columns = [
        {
            key: "title",
            header: "Task",
            render: (task) => (
                <div className="min-w-0">
                    <p
                        className={`font-semibold m-0 truncate ${
                            task.status === "DONE" ? "text-ink-400 line-through" : "text-ink-900"
                        }`}
                    >
                        {task.title}
                    </p>
                    {task.contact_name && (
                        <Link
                            to={`/contacts/${task.contact}`}
                            className="text-xs text-brand-700 no-underline hover:underline"
                        >
                            {task.contact_name}
                        </Link>
                    )}
                </div>
            ),
        },
        {
            key: "kind",
            header: "Kind",
            hideBelow: "md",
            render: (task) => <span className="text-ink-600">{KIND_LABEL[task.kind] || task.kind}</span>,
        },
        {
            key: "due_at",
            header: "Due",
            render: (task) => (
                <div className="whitespace-nowrap">
                    <span className={task.is_overdue ? "text-red-600 font-semibold" : "text-ink-700"}>
                        {timeAgo(task.due_at)}
                    </span>
                    <p className="text-xs text-ink-400 m-0 hidden sm:block">{formatDateTime(task.due_at)}</p>
                </div>
            ),
        },
        {
            key: "assigned_to",
            header: "Who",
            hideBelow: "lg",
            render: (task) => <span className="text-ink-600">{task.assigned_to_username || "Nobody"}</span>,
        },
        {
            key: "status",
            header: "Status",
            hideBelow: "sm",
            render: (task) =>
                task.status === "DONE" ? (
                    <Badge tone="green">Done</Badge>
                ) : task.is_overdue ? (
                    <Badge tone="red">Overdue</Badge>
                ) : (
                    <Badge tone="blue">Open</Badge>
                ),
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (task) => (
                <div className="flex justify-end gap-3 whitespace-nowrap">
                    {task.status === "OPEN" ? (
                        <RowAction tone="brand" onClick={() => complete(task)}>
                            Done
                        </RowAction>
                    ) : (
                        <RowAction onClick={() => reopen(task)}>Reopen</RowAction>
                    )}
                    <RowAction
                        onClick={() =>
                            setForm({
                                id: task.id,
                                title: task.title,
                                notes: task.notes || "",
                                kind: task.kind,
                                due_at: toLocalInput(task.due_at),
                                assigned_to: task.assigned_to || "",
                            })
                        }
                    >
                        Edit
                    </RowAction>
                </div>
            ),
        },
    ];

    const openCount = tasks.filter((task) => task.status === "OPEN").length;

    return (
        <div>
            <PageHeader
                title="Tasks"
                description={loading ? "Everything you have promised to do." : `${tasks.length} shown`}
                actions={
                    <Button
                        icon={Plus}
                        onClick={() =>
                            setForm({
                                title: "", notes: "", kind: "FOLLOW_UP",
                                due_at: toLocalInput(Date.now() + 86400000), assigned_to: "",
                            })
                        }
                    >
                        Add task
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            <Tabs tabs={TABS} active={scope} onChange={setScope} />

            <BulkActionBar count={selection.selectedCount} label="task" onClear={selection.clear}>
                <Button
                    size="sm"
                    variant="secondary"
                    icon={CheckCircle2}
                    disabled={bulkBusy}
                    onClick={() => runBulkAction("complete")}
                >
                    Mark done
                </Button>
                <Button
                    size="sm"
                    variant="danger"
                    icon={Trash2}
                    disabled={bulkBusy}
                    onClick={() => runBulkAction("delete")}
                >
                    Delete
                </Button>
            </BulkActionBar>

            {loading ? (
                <TableSkeleton rows={6} columns={5} />
            ) : (
                <DataTable
                    columns={columns}
                    rows={tasks}
                    selection={selection}
                    empty={
                        <EmptyState
                            icon={scope === "done" ? CheckCircle2 : ListChecks}
                            title={
                                scope === "overdue"
                                    ? "Nothing is overdue"
                                    : scope === "done"
                                      ? "Nothing finished yet"
                                      : openCount === 0 && scope === "today"
                                        ? "Nothing due today"
                                        : "No tasks here"
                            }
                            description="A task is anything you have promised a customer, with a time it has to happen by. Whatever is due shows up on your dashboard."
                            action={
                                <Button
                                    icon={Plus}
                                    onClick={() =>
                                        setForm({
                                            title: "", notes: "", kind: "FOLLOW_UP",
                                            due_at: toLocalInput(Date.now() + 86400000), assigned_to: "",
                                        })
                                    }
                                >
                                    Add a task
                                </Button>
                            }
                        />
                    }
                />
            )}

            {form && (
                <Modal
                    title={form.id ? "Edit task" : "Add task"}
                    onClose={() => setForm(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="task-page-form" loading={busy}>
                                Save task
                            </Button>
                        </>
                    }
                >
                    <form id="task-page-form" onSubmit={save} className="space-y-4">
                        <Field label="What needs doing?" required>
                            <Input
                                autoFocus
                                required
                                value={form.title}
                                onChange={(event) => setForm({ ...form, title: event.target.value })}
                                placeholder="Call about the bulk quote"
                            />
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="When" required>
                                <Input
                                    type="datetime-local"
                                    required
                                    value={form.due_at}
                                    onChange={(event) => setForm({ ...form, due_at: event.target.value })}
                                />
                            </Field>
                            <Field label="Kind">
                                <Select
                                    value={form.kind}
                                    onChange={(event) => setForm({ ...form, kind: event.target.value })}
                                >
                                    {Object.entries(KIND_LABEL).map(([value, label]) => (
                                        <option key={value} value={value}>
                                            {label}
                                        </option>
                                    ))}
                                </Select>
                            </Field>
                        </div>
                        <Field label="Who">
                            <Select
                                value={form.assigned_to}
                                onChange={(event) => setForm({ ...form, assigned_to: event.target.value })}
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
                                rows={3}
                                value={form.notes}
                                onChange={(event) => setForm({ ...form, notes: event.target.value })}
                            />
                        </Field>
                    </form>
                </Modal>
            )}
        </div>
    );
}
