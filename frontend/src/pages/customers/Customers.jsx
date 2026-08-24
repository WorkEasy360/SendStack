import { useEffect, useState } from "react";
import { Building2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import api, { apiErrorMessage } from "../../services/api";
import { useAuth } from "../../context/AuthContext";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card from "../../components/ui/Card";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import StatCard from "../../components/ui/StatCard";
import { Field, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * Every workspace beneath this account, and how each is doing.
 *
 * The list answers "who should I be worried about" — a customer with no
 * connected device, a plan running out, credits at zero — so those are the
 * columns, rather than a directory of names.
 */

const STATUS_TONE = {
    ACTIVE: "green",
    TRIALING: "blue",
    PAST_DUE: "amber",
    CANCELLED: "red",
};

function relativeTime(value) {
    if (!value) return "Never";
    const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
    if (minutes < 60) return `${Math.max(minutes, 0)}m ago`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}h ago`;
    return `${Math.round(minutes / 1440)}d ago`;
}

export default function Customers() {
    const toast = useToast();
    const { user } = useAuth();
    const [customers, setCustomers] = useState(null);
    const [plans, setPlans] = useState([]);
    const [error, setError] = useState("");
    const [refreshing, setRefreshing] = useState(false);
    const [assigning, setAssigning] = useState(null);
    const [usageFor, setUsageFor] = useState(null);

    const load = async ({ silent = false } = {}) => {
        if (silent) setRefreshing(true);
        try {
            const [customersRes, plansRes] = await Promise.all([
                api.get("/tenants/customers/"),
                api.get("/billing/plans/"),
            ]);
            setCustomers(customersRes.data.customers || []);
            setPlans(plansRes.data.results || []);
            setError("");
        } catch (err) {
            setCustomers([]);
            setError(apiErrorMessage(err, "Unable to load your customers."));
        } finally {
            setRefreshing(false);
        }
    };

    useEffect(() => {
        load();
    }, []);

    const assignPlan = async (customer, planId) => {
        try {
            if (customer.subscription_id) {
                await api.patch(`/billing/subscriptions/${customer.subscription_id}/`, { plan: planId });
            } else {
                await api.post("/billing/subscriptions/", { tenant: customer.tenant_id, plan: planId });
            }
            await load({ silent: true });
            toast.success("Plan updated.");
            setAssigning(null);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to change this customer's plan."));
        }
    };

    if (customers === null) return <Spinner />;

    const totals = customers.reduce(
        (sum, customer) => ({
            customers: sum.customers + 1,
            messages: sum.messages + customer.messages_sent,
            credits: sum.credits + customer.available_credits,
            offline: sum.offline + (customer.connected_devices === 0 ? 1 : 0),
        }),
        { customers: 0, messages: 0, credits: 0, offline: 0 }
    );

    const columns = [
        {
            key: "name",
            header: "Customer",
            render: (row) => (
                <div className="min-w-0">
                    <span className="font-semibold text-ink-900">{row.name}</span>
                    {!row.is_active && <Badge tone="red" className="ml-2">Suspended</Badge>}
                    <p className="text-xs text-ink-500 m-0">
                        {row.users} user{row.users === 1 ? "" : "s"} · {row.contacts.toLocaleString()} contacts
                    </p>
                </div>
            ),
        },
        {
            key: "plan",
            header: "Plan",
            render: (row) =>
                row.plan ? (
                    <div>
                        <span className="text-ink-900">{row.plan}</span>
                        {row.subscription_status && row.subscription_status !== "ACTIVE" && (
                            <Badge tone={STATUS_TONE[row.subscription_status] || "gray"} className="ml-2">
                                {row.subscription_status.replace("_", " ").toLowerCase()}
                            </Badge>
                        )}
                    </div>
                ) : (
                    <span className="text-ink-400">No plan</span>
                ),
        },
        {
            key: "devices",
            header: "Devices",
            render: (row) => (
                <span className={row.connected_devices === 0 ? "text-amber-700" : "text-ink-600"}>
                    <span className="tabular-nums font-semibold">{row.connected_devices}</span>
                    <span className="tabular-nums"> / {row.devices}</span>
                    {row.connected_devices === 0 && row.devices > 0 && " offline"}
                </span>
            ),
        },
        {
            key: "messages_sent",
            header: "Sent",
            render: (row) => (
                <div>
                    <span className="tabular-nums text-ink-900">{row.messages_sent.toLocaleString()}</span>
                    {row.messages_failed > 0 && (
                        <span className="text-xs text-red-600 ml-1.5 tabular-nums">
                            {row.messages_failed} failed
                        </span>
                    )}
                </div>
            ),
        },
        {
            key: "available_credits",
            header: "Credits",
            render: (row) => (
                <span
                    className={`tabular-nums ${row.available_credits <= 0 ? "text-red-600 font-semibold" : "text-ink-600"}`}
                >
                    {row.available_credits.toLocaleString()}
                </span>
            ),
        },
        {
            key: "last_activity_at",
            header: "Last activity",
            render: (row) => <span className="text-xs text-ink-500">{relativeTime(row.last_activity_at)}</span>,
        },
        {
            key: "actions",
            header: "",
            render: (row) => (
                <div className="flex gap-1 justify-end">
                    <Button size="xs" variant="ghost" onClick={() => setUsageFor(row)}>
                        Usage
                    </Button>
                    <Button size="xs" variant="secondary" onClick={() => setAssigning(row)}>
                        {row.plan ? "Change plan" : "Assign plan"}
                    </Button>
                </div>
            ),
        },
    ];

    return (
        <div>
            <PageHeader
                title="Customers"
                description={
                    user?.role === "SUPERADMIN"
                        ? "Every workspace on this platform, what each is on, and how much it is using."
                        : "Every workspace you have created, what they are on, and how much they are using."
                }
                actions={
                    <Button
                        variant="secondary"
                        icon={RefreshCw}
                        loading={refreshing}
                        onClick={() => load({ silent: true })}
                    >
                        Refresh
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            {customers.length > 0 && (
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                    <StatCard label="Customers" value={totals.customers} icon={Building2} tone="violet" />
                    <StatCard label="Messages sent" value={totals.messages} />
                    <StatCard label="Credits held" value={totals.credits} />
                    <StatCard
                        label="Nothing connected"
                        value={totals.offline}
                        hint={totals.offline > 0 ? "These cannot send" : ""}
                        tone={totals.offline > 0 ? "red" : "default"}
                    />
                </div>
            )}

            <DataTable
                columns={columns}
                rows={customers}
                rowKey={(row) => row.tenant_id}
                empty={
                    <EmptyState
                        icon={Building2}
                        title="No customers yet"
                        description="Create a customer account and it appears here with its usage."
                        action={
                            <Link to="/users">
                                <Button>Go to Users</Button>
                            </Link>
                        }
                    />
                }
            />

            {assigning && (
                <AssignPlanModal
                    customer={assigning}
                    plans={plans}
                    onAssign={assignPlan}
                    onClose={() => setAssigning(null)}
                />
            )}

            {usageFor && <UsageModal customer={usageFor} onClose={() => setUsageFor(null)} />}
        </div>
    );
}

function AssignPlanModal({ customer, plans, onAssign, onClose }) {
    const [planId, setPlanId] = useState(customer.plan_id || "");
    const [saving, setSaving] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        if (!planId) return;
        setSaving(true);
        await onAssign(customer, Number(planId));
        setSaving(false);
    };

    const available = plans.filter((plan) => plan.is_active || plan.id === customer.plan_id);

    return (
        <Modal title={`Plan for ${customer.name}`} onClose={onClose}>
            <form onSubmit={submit}>
                {available.length === 0 ? (
                    <Alert tone="warning" title="No plans to assign">
                        Define a plan first — there is nothing to put this customer on yet.
                    </Alert>
                ) : (
                    <Field label="Plan" required>
                        <Select value={planId} onChange={(event) => setPlanId(event.target.value)} required>
                            <option value="">Choose a plan...</option>
                            {available.map((plan) => (
                                <option key={plan.id} value={plan.id}>
                                    {plan.name}
                                    {plan.max_messages_per_month
                                        ? ` — ${plan.max_messages_per_month.toLocaleString()} messages/month`
                                        : " — unlimited messages"}
                                </option>
                            ))}
                        </Select>
                    </Field>
                )}

                <p className="text-xs text-ink-500 mb-5">
                    Changing the plan takes effect immediately. It does not restart their billing period — use Renew
                    for that.
                </p>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving} disabled={!planId}>
                        Save plan
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

/** Limits against usage for one customer, read on demand. */
function UsageModal({ customer, onClose }) {
    const [state, setState] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get(`/billing/usage/${customer.tenant_id}/`)
            .then((response) => setState(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load this customer's usage.")));
    }, [customer.tenant_id]);

    const LABELS = {
        messages_per_month: "Messages this period",
        contacts: "Contacts",
        devices: "WhatsApp devices",
        users: "Team members",
    };

    return (
        <Modal title={`${customer.name} — usage`} onClose={onClose} width="lg">
            <Alert>{error}</Alert>

            {!state && !error ? (
                <Spinner />
            ) : state ? (
                <div>
                    {state.plan ? (
                        <Card className="mb-4">
                            <div className="flex items-center justify-between gap-3">
                                <div>
                                    <p className="text-sm font-bold text-ink-900 m-0">{state.plan.name}</p>
                                    <p className="text-xs text-ink-500 m-0">
                                        Period began {new Date(state.current_period_start).toLocaleDateString()}
                                    </p>
                                </div>
                                <Badge tone={STATUS_TONE[state.status] || "gray"}>
                                    {(state.status || "").replace("_", " ").toLowerCase()}
                                </Badge>
                            </div>
                        </Card>
                    ) : (
                        <Alert tone="info" title="No plan">
                            This customer is on no plan, so nothing is capped.
                        </Alert>
                    )}

                    <ul className="list-none p-0 m-0 space-y-3">
                        {Object.entries(state.metrics || {}).map(([key, metric]) => (
                            <li key={key}>
                                <div className="flex justify-between text-sm mb-1">
                                    <span className="text-ink-700">{LABELS[key] || key}</span>
                                    <span className="tabular-nums text-ink-900 font-semibold">
                                        {metric.used.toLocaleString()}
                                        {metric.limit ? ` / ${metric.limit.toLocaleString()}` : " · unlimited"}
                                    </span>
                                </div>
                                {metric.limit > 0 && (
                                    <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
                                        <div
                                            className={`h-full rounded-full ${
                                                metric.exceeded ? "bg-red-500" : "bg-brand-500"
                                            }`}
                                            style={{ width: `${metric.percent || 0}%` }}
                                        />
                                    </div>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            ) : null}

            <div className="flex justify-end mt-5">
                <Button variant="secondary" onClick={onClose}>
                    Close
                </Button>
            </div>
        </Modal>
    );
}
