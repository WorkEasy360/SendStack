import { useEffect, useState } from "react";
import { Layers, Pencil, Plus, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * The plans this account sells to its customers.
 *
 * Limits use 0 for unlimited throughout, matching the rest of the product
 * (team capacity, AI reply caps). The form says so on every field rather than
 * relying on anyone remembering it.
 */

const FEATURES = [
    { key: "ai_agent", label: "AI agent", hint: "Answering from a knowledge base, suggestions and summaries." },
    { key: "workflows", label: "Workflows", hint: "The drag-and-drop automation builder." },
    { key: "click_tracking", label: "Click tracking", hint: "Short links and per-recipient attribution." },
    { key: "api_access", label: "API access", hint: "Programmatic access to their data." },
    { key: "number_filter", label: "Number filter", hint: "Check which numbers are on WhatsApp through a linked number." },
    { key: "group_grabber", label: "Group grabber", hint: "Import the members of WhatsApp groups a linked number is in." },
];

const LIMIT_FIELDS = [
    { key: "max_messages_per_month", label: "Messages per month", hint: "Enforced at send time. 0 for unlimited." },
    { key: "max_contacts", label: "Contacts", hint: "0 for unlimited." },
    { key: "max_devices", label: "WhatsApp devices", hint: "0 for unlimited." },
    { key: "max_users", label: "Team members", hint: "0 for unlimited." },
];

const EMPTY_PLAN = {
    name: "",
    description: "",
    price_cents: 0,
    currency: "USD",
    interval: "MONTHLY",
    max_messages_per_month: 0,
    max_contacts: 0,
    max_devices: 0,
    max_users: 0,
    features: {},
    is_active: true,
};

function formatPrice(cents, currency) {
    if (!cents) return "Free";
    try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(
            cents / 100
        );
    } catch {
        // An unknown currency code should not take the page down with it.
        return `${(cents / 100).toFixed(2)} ${currency || ""}`.trim();
    }
}

const limitLabel = (value) => (value ? value.toLocaleString() : "Unlimited");

