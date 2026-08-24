import { useEffect, useState } from "react";
import api, { apiErrorMessage } from "../services/api";
import Alert from "./ui/Alert";
import Button from "./ui/Button";
import Modal from "./ui/Modal";
import { Field, Input, Toggle } from "./ui/Field";
import { useToast } from "./ui/Toast";

/**
 * Per-number send limits.
 *
 * Blank means "use the platform default" rather than "no limit" -- the same
 * convention the campaign delay fields use, and the reason both inputs are
 * left empty rather than pre-filled with the effective number: pre-filling
 * would silently turn an inherited default into a fixed override the first
 * time somebody opened this and pressed Save.
 */
export default function SendingLimitsModal({ account, health, onClose, onSaved }) {
    const toast = useToast();
    const [form, setForm] = useState(null);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        let cancelled = false;
        api.get(`/whatsapp/accounts/${account.id}/sending-policy/`)
            .then((response) => {
                if (cancelled) return;
                const policy = response.data;
                setForm({
                    daily_limit: policy.daily_limit ?? "",
                    hourly_limit: policy.hourly_limit ?? "",
                    warmup_enabled: policy.warmup_enabled,
                    auto_pause_enabled: policy.auto_pause_enabled,
                });
            })
            .catch((err) => !cancelled && setError(apiErrorMessage(err, "Unable to load this number's limits.")));
        return () => {
            cancelled = true;
        };
    }, [account.id]);

    const update = (patch) => setForm((current) => ({ ...current, ...patch }));

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            await api.patch(`/whatsapp/accounts/${account.id}/sending-policy/`, {
                daily_limit: form.daily_limit === "" ? null : Number(form.daily_limit),
                hourly_limit: form.hourly_limit === "" ? null : Number(form.hourly_limit),
                warmup_enabled: form.warmup_enabled,
                auto_pause_enabled: form.auto_pause_enabled,
            });
            toast.success("Send limits updated.");
            onSaved?.();
            onClose();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these limits."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal
            title={`Send limits — ${account.name}`}
            description="How much this number is allowed to send. Messages over the limit are held and sent when the window reopens, never dropped."
            onClose={onClose}
            footer={
                <>
                    <Button variant="secondary" onClick={onClose} disabled={saving}>
                        Cancel
                    </Button>
                    <Button onClick={save} loading={saving} disabled={!form}>
                        Save limits
                    </Button>
                </>
            }
        >
            <Alert tone="error">{error}</Alert>

            {form && (
                <>
                    <div className="grid gap-3 sm:grid-cols-2">
                        <Field label="Messages per day" hint="Leave blank to use the platform default.">
                            <Input
                                type="number"
                                min="1"
                                value={form.daily_limit}
                                placeholder={health?.daily_limit ?? "No limit"}
                                onChange={(event) => update({ daily_limit: event.target.value })}
                            />
                        </Field>
                        <Field label="Messages per hour" hint="Leave blank to use the platform default.">
                            <Input
                                type="number"
                                min="1"
                                value={form.hourly_limit}
                                placeholder={health?.hourly_limit ?? "No limit"}
                                onChange={(event) => update({ hourly_limit: event.target.value })}
                            />
                        </Field>
                    </div>

                    <div className="mt-2 pt-4 border-t border-ink-100 space-y-4">
                        <Toggle
                            label="Warm this number up"
                            description="A newly connected number sends a little on its first day and more each day after, which is what a real number looks like. Numbers with sending history are already past this."
                            checked={form.warmup_enabled}
                            onChange={(event) => update({ warmup_enabled: event.target.checked })}
                        />
                        <Toggle
                            label="Pause automatically when sends start failing"
                            description="Stops for an hour if an unusual share of messages fail, rather than pushing a number WhatsApp has already begun refusing."
                            checked={form.auto_pause_enabled}
                            onChange={(event) => update({ auto_pause_enabled: event.target.checked })}
                        />
                    </div>

                    {health?.warmup_active && (
                        <p className="text-xs text-ink-500 mt-4 mb-0">
                            This number is on day {health.warmup_day} of {health.warmup_days_total} of its warmup, so
                            today it can send {health.daily_limit}. A limit you set below that still applies; one above
                            it takes effect once the warmup finishes.
                        </p>
                    )}
                </>
            )}
        </Modal>
    );
}
