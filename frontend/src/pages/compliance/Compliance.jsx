import { useEffect, useMemo, useState } from "react";
import { Clock, MessageSquareOff, Plus, ScrollText, ShieldCheck, X } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { CardGridSkeleton } from "../../components/ui/Skeleton";
import { useToast } from "../../components/ui/Toast";

/**
 * Consent and sending-hours rules for the whole workspace.
 *
 * Deliberately one page rather than a tab inside Settings: these are the
 * rules that decide whether a message is allowed to leave at all, and they
 * are the ones a customer will be asked to account for. Burying them next to
 * the sidebar-collapse preference would say something untrue about how much
 * they matter.
 */

const SOURCE_LABEL = {
    INBOUND_KEYWORD: "Replied to stop",
    INBOUND_BUTTON: "Tapped unsubscribe",
    AGENT: "Team member",
    IMPORT: "Import",
    API: "API",
    SYSTEM: "System",
};

function Section({ icon: Icon, title, description, children }) {
    return (
        <section className="mb-6">
            <div className="flex items-start gap-3 mb-3">
                <span className="shrink-0 w-8 h-8 rounded-lg bg-ink-100 text-ink-500 flex items-center justify-center">
                    <Icon size={16} aria-hidden="true" />
                </span>
                <div>
                    <h2 className="text-sm font-bold text-ink-900 m-0">{title}</h2>
                    {description && <p className="text-xs text-ink-500 mt-0.5 mb-0">{description}</p>}
                </div>
            </div>
            {children}
        </section>
    );
}

/** The tenant's own stop/start phrases, on top of the built-in list. */
function KeywordEditor({ label, hint, value, builtIn, onChange }) {
    const [draft, setDraft] = useState("");

    const add = () => {
        const phrase = draft.trim();
        if (!phrase) return;
        if (!value.some((existing) => existing.toLowerCase() === phrase.toLowerCase())) {
            onChange([...value, phrase]);
        }
        setDraft("");
    };

    return (
        <Field label={label} hint={hint}>
            <div className="flex gap-2">
                <Input
                    value={draft}
                    placeholder="e.g. cancelar"
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                        if (event.key === "Enter") {
                            event.preventDefault();
                            add();
                        }
                    }}
                />
                <Button type="button" variant="secondary" icon={Plus} onClick={add}>
                    Add
                </Button>
            </div>

            {value.length > 0 && (
                <ul className="list-none p-0 m-0 mt-2 flex flex-wrap gap-1.5">
                    {value.map((phrase) => (
                        <li key={phrase}>
                            <span className="inline-flex items-center gap-1 rounded-md bg-ink-100 pl-2 pr-1 py-0.5 text-xs font-medium text-ink-700">
                                {phrase}
                                <button
                                    type="button"
                                    onClick={() => onChange(value.filter((item) => item !== phrase))}
                                    className="cursor-pointer border-none bg-transparent p-0.5 text-ink-400 hover:text-red-600"
                                    aria-label={`Remove ${phrase}`}
                                >
                                    <X size={12} aria-hidden="true" />
                                </button>
                            </span>
                        </li>
                    ))}
                </ul>
            )}

            <p className="text-xs text-ink-400 mt-2 mb-0">
                Recognised already, in {builtIn.length} phrases across a dozen languages:{" "}
                {builtIn.slice(0, 6).join(", ")}…
            </p>
        </Field>
    );
}

/** Shows what the server would actually do with a given message. */
function KeywordTester() {
    const [text, setText] = useState("");
    const [result, setResult] = useState(null);
    const [checking, setChecking] = useState(false);

    const check = async () => {
        setChecking(true);
        try {
            const response = await api.post("/compliance/keyword-preview/", { text });
            setResult(response.data);
        } catch {
            setResult(null);
        } finally {
            setChecking(false);
        }
    };

    return (
        <div className="mt-5 pt-5 border-t border-ink-100">
            <Field
                label="Try a message"
                hint="Checked by the server, so this is exactly what a real reply would do."
            >
                <div className="flex gap-2">
                    <Input
                        value={text}
                        placeholder="please stop"
                        onChange={(event) => setText(event.target.value)}
                        onKeyDown={(event) => {
                            if (event.key === "Enter") {
                                event.preventDefault();
                                check();
                            }
                        }}
                    />
                    <Button type="button" variant="secondary" onClick={check} loading={checking} disabled={!text.trim()}>
                        Check
                    </Button>
                </div>
            </Field>

            {result && (
                <p className="text-sm m-0">
                    {result.stops ? (
                        <Badge tone="red">Unsubscribes this contact</Badge>
                    ) : result.starts ? (
                        <Badge tone="green">Resubscribes this contact</Badge>
                    ) : (
                        <Badge tone="gray">Treated as a normal message</Badge>
                    )}
                </p>
            )}
        </div>
    );
}

