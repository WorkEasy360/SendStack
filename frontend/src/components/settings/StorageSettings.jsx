import { useEffect, useState } from "react";
import { Database, Save, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../ui/Alert";
import Button from "../ui/Button";
import Card, { CardHeader } from "../ui/Card";
import ConfirmDialog from "../ui/ConfirmDialog";
import { Field, Input, Select } from "../ui/Field";
import { useToast } from "../ui/Toast";

/**
 * What this workspace is storing, and the one thing it may clean up.
 *
 * The cleanup covers **finished campaigns and the message rows under them**,
 * and nothing else — not contacts, credits, payments, consent evidence, audit
 * records, inbound messages, or any campaign still running. That is not a
 * promise made by a checkbox on this screen; it is that `tenants.storage` has
 * no rule naming any of them.
 *
 * The horizon is previewed before it is applied, because "delete everything
 * older than 30 days" is not a sentence anybody can evaluate without knowing
 * what it covers.
 */

const HORIZONS = [
    { value: 0, label: "Keep everything" },
    { value: 7, label: "7 days" },
    { value: 30, label: "30 days" },
    { value: 60, label: "60 days" },
    { value: 90, label: "90 days" },
];

function formatBytes(bytes) {
    // "0 B" rather than "0 MB": a category holding nothing should read as
    // empty, not as a rounded-down megabyte.
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return `${value < 10 && unit > 1 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export default function StorageSettings() {
    const toast = useToast();
    const [usage, setUsage] = useState(null);
    const [days, setDays] = useState(0);
    const [custom, setCustom] = useState(false);
    const [preview, setPreview] = useState(null);
    const [saving, setSaving] = useState(false);
    const [cleaning, setCleaning] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [error, setError] = useState("");

    const load = async () => {
        try {
            const [storage, settings] = await Promise.all([
                api.get("/tenants/storage/"),
                api.get("/tenants/settings/"),
            ]);
            setUsage(storage.data);
            const stored = settings.data.campaign_data_retention_days;
            setDays(stored);
            setCustom(stored > 0 && !HORIZONS.some((horizon) => horizon.value === stored));
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load your storage usage."));
        }
    };

    useEffect(() => {
        load();
    }, []);

    // What the *chosen* horizon would remove, which is not necessarily the
    // stored one — somebody comparing 30 against 90 needs both answers before
    // they commit to either.
    useEffect(() => {
        if (!days) {
            setPreview(null);
            return undefined;
        }
        const timer = setTimeout(() => {
            api.get(`/tenants/storage/?days=${days}`)
                .then((response) => setPreview(response.data.preview))
                .catch(() => setPreview(null));
        }, 350);
        return () => clearTimeout(timer);
    }, [days]);

    if (!usage) {
        return (
            <Card>
                <Alert>{error}</Alert>
                {!error && <p className="m-0 text-sm text-ink-500">Loading…</p>}
            </Card>
        );
    }

    const saveHorizon = async () => {
        setSaving(true);
        setError("");
        try {
            await api.patch("/tenants/settings/", { campaign_data_retention_days: days });
            toast.success(
                days
                    ? `Finished campaigns will be removed after ${days} days.`
                    : "Nothing will be removed automatically.",
            );
            await load();
        } catch (err) {
            setError(apiErrorMessage(err, "That could not be saved."));
        } finally {
            setSaving(false);
        }
    };

    const cleanNow = async () => {
        setConfirming(false);
        setCleaning(true);
        setError("");
        try {
            const response = await api.post("/tenants/storage/", {});
            setUsage(response.data.usage);
            toast.success(response.data.detail);
        } catch (err) {
            setError(apiErrorMessage(err, "The cleanup could not be run."));
        } finally {
            setCleaning(false);
        }
    };

    return (
        <>
            <Alert>{error}</Alert>

            <Card className="mb-4">
                <CardHeader
                    title="What you are storing"
                    description="Media is measured exactly. The rest is estimated from row counts."
                />

                <div className="mb-4 flex flex-wrap items-baseline gap-x-4 gap-y-1">
                    <span className="text-2xl font-bold tabular-nums text-ink-900">
                        {formatBytes(usage.used_bytes)}
                    </span>
                    {usage.unlimited ? (
                        <span className="text-sm text-ink-500">used — no storage limit on this deployment</span>
                    ) : (
                        <span className="text-sm text-ink-500">
                            of {formatBytes(usage.limit_bytes)} · {formatBytes(usage.available_bytes)} available
                        </span>
                    )}
                </div>

                {!usage.unlimited && (
                    <div className="mb-4 h-2 overflow-hidden rounded-full bg-ink-100">
                        <div
                            className={`h-full rounded-full ${
                                usage.used_percent >= 90 ? "bg-red-500" : "bg-brand-500"
                            }`}
                            style={{ width: `${Math.min(usage.used_percent, 100)}%` }}
                        />
                    </div>
                )}

                <ul className="m-0 list-none p-0">
                    {usage.categories.map((category) => (
                        <li
                            key={category.key}
                            className="flex items-start justify-between gap-4 border-b border-ink-100 py-2.5 last:border-0"
                        >
                            <div className="min-w-0">
                                <p className="m-0 text-sm font-semibold text-ink-800">{category.label}</p>
                                <p className="mt-0.5 mb-0 text-xs text-ink-500">{category.detail}</p>
                            </div>
                            <div className="shrink-0 text-right">
                                <p className="m-0 text-sm font-semibold tabular-nums text-ink-900">
                                    {/* No "~" on nothing: an estimate of zero
                                        is not an estimate. */}
                                    {category.exact || !category.bytes ? "" : "~"}
                                    {formatBytes(category.bytes)}
                                </p>
                                <p className="mt-0.5 mb-0 text-xs tabular-nums text-ink-400">
                                    {category.count.toLocaleString()} item
                                    {category.count === 1 ? "" : "s"}
                                </p>
                            </div>
                        </li>
                    ))}
                </ul>
            </Card>

            <Card>
                <CardHeader
                    title="Clean up old campaign data"
                    description="Removes finished campaigns and their per-recipient rows. Never contacts, credits, payments, consent records, received messages or a campaign still running."
                />

                <div className="mb-3 grid gap-3 sm:grid-cols-2">
                    <Field label="Remove finished campaigns older than" className="mb-0">
                        <Select
                            value={custom ? "custom" : days}
                            onChange={(event) => {
                                if (event.target.value === "custom") {
                                    setCustom(true);
                                    return;
                                }
                                setCustom(false);
                                setDays(Number(event.target.value));
                            }}
                        >
                            {HORIZONS.map((horizon) => (
                                <option key={horizon.value} value={horizon.value}>
                                    {horizon.label}
                                </option>
                            ))}
                            <option value="custom">Custom…</option>
                        </Select>
                    </Field>
                    {custom && (
                        <Field label="Days" className="mb-0">
                            <Input
                                type="number"
                                min={1}
                                value={days || ""}
                                onChange={(event) => setDays(Number(event.target.value) || 0)}
                            />
                        </Field>
                    )}
                </div>

                {days > 0 && preview && (
                    <p className="m-0 mb-3 flex items-start gap-2 rounded-lg border border-ink-200 bg-ink-50 p-2.5 text-xs text-ink-600">
                        <Database size={14} aria-hidden="true" className="mt-0.5 shrink-0 text-ink-400" />
                        <span>
                            Right now that covers{" "}
                            <span className="font-semibold tabular-nums">
                                {preview.campaigns.toLocaleString()} campaign
                                {preview.campaigns === 1 ? "" : "s"}
                            </span>{" "}
                            and{" "}
                            <span className="font-semibold tabular-nums">
                                {preview.messages.toLocaleString()} message row
                                {preview.messages === 1 ? "" : "s"}
                            </span>
                            .
                        </span>
                    </p>
                )}

                <div className="flex flex-wrap justify-end gap-2">
                    <Button
                        type="button"
                        variant="secondary"
                        icon={Trash2}
                        loading={cleaning}
                        disabled={!usage.cleanable?.enabled}
                        onClick={() => setConfirming(true)}
                    >
                        Clean up now
                    </Button>
                    <Button type="button" icon={Save} loading={saving} onClick={saveHorizon}>
                        Save
                    </Button>
                </div>

                {!usage.cleanable?.enabled && (
                    <p className="mt-2 mb-0 text-right text-xs text-ink-500">
                        Save a horizon above before running a cleanup.
                    </p>
                )}
            </Card>

            {confirming && (
                <ConfirmDialog
                    title="Clean up campaign data"
                    message={
                        `This removes ${(usage.cleanable.campaigns || 0).toLocaleString()} finished campaign` +
                        `${usage.cleanable.campaigns === 1 ? "" : "s"} older than ${usage.cleanable.days} days, ` +
                        `and the ${(usage.cleanable.messages || 0).toLocaleString()} per-recipient row` +
                        `${usage.cleanable.messages === 1 ? "" : "s"} behind their reports. ` +
                        "Contacts, credits, payments and received messages are not affected. This cannot be undone."
                    }
                    confirmLabel="Remove them"
                    onConfirm={cleanNow}
                    onClose={() => setConfirming(false)}
                />
            )}
        </>
    );
}
