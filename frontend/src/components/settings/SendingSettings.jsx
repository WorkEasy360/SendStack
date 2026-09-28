import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../ui/Alert";
import Button from "../ui/Button";
import Card, { CardHeader } from "../ui/Card";
import { Field, Input, Select, Toggle } from "../ui/Field";
import { useToast } from "../ui/Toast";

/**
 * How fast this workspace sends, and what it does when a send fails.
 *
 * Every field here is read by the worker, not merely stored: the delays feed
 * `campaigns.assignment.resolve_delay_bounds`, the batch pause and the sleep
 * are added to the cumulative countdown the dispatcher builds, and the retry
 * settings bound `messaging.tasks.send_message_task`. A setting the worker did
 * not read would be worse than no setting, because the customer would believe
 * their sending was paced when it was not.
 *
 * Blank delays mean "use the platform default", which is what every workspace
 * has until it changes something — hence the resolved values shown beneath.
 */

function NumberField({ label, hint, value, onChange, min = 0, suffix, ...props }) {
    return (
        <Field label={label} hint={hint} className="mb-0">
            <div className="flex items-center gap-2">
                <Input
                    type="number"
                    min={min}
                    value={value ?? ""}
                    onChange={(event) =>
                        onChange(event.target.value === "" ? null : Number(event.target.value))
                    }
                    {...props}
                />
                {suffix && <span className="shrink-0 text-xs text-ink-500">{suffix}</span>}
            </div>
        </Field>
    );
}

export default function SendingSettings() {
    const toast = useToast();
    const [settings, setSettings] = useState(null);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/tenants/settings/")
            .then((response) => setSettings(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your sending settings.")));
    }, []);

    if (!settings) {
        return (
            <Card>
                <Alert>{error}</Alert>
                {!error && <p className="m-0 text-sm text-ink-500">Loading…</p>}
            </Card>
        );
    }

    const patch = (changes) => setSettings({ ...settings, ...changes });

    const save = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            const response = await api.patch("/tenants/settings/", settings);
            setSettings(response.data);
            toast.success("Sending settings saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Those settings could not be saved."));
        } finally {
            setSaving(false);
        }
    };

    const fixed = settings.delay_mode === "FIXED";

    return (
        <form onSubmit={save}>
            <Alert>{error}</Alert>

            <Card className="mb-4">
                <CardHeader
                    title="Delay between messages"
                    description="A gap is drawn fresh for every single message, never once per campaign."
                />
                <div className="mb-4 grid gap-3 sm:grid-cols-3">
                    <Field label="Mode" className="mb-0">
                        <Select
                            value={settings.delay_mode}
                            onChange={(event) => patch({ delay_mode: event.target.value })}
                        >
                            <option value="RANDOM">Random within a window</option>
                            <option value="FIXED">The same every time</option>
                        </Select>
                    </Field>
                    <NumberField
                        label={fixed ? "Delay" : "Shortest gap"}
                        suffix="sec"
                        step="0.1"
                        value={settings.min_delay_seconds}
                        onChange={(min_delay_seconds) => patch({ min_delay_seconds })}
                    />
                    {!fixed && (
                        <NumberField
                            label="Longest gap"
                            suffix="sec"
                            step="0.1"
                            value={settings.max_delay_seconds}
                            onChange={(max_delay_seconds) => patch({ max_delay_seconds })}
                        />
                    )}
                </div>
                <p className="m-0 text-xs text-ink-500">
                    Leave blank to use the platform default. Campaigns currently send with a gap of{" "}
                    <span className="font-semibold tabular-nums">
                        {settings.effective_min_delay_seconds}s–{settings.effective_max_delay_seconds}s
                    </span>
                    . A campaign can still set its own.
                </p>
            </Card>

            <Card className="mb-4">
                <CardHeader
                    title="Batch sending"
                    description="A short, regular pause every so many messages. 0 sends continuously."
                />
                <div className="grid gap-3 sm:grid-cols-2">
                    <NumberField
                        label="Messages per batch"
                        suffix="msgs"
                        value={settings.batch_size}
                        onChange={(batch_size) => patch({ batch_size })}
                    />
                    <NumberField
                        label="Pause after each batch"
                        suffix="sec"
                        value={settings.batch_pause_seconds}
                        onChange={(batch_pause_seconds) => patch({ batch_pause_seconds })}
                    />
                </div>
            </Card>

            <Card className="mb-4">
                <CardHeader
                    title="Sleep mode"
                    description="An occasional long, random break. A predictable send pattern is what looks automated."
                />
                <Toggle
                    label="Take long breaks"
                    description="Separate from batch pauses — running both is the point."
                    checked={settings.sleep_enabled}
                    onChange={(event) => patch({ sleep_enabled: event.target.checked })}
                />
                {settings.sleep_enabled && (
                    <div className="mt-4 grid gap-3 border-t border-ink-100 pt-4 sm:grid-cols-3">
                        <NumberField
                            label="Sleep after"
                            suffix="msgs"
                            min={1}
                            value={settings.sleep_after_messages}
                            onChange={(sleep_after_messages) => patch({ sleep_after_messages })}
                        />
                        <NumberField
                            label="Shortest break"
                            suffix="sec"
                            value={settings.sleep_min_seconds}
                            onChange={(sleep_min_seconds) => patch({ sleep_min_seconds })}
                        />
                        <NumberField
                            label="Longest break"
                            suffix="sec"
                            value={settings.sleep_max_seconds}
                            onChange={(sleep_max_seconds) => patch({ sleep_max_seconds })}
                        />
                    </div>
                )}
            </Card>

            <Card className="mb-4">
                <CardHeader
                    title="Retries"
                    description="What happens when a message fails to go out."
                />
                <Toggle
                    label="Retry failed messages"
                    description="A send whose delivery could not be confirmed is never retried, whatever this says — it may already be on the recipient's phone."
                    checked={settings.retry_enabled}
                    onChange={(event) => patch({ retry_enabled: event.target.checked })}
                />
                {settings.retry_enabled && (
                    <div className="mt-4 grid gap-3 border-t border-ink-100 pt-4 sm:grid-cols-2">
                        <NumberField
                            label="Maximum retries"
                            hint="At most 3."
                            max={3}
                            value={settings.max_retries}
                            onChange={(max_retries) => patch({ max_retries })}
                        />
                        <NumberField
                            label="Wait before the first retry"
                            suffix="sec"
                            hint="Later retries back off from this."
                            min={1}
                            value={settings.retry_delay_seconds}
                            onChange={(retry_delay_seconds) => patch({ retry_delay_seconds })}
                        />
                    </div>
                )}
            </Card>

            <div className="flex justify-end">
                <Button type="submit" icon={Save} loading={saving}>
                    Save sending settings
                </Button>
            </div>
        </form>
    );
}