function ConsentLog() {
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        api.get("/compliance/consent-events/")
            .then((response) => setEvents(response.data.results || []))
            .catch(() => setEvents([]))
            .finally(() => setLoading(false));
    }, []);

    if (loading) return <CardGridSkeleton count={1} />;

    if (events.length === 0) {
        return (
            <Card>
                <p className="text-sm text-ink-500 m-0">
                    Nothing recorded yet. Every unsubscribe and resubscribe will be listed here with who did it and
                    what they said.
                </p>
            </Card>
        );
    }

    return (
        <Card padded={false}>
            <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                {events.map((event) => (
                    <li key={event.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
                        <Badge tone={event.event === "OPT_OUT" ? "red" : "green"}>
                            {event.event === "OPT_OUT" ? "Opted out" : "Opted in"}
                        </Badge>
                        <span className="text-sm font-semibold text-ink-900">
                            {event.contact_name || event.contact_phone}
                        </span>
                        <span className="text-xs text-ink-500">{SOURCE_LABEL[event.source] || event.source}</span>
                        {event.detail && <span className="text-xs text-ink-400 truncate">{event.detail}</span>}
                        <span className="text-xs text-ink-400 ml-auto tabular-nums">
                            {new Date(event.created_at).toLocaleString()}
                        </span>
                    </li>
                ))}
            </ul>
        </Card>
    );
}

