import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, Trash2, UserCheck, UserCog, UserX } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import useSelection from "../../hooks/useSelection";
import { useAuth } from "../../context/AuthContext";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Modal from "../../components/ui/Modal";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import BulkActionBar from "../../components/ui/BulkActionBar";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

const EMPTY_FORM = { username: "", email: "", password: "", role: "USER", tenant_name: "", mobile: "" };

const ROLE_TONE = { SUPERADMIN: "violet", RESELLER: "blue", USER: "gray" };

export default function Users() {
    const { user, loginAs } = useAuth();
    const navigate = useNavigate();
    const toast = useToast();

    const [users, setUsers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [showModal, setShowModal] = useState(false);
    const [form, setForm] = useState(EMPTY_FORM);
    const [formError, setFormError] = useState("");
    const [saving, setSaving] = useState(false);

    const [pendingDelete, setPendingDelete] = useState(null);
    const [pendingLoginAs, setPendingLoginAs] = useState(null);
    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);

    // Your own account is never selectable: the row already hides its
    // actions, and the API refuses a bulk change to yourself outright.
    const selection = useSelection(users, { canSelect: (row) => row.id !== user?.id });

    const load = async () => {
        try {
            const response = await api.get("/auth/users/");
            setUsers(response.data.results || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load users."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
    }, []);

    const create = async (event) => {
        event.preventDefault();
        setSaving(true);
        setFormError("");
        try {
            await api.post("/auth/users/", form);
            setShowModal(false);
            setForm(EMPTY_FORM);
            toast.success("User created.");
            await load();
        } catch (err) {
            setFormError(apiErrorMessage(err, "Unable to create this user."));
        } finally {
            setSaving(false);
        }
    };

    const toggleActive = async (target) => {
        try {
            await api.patch(`/auth/users/${target.id}/`, { is_active: !target.is_active });
            toast.success(target.is_active ? "Account suspended." : "Account activated.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this user."));
        }
        await load();
    };

    const loginAsTarget = async (target, reason) => {
        const result = await loginAs(target, reason);
        if (!result.success) {
            setError(result.message);
            setPendingLoginAs(null);
            return;
        }
        navigate("/");
    };

    const deleteTarget = async (target) => {
        try {
            await api.delete(`/auth/users/${target.id}/`);
            toast.success(`${target.username} deleted.`);
            await load();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this user."));
        } finally {
            setPendingDelete(null);
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const result = await runBulk("/auth/users/bulk/", { action, ids: selection.selectedIds });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            setError(describeBulkFailures(result, (id) => users.find((entry) => entry.id === id)?.username));
        }
        selection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        await load();
    };

    const columns = [
        {
            key: "username",
            header: "Username",
            render: (u) => (
                <div className="flex items-center gap-2.5 min-w-0">
                    <span className="shrink-0 w-8 h-8 rounded-lg bg-ink-900 text-ink-50 text-xs font-bold flex items-center justify-center uppercase">
                        {(u.username || "?").charAt(0)}
                    </span>
                    <div className="min-w-0">
                        <p className="font-semibold text-ink-900 truncate m-0">{u.username}</p>
                        <p className="text-xs text-ink-500 truncate m-0">{u.email || "—"}</p>
                    </div>
                </div>
            ),
        },
        { key: "role", header: "Role", render: (u) => <Badge tone={ROLE_TONE[u.role] || "gray"}>{u.role}</Badge> },
        {
            key: "tenant",
            header: "Business",
            hideBelow: "md",
            render: (u) => <span className="text-ink-600">{u.tenant_name}</span>,
        },
        {
            key: "credits",
            header: "Credits",
            align: "right",
            hideBelow: "sm",
            render: (u) => <span className="tabular-nums">{u.available_credits}</span>,
        },
        {
            key: "status",
            header: "Status",
            render: (u) => <Badge tone={u.is_active ? "green" : "gray"}>{u.is_active ? "Active" : "Suspended"}</Badge>,
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (u) =>
                u.id !== user?.id && (
                    <div className="flex justify-end gap-3 whitespace-nowrap">
                        <RowAction onClick={() => setPendingLoginAs(u)}>Login as</RowAction>
                        <RowAction onClick={() => toggleActive(u)}>{u.is_active ? "Suspend" : "Activate"}</RowAction>
                        <RowAction tone="danger" onClick={() => setPendingDelete(u)}>
                            Delete
                        </RowAction>
                    </div>
                ),
        },
    ];

    return (
        <div>
            <PageHeader
                title={user?.role === "SUPERADMIN" ? "Resellers & Users" : "Your Users"}
                description={loading ? "Manage the accounts under you." : `${users.length} account${users.length === 1 ? "" : "s"}`}
                actions={
                    <Button icon={Plus} onClick={() => setShowModal(true)}>
                        Create User
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            <BulkActionBar count={selection.selectedCount} label="account" onClear={selection.clear}>
                <Button
                    size="sm"
                    variant="secondary"
                    icon={UserCheck}
                    disabled={bulkBusy}
                    onClick={() => runBulkAction("activate")}
                >
                    Activate
                </Button>
                <Button
                    size="sm"
                    variant="secondary"
                    icon={UserX}
                    disabled={bulkBusy}
                    onClick={() => setPendingBulk("suspend")}
                >
                    Suspend
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

            {loading ? (
                <TableSkeleton rows={5} columns={5} />
            ) : (
                <DataTable
                    columns={columns}
                    rows={users}
                    selection={selection}
                    empty={
                        <EmptyState
                            icon={UserCog}
                            title="No accounts yet"
                            description="Create a user or reseller account to give someone access to their own workspace."
                            action={
                                <Button icon={Plus} onClick={() => setShowModal(true)}>
                                    Create User
                                </Button>
                            }
                        />
                    }
                />
            )}

            {showModal && (
                <Modal title="Create User" onClose={() => setShowModal(false)}>
                    <form onSubmit={create}>
                        <Alert>{formError}</Alert>
                        <Field label="Username" required>
                            <Input
                                value={form.username}
                                onChange={(e) => setForm({ ...form, username: e.target.value })}
                                autoFocus
                                required
                            />
                        </Field>
                        <Field label="Email" required>
                            <Input
                                type="email"
                                value={form.email}
                                onChange={(e) => setForm({ ...form, email: e.target.value })}
                                required
                            />
                        </Field>
                        <Field label="Password" hint="At least 12 characters." required>
                            <Input
                                type="password"
                                value={form.password}
                                onChange={(e) => setForm({ ...form, password: e.target.value })}
                                minLength={12}
                                required
                            />
                        </Field>
                        {user?.role === "SUPERADMIN" && (
                            <Field label="Role">
                                <Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                                    <option value="USER">User</option>
                                    <option value="RESELLER">Reseller</option>
                                </Select>
                            </Field>
                        )}
                        <Field label="Business Name" hint="Creates a new tenant for this account." required>
                            <Input
                                value={form.tenant_name}
                                onChange={(e) => setForm({ ...form, tenant_name: e.target.value })}
                                required
                            />
                        </Field>
                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={() => setShowModal(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" loading={saving}>
                                Create User
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}

            {pendingBulk && (
                <ConfirmDialog
                    title={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount} account${selection.selectedCount === 1 ? "" : "s"}`
                            : `Suspend ${selection.selectedCount} account${selection.selectedCount === 1 ? "" : "s"}`
                    }
                    message={
                        pendingBulk === "delete"
                            ? `Delete the ${selection.selectedCount} selected account${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? Their available credits are returned to the parent account. This cannot be undone.`
                            : `Suspend the ${selection.selectedCount} selected account${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? They stay in the list and can be activated again at any time.`
                    }
                    tone={pendingBulk === "delete" ? "danger" : "default"}
                    confirmLabel={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount}`
                            : `Suspend ${selection.selectedCount}`
                    }
                    onConfirm={() => runBulkAction(pendingBulk)}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {pendingLoginAs && (
                <ConfirmDialog
                    title={`Log in as ${pendingLoginAs.username}`}
                    message="You'll act as this account until you exit. The reason below is recorded in the audit log."
                    tone="default"
                    confirmLabel="Log in as user"
                    promptLabel="Reason"
                    promptHint="Visible to administrators in the audit trail."
                    promptDefault="Support / troubleshooting"
                    onConfirm={(reason) => loginAsTarget(pendingLoginAs, reason)}
                    onClose={() => setPendingLoginAs(null)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete account"
                    message={`Delete ${pendingDelete.username}? Their ${pendingDelete.available_credits} available credit${
                        pendingDelete.available_credits === 1 ? "" : "s"
                    } will be returned to the parent account. This cannot be undone.`}
                    confirmLabel="Delete account"
                    onConfirm={() => deleteTarget(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}