export default function Plans() {
    const toast = useToast();
    const [plans, setPlans] = useState(null);
    const [error, setError] = useState("");
    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);

    const load = async () => {
        try {
            const response = await api.get("/billing/plans/");
            setPlans(response.data.results || []);
            setError("");
        } catch (err) {
            setPlans([]);
            setError(apiErrorMessage(err, "Unable to load your plans."));
        }
    };

    useEffect(() => {
        load();
    }, []);

    const submit = async (form) => {
        const payload = {
            name: form.name,
            description: form.description,
            price_cents: Number(form.price_cents) || 0,
            currency: form.currency,
            interval: form.interval,
            max_messages_per_month: Number(form.max_messages_per_month) || 0,
            max_contacts: Number(form.max_contacts) || 0,
            max_devices: Number(form.max_devices) || 0,
            max_users: Number(form.max_users) || 0,
            features: form.features,
            is_active: form.is_active,
        };
        if (form.id) await api.patch(`/billing/plans/${form.id}/`, payload);
        else await api.post("/billing/plans/", payload);
        await load();
        toast.success(form.id ? "Plan updated." : "Plan created.");
        setEditing(null);
    };

    const confirmDelete = async () => {
        try {
            await api.delete(`/billing/plans/${pendingDelete.id}/`);
            await load();
            toast.success("Plan deleted.");
        } catch (err) {
            // PROTECT on the subscription FK: a plan with customers on it
            // cannot be deleted, and saying so beats a bare 500.
            setError(
                apiErrorMessage(
                    err,
                    "Unable to delete this plan. Move any customers on it to another plan first."
                )
            );
        } finally {
            setPendingDelete(null);
        }
    };

    if (plans === null) return <Spinner />;

    return (
        <div>
            <PageHeader
                title="Plans"
                description="What you sell to your customers, and the ceilings that come with each."
                actions={
                    <Button icon={Plus} onClick={() => setEditing({ ...EMPTY_PLAN })}>
                        New plan
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            {plans.length === 0 ? (
                <EmptyState
                    icon={Layers}
                    title="No plans yet"
                    description="Define what you sell — a price, the monthly message allowance, and which parts of the product it opens up."
                    action={
                        <Button icon={Plus} onClick={() => setEditing({ ...EMPTY_PLAN })}>
                            New plan
                        </Button>
                    }
                />
            ) : (
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {plans.map((plan) => (
                        <Card key={plan.id} className="flex flex-col">
                            <div className="flex items-start justify-between gap-2">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <h2 className="text-sm font-bold text-ink-900 m-0">{plan.name}</h2>
                                        {!plan.is_active && <Badge tone="gray">Hidden</Badge>}
                                    </div>
                                    <p className="text-2xl font-bold text-ink-900 mt-1 mb-0 tabular-nums">
                                        {formatPrice(plan.price_cents, plan.currency)}
                                        <span className="text-xs font-medium text-ink-500 ml-1">
                                            /{plan.interval === "YEARLY" ? "year" : "month"}
                                        </span>
                                    </p>
                                </div>
                                <div className="flex gap-1 shrink-0">
                                    <Button size="xs" variant="ghost" icon={Pencil} onClick={() => setEditing(plan)}>
                                        Edit
                                    </Button>
                                    <Button
                                        size="xs"
                                        variant="ghost"
                                        icon={Trash2}
                                        onClick={() => setPendingDelete(plan)}
                                    />
                                </div>
                            </div>

                            {plan.description && (
                                <p className="text-xs text-ink-500 mt-2 mb-0">{plan.description}</p>
                            )}

                            <ul className="list-none p-0 mt-3 mb-0 space-y-1 text-xs text-ink-600">
                                {LIMIT_FIELDS.map((field) => (
                                    <li key={field.key} className="flex justify-between gap-2">
                                        <span>{field.label}</span>
                                        <span className="font-semibold text-ink-900 tabular-nums">
                                            {limitLabel(plan[field.key])}
                                        </span>
                                    </li>
                                ))}
                            </ul>

                            <div className="flex flex-wrap gap-1.5 mt-3">
                                {FEATURES.map((feature) => {
                                    // Unlisted means on: a plan written before a
                                    // switch existed keeps the feature.
                                    const on = plan.features?.[feature.key] !== false;
                                    return (
                                        <Badge key={feature.key} tone={on ? "green" : "gray"}>
                                            {on ? feature.label : `No ${feature.label.toLowerCase()}`}
                                        </Badge>
                                    );
                                })}
                            </div>

                            <p className="text-xs text-ink-400 mt-3 mb-0 pt-3 border-t border-ink-100">
                                {plan.subscriber_count} customer{plan.subscriber_count === 1 ? "" : "s"} on this plan
                            </p>
                        </Card>
                    ))}
                </div>
            )}

            {editing && <PlanModal initial={editing} onSubmit={submit} onClose={() => setEditing(null)} />}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete this plan"
                    message={
                        pendingDelete.subscriber_count > 0
                            ? `“${pendingDelete.name}” has ${pendingDelete.subscriber_count} customer(s) on it. Move them to another plan first — this will not delete.`
                            : `Delete “${pendingDelete.name}”? This cannot be undone.`
                    }
                    confirmLabel="Delete"
                    onConfirm={confirmDelete}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

function PlanModal({ initial, onSubmit, onClose }) {
    const [form, setForm] = useState({ ...EMPTY_PLAN, ...initial, features: { ...(initial.features || {}) } });
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            await onSubmit(form);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save this plan."));
        } finally {
            setSaving(false);
        }
    };

    const setFeature = (key, on) => setForm({ ...form, features: { ...form.features, [key]: on } });

    return (
        <Modal title={form.id ? "Edit plan" : "New plan"} onClose={onClose} width="lg">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Name" required>
                    <Input
                        value={form.name}
                        onChange={(event) => setForm({ ...form, name: event.target.value })}
                        placeholder="Growth"
                        autoFocus
                        required
                    />
                </Field>

                <Field label="Description" hint="Shown to you, not to the customer.">
                    <Textarea
                        rows={2}
                        value={form.description}
                        onChange={(event) => setForm({ ...form, description: event.target.value })}
                    />
                </Field>

                <div className="grid sm:grid-cols-3 gap-x-4">
                    <Field label="Price" hint="In minor units — 2900 is 29.00.">
                        <Input
                            type="number"
                            min="0"
                            value={form.price_cents}
                            onChange={(event) => setForm({ ...form, price_cents: event.target.value })}
                        />
                    </Field>
                    <Field label="Currency">
                        <Input
                            value={form.currency}
                            onChange={(event) => setForm({ ...form, currency: event.target.value.toUpperCase() })}
                            maxLength={3}
                        />
                    </Field>
                    <Field label="Billed">
                        <Select
                            value={form.interval}
                            onChange={(event) => setForm({ ...form, interval: event.target.value })}
                        >
                            <option value="MONTHLY">Monthly</option>
                            <option value="YEARLY">Yearly</option>
                        </Select>
                    </Field>
                </div>

                <p className="text-sm font-semibold text-ink-800 mt-2 mb-2">Limits</p>
                <div className="grid sm:grid-cols-2 gap-x-4">
                    {LIMIT_FIELDS.map((field) => (
                        <Field key={field.key} label={field.label} hint={field.hint}>
                            <Input
                                type="number"
                                min="0"
                                value={form[field.key]}
                                onChange={(event) => setForm({ ...form, [field.key]: event.target.value })}
                            />
                        </Field>
                    ))}
                </div>

                <p className="text-sm font-semibold text-ink-800 mt-2 mb-2">What this plan includes</p>
                <div className="space-y-3 mb-5">
                    {FEATURES.map((feature) => (
                        <Toggle
                            key={feature.key}
                            checked={form.features[feature.key] !== false}
                            onChange={(event) => setFeature(feature.key, event.target.checked)}
                            label={feature.label}
                            description={feature.hint}
                        />
                    ))}
                </div>

                <div className="mb-5">
                    <Toggle
                        checked={form.is_active}
                        onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                        label="Available"
                        description="Turn off to stop offering this plan without touching customers already on it."
                    />
                </div>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Create plan"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}