export default function Compliance() {
    const toast = useToast();
    const [policy, setPolicy] = useState(null);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        api.get("/compliance/policy/")
            .then((response) => setPolicy(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your compliance settings.")));
    }, []);

    const update = (patch) => setPolicy((current) => ({ ...current, ...patch }));

    const timezones = useMemo(() => policy?.available_timezones || ["UTC"], [policy]);

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const response = await api.patch("/compliance/policy/", {
                stop_keywords_enabled: policy.stop_keywords_enabled,
                extra_stop_keywords: policy.extra_stop_keywords,
                extra_start_keywords: policy.extra_start_keywords,
                send_opt_out_confirmation: policy.send_opt_out_confirmation,
                opt_out_confirmation: policy.opt_out_confirmation,
                send_opt_in_confirmation: policy.send_opt_in_confirmation,
                opt_in_confirmation: policy.opt_in_confirmation,
                opt_out_style: policy.opt_out_style,
                opt_out_button_title: policy.opt_out_button_title,
                opt_out_footer: policy.opt_out_footer,
                quiet_hours_enabled: policy.quiet_hours_enabled,
                quiet_hours_start: policy.quiet_hours_start,
                quiet_hours_end: policy.quiet_hours_end,
                timezone: policy.timezone,
                use_contact_timezone: policy.use_contact_timezone,
            });
            setPolicy(response.data);
            toast.success("Compliance settings saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these settings."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="max-w-3xl">
            <PageHeader
                title="Compliance"
                description="How people opt out, and when your workspace is allowed to message them."
                action={
                    <Button onClick={save} loading={saving} disabled={!policy}>
                        Save changes
                    </Button>
                }
            />

            <Alert tone="error">{error}</Alert>

            {!policy ? (
                <CardGridSkeleton count={2} />
            ) : (
                <>
                    <Section
                        icon={MessageSquareOff}
                        title="Opting out"
                        description="Anyone who asks to stop hearing from you is removed from every campaign immediately, including ones already running."
                    >
                        <Card>
                            <Toggle
                                label="Unsubscribe on a stop word"
                                description="Recognises STOP, UNSUBSCRIBE and their equivalents in several languages. The unsubscribe button is always honoured, whether or not this is on."
                                checked={policy.stop_keywords_enabled}
                                onChange={(event) => update({ stop_keywords_enabled: event.target.checked })}
                            />

                            {policy.stop_keywords_enabled && (
                                <div className="mt-5 pt-5 border-t border-ink-100">
                                    <KeywordEditor
                                        label="Your own stop words"
                                        hint="Words specific to your audience. Only whole, short messages count, so ordinary conversation is never mistaken for an opt-out."
                                        value={policy.extra_stop_keywords || []}
                                        builtIn={policy.built_in_stop_keywords || []}
                                        onChange={(extra_stop_keywords) => update({ extra_stop_keywords })}
                                    />
                                    <KeywordEditor
                                        label="Your own resubscribe words"
                                        hint="Only ever acted on for a contact who has already opted out."
                                        value={policy.extra_start_keywords || []}
                                        builtIn={policy.built_in_start_keywords || []}
                                        onChange={(extra_start_keywords) => update({ extra_start_keywords })}
                                    />
                                    <KeywordTester />
                                </div>
                            )}
                        </Card>

                        <Card className="mt-4">
                            <CardHeader
                                title="How campaigns offer the way out"
                                description="Applied to every campaign with the unsubscribe option switched on."
                            />

                            <Field
                                label="Style"
                                hint="A line of text renders on every phone. A button is easier to tap and is honoured exactly, but turns the message into an interactive one, which QR-connected numbers cannot guarantee will render."
                            >
                                <Select
                                    value={policy.opt_out_style}
                                    onChange={(event) => update({ opt_out_style: event.target.value })}
                                >
                                    <option value="FOOTER">A "reply STOP" line</option>
                                    <option value="BUTTON">A tappable unsubscribe button</option>
                                </Select>
                            </Field>

                            {policy.opt_out_style === "BUTTON" ? (
                                <Field
                                    label="Button label"
                                    hint="Falls back to the line below whenever a button will not fit — three buttons already used, a link button, or a connection that cannot send them."
                                >
                                    <Input
                                        maxLength={20}
                                        value={policy.opt_out_button_title}
                                        onChange={(event) => update({ opt_out_button_title: event.target.value })}
                                    />
                                </Field>
                            ) : null}

                            <Field label="Opt-out line" hint="Added to the end of the message.">
                                <Input
                                    maxLength={200}
                                    value={policy.opt_out_footer}
                                    onChange={(event) => update({ opt_out_footer: event.target.value })}
                                />
                            </Field>
                        </Card>

                        <Card className="mt-4">
                            <Toggle
                                label="Confirm an unsubscribe"
                                description="One message back, so the person knows it worked. Sent even though they have just unsubscribed — it is the acknowledgement they asked for."
                                checked={policy.send_opt_out_confirmation}
                                onChange={(event) => update({ send_opt_out_confirmation: event.target.checked })}
                            />
                            {policy.send_opt_out_confirmation && (
                                <div className="mt-4">
                                    <Textarea
                                        rows={3}
                                        value={policy.opt_out_confirmation}
                                        onChange={(event) => update({ opt_out_confirmation: event.target.value })}
                                    />
                                </div>
                            )}

                            <div className="mt-5 pt-5 border-t border-ink-100">
                                <Toggle
                                    label="Confirm a resubscribe"
                                    checked={policy.send_opt_in_confirmation}
                                    onChange={(event) => update({ send_opt_in_confirmation: event.target.checked })}
                                />
                                {policy.send_opt_in_confirmation && (
                                    <div className="mt-4">
                                        <Textarea
                                            rows={2}
                                            value={policy.opt_in_confirmation}
                                            onChange={(event) => update({ opt_in_confirmation: event.target.value })}
                                        />
                                    </div>
                                )}
                            </div>
                        </Card>
                    </Section>

                    <Section
                        icon={Clock}
                        title="Quiet hours"
                        description="Marketing that lands in the middle of the night is what gets a number reported. Messages inside the window are held and sent when it opens, never dropped."
                    >
                        <Card>
                            <Toggle
                                label="Hold campaigns outside sending hours"
                                description="Replies typed by your team in the inbox are never held — someone waiting on an answer should get it."
                                checked={policy.quiet_hours_enabled}
                                onChange={(event) => update({ quiet_hours_enabled: event.target.checked })}
                            />

                            {policy.quiet_hours_enabled && (
                                <div className="mt-5 pt-5 border-t border-ink-100">
                                    <div className="grid gap-3 sm:grid-cols-2">
                                        <Field label="Quiet from">
                                            <Input
                                                type="time"
                                                value={(policy.quiet_hours_start || "").slice(0, 5)}
                                                onChange={(event) => update({ quiet_hours_start: event.target.value })}
                                            />
                                        </Field>
                                        <Field label="Quiet until">
                                            <Input
                                                type="time"
                                                value={(policy.quiet_hours_end || "").slice(0, 5)}
                                                onChange={(event) => update({ quiet_hours_end: event.target.value })}
                                            />
                                        </Field>
                                    </div>

                                    <Field label="Timezone" hint="Used when a contact's own timezone is unknown.">
                                        <Select
                                            value={policy.timezone}
                                            onChange={(event) => update({ timezone: event.target.value })}
                                        >
                                            {timezones.map((zone) => (
                                                <option key={zone} value={zone}>
                                                    {zone}
                                                </option>
                                            ))}
                                        </Select>
                                    </Field>

                                    <Toggle
                                        label="Use each contact's own timezone"
                                        description="Where the contact's country is known, quiet hours follow their local clock rather than yours."
                                        checked={policy.use_contact_timezone}
                                        onChange={(event) => update({ use_contact_timezone: event.target.checked })}
                                    />
                                </div>
                            )}
                        </Card>
                    </Section>

                    <Section
                        icon={ScrollText}
                        title="Consent history"
                        description="Every opt-out and opt-in, with who decided it and on what evidence."
                    >
                        <ConsentLog />
                    </Section>

                    <div className="flex justify-end gap-2 pb-2">
                        <Button onClick={save} loading={saving} icon={ShieldCheck}>
                            Save changes
                        </Button>
                    </div>
                </>
            )}
        </div>
    );
}
