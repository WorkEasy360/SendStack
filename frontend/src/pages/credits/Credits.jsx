import { useEffect, useState } from "react";
import { Coins, Lock, Minus, Plus, Wallet } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useAuth } from "../../context/AuthContext";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import StatCard from "../../components/ui/StatCard";
import Modal from "../../components/ui/Modal";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import DataTable from "../../components/ui/DataTable";
import { StatCardSkeleton, TableSkeleton } from "../../components/ui/Skeleton";
import { CheckboxField, Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

const TYPE_TONE = {
    CREDIT: "green",
    DEBIT: "red",
    ASSIGN: "blue",
    DEDUCT: "amber",
    RESERVE: "amber",
    RELEASE: "gray",
    REFUND: "green",
};

export default function Credits() {
    const { user } = useAuth();
    const toast = useToast();
    const canAssign = user?.role === "SUPERADMIN" || user?.role === "RESELLER";

    const [balance, setBalance] = useState(null);
    const [transactions, setTransactions] = useState([]);
    const [managedUsers, setManagedUsers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [showAssign, setShowAssign] = useState(false);
    const [form, setForm] = useState({ target_user_id: "", amount: "", reference: "", deduct: true });
    const [assignError, setAssignError] = useState("");
    const [saving, setSaving] = useState(false);

    const [showDeduct, setShowDeduct] = useState(false);
    const [deductForm, setDeductForm] = useState({ target_user_id: "", amount: "", reference: "" });
    const [deductError, setDeductError] = useState("");
    const [deducting, setDeducting] = useState(false);

    const load = async () => {
        try {
            const requests = [
                api.get("/credits/balance/"),
                api.get("/credits/transactions/", { params: { ordering: "-created_at" } }),
            ];
            if (canAssign) requests.push(api.get("/auth/users/"));
            const [balanceRes, txRes, usersRes] = await Promise.all(requests);
            setBalance(balanceRes.data);
            setTransactions(txRes.data.results || []);
            if (usersRes) setManagedUsers(usersRes.data.results || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load credit information."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const assignCredits = async (event) => {
        event.preventDefault();
        setSaving(true);
        setAssignError("");
        try {
            await api.post("/credits/assign/", {
                target_user_id: Number(form.target_user_id),
                amount: Number(form.amount),
                reference: form.reference,
                deduct: form.deduct,
            });
            setShowAssign(false);
            setForm({ target_user_id: "", amount: "", reference: "", deduct: true });
            toast.success("Credits assigned.");
            await load();
        } catch (err) {
            setAssignError(apiErrorMessage(err, "Unable to assign credits."));
        } finally {
            setSaving(false);
        }
    };

    const deductCredits = async (event) => {
        event.preventDefault();
        setDeducting(true);
        setDeductError("");
        try {
            await api.post("/credits/deduct/", {
                target_user_id: Number(deductForm.target_user_id),
                amount: Number(deductForm.amount),
                reference: deductForm.reference,
            });
            setShowDeduct(false);
            setDeductForm({ target_user_id: "", amount: "", reference: "" });
            toast.success("Credits deducted.");
            await load();
        } catch (err) {
            setDeductError(apiErrorMessage(err, "Unable to deduct credits."));
        } finally {
            setDeducting(false);
        }
    };

    const columns = [
        {
            key: "type",
            header: "Type",
            render: (tx) => <Badge tone={TYPE_TONE[tx.type] || "blue"}>{tx.type}</Badge>,
        },
        {
            key: "amount",
            header: "Amount",
            align: "right",
            render: (tx) => (
                <span className={`tabular-nums font-semibold ${tx.amount < 0 ? "text-red-600" : "text-ink-900"}`}>
                    {tx.amount > 0 ? `+${tx.amount}` : tx.amount}
                </span>
            ),
        },
        {
            key: "balance_after",
            header: "Balance After",
            align: "right",
            hideBelow: "sm",
            render: (tx) => <span className="tabular-nums">{tx.balance_after}</span>,
        },
        {
            key: "reference",
            header: "Reference",
            hideBelow: "md",
            render: (tx) => <span className="text-ink-500">{tx.reference || "—"}</span>,
        },
        {
            key: "created_at",
            header: "Date",
            align: "right",
            hideBelow: "sm",
            render: (tx) => <span className="text-ink-500">{new Date(tx.created_at).toLocaleString()}</span>,
        },
    ];

    return (
        <div>
            <PageHeader
                title="Credits"
                description="Track your balance and see the full transaction ledger."
                actions={
                    canAssign && (
                        <>
                            <Button variant="secondary" icon={Minus} onClick={() => setShowDeduct(true)}>
                                Deduct
                            </Button>
                            <Button icon={Plus} onClick={() => setShowAssign(true)}>
                                Assign Credits
                            </Button>
                        </>
                    )
                }
            />

            <Alert>{error}</Alert>

            {loading ? (
                <div className="space-y-4">
                    <StatCardSkeleton count={3} />
                    <TableSkeleton rows={5} columns={5} />
                </div>
            ) : (
                <>
                    {balance && (
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
                            <StatCard label="Total balance" value={balance.credits} icon={Wallet} tone="brand" />
                            <StatCard
                                label="Reserved"
                                value={balance.reserved_credits}
                                hint="Held against in-flight campaigns"
                                icon={Lock}
                                tone="amber"
                            />
                            <StatCard label="Available" value={balance.available_credits} icon={Coins} tone="blue" />
                        </div>
                    )}

                    <h2 className="text-base font-bold text-ink-900 mb-3">Transaction history</h2>
                    <DataTable
                        columns={columns}
                        rows={transactions}
                        empty={
                            <EmptyState
                                icon={Coins}
                                title="No transactions yet"
                                description="Credit assignments, reservations and campaign spend appear here."
                            />
                        }
                    />
                </>
            )}

            {showAssign && (
                <Modal title="Assign Credits" onClose={() => setShowAssign(false)}>
                    <form onSubmit={assignCredits}>
                        <Alert>{assignError}</Alert>
                        <Field label="User" required>
                            <Select
                                value={form.target_user_id}
                                onChange={(e) => setForm({ ...form, target_user_id: e.target.value })}
                                required
                            >
                                <option value="">Select a user...</option>
                                {managedUsers.map((u) => (
                                    <option key={u.id} value={u.id}>
                                        {u.username} ({u.role})
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field label="Amount" required>
                            <Input
                                type="number"
                                min={1}
                                value={form.amount}
                                onChange={(e) => setForm({ ...form, amount: e.target.value })}
                                required
                            />
                        </Field>
                        <Field label="Reference" hint="Optional note for this transaction.">
                            <Input value={form.reference} onChange={(e) => setForm({ ...form, reference: e.target.value })} />
                        </Field>
                        {user?.role === "RESELLER" && (
                            <div className="mb-5">
                                <CheckboxField
                                    label="Deduct from my balance"
                                    description="If unchecked, the user is topped up without reducing your own balance."
                                    checked={form.deduct}
                                    onChange={(e) => setForm({ ...form, deduct: e.target.checked })}
                                />
                            </div>
                        )}
                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={() => setShowAssign(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" loading={saving}>
                                Assign Credits
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}

            {showDeduct && (
                <Modal
                    title="Deduct Credits"
                    description="Removes credits from a user or reseller below you in the hierarchy and returns them to your own balance."
                    onClose={() => setShowDeduct(false)}
                >
                    <form onSubmit={deductCredits}>
                        <Alert>{deductError}</Alert>
                        <Field label="User" required>
                            <Select
                                value={deductForm.target_user_id}
                                onChange={(e) => setDeductForm({ ...deductForm, target_user_id: e.target.value })}
                                required
                            >
                                <option value="">Select a user...</option>
                                {managedUsers.map((u) => (
                                    <option key={u.id} value={u.id}>
                                        {u.username} ({u.role})
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field label="Amount" required>
                            <Input
                                type="number"
                                min={1}
                                value={deductForm.amount}
                                onChange={(e) => setDeductForm({ ...deductForm, amount: e.target.value })}
                                required
                            />
                        </Field>
                        <Field label="Reference" hint="Optional note for this transaction.">
                            <Input
                                value={deductForm.reference}
                                onChange={(e) => setDeductForm({ ...deductForm, reference: e.target.value })}
                            />
                        </Field>
                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={() => setShowDeduct(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" variant="danger" loading={deducting}>
                                Deduct Credits
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}
        </div>
    );
}
