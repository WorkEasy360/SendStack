import { useCallback, useEffect, useState } from "react";
import {
    KeyRound,
    Phone,
    PhoneCall,
    PhoneOff,
    Settings2,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge, { StatusBadge } from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import StatCard from "../../components/ui/StatCard";
import Tabs from "../../components/ui/Tabs";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * The calling agent: what it says on the phone, what it is allowed to do, and
 * what happened on every call it has made.
 *
 * Two tabs, because they are read at completely different moments. The agent
 * is configured once and rarely revisited. The call log is opened the moment
 * somebody asks "what did it actually say to my customer?" — which is why the
 * transcript, not the summary, is the thing the detail view leads with.
 */

const TABS = [
    { id: "agent", label: "Agent", icon: Settings2 },
    { id: "calls", label: "Calls", icon: PhoneCall },
];

const OUTCOME_LABEL = {
    AGENT_ENDED: "Agent ended the call",
    CALLER_ENDED: "Caller hung up",
    TRANSFERRED: "Transferred to a person",
    VOICEMAIL: "Answering machine",
    LIMIT_REACHED: "Hit a limit",
    NO_SPEECH: "Nobody spoke",
    ERROR: "Error",
};

const OUTCOME_TONE = {
    AGENT_ENDED: "green",
    CALLER_ENDED: "blue",
    TRANSFERRED: "violet",
    VOICEMAIL: "amber",
    LIMIT_REACHED: "amber",
    NO_SPEECH: "gray",
    ERROR: "red",
};

const ROLE_STYLE = {
    AGENT: { label: "Agent", className: "bg-brand-50 text-ink-800 border-brand-100" },
    CALLER: { label: "Caller", className: "bg-ink-50 text-ink-800 border-ink-200" },
    TOOL: { label: "Action", className: "bg-violet-50 text-violet-800 border-violet-100" },
    SYSTEM: { label: "System", className: "bg-amber-50 text-amber-800 border-amber-100" },
};

function duration(seconds) {
    if (!seconds) return "—";
    const minutes = Math.floor(seconds / 60);
    return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

export default function VoiceAgent() {
    const [tab, setTab] = useState("agent");
    const [readiness, setReadiness] = useState(null);
    const [placing, setPlacing] = useState(false);
    const [refreshKey, setRefreshKey] = useState(0);

    const loadReadiness = useCallback(() => {
        api.get("/voice/readiness/")
            .then((response) => setReadiness(response.data))
            .catch(() => setReadiness(null));
    }, []);

    useEffect(loadReadiness, [loadReadiness, refreshKey]);

    return (
        <div>
            <PageHeader
                title="Calling agent"
                description="Phones your contacts, holds a real conversation, and writes down what was said."
                actions={
                    <Button
                        icon={Phone}
                        onClick={() => setPlacing(true)}
                        disabled={!readiness?.ready}
                        title={readiness?.ready ? undefined : "The agent is not ready to place calls yet."}
                    >
                        Place a call
                    </Button>
                }
            />

            {readiness && !readiness.ready && (
                <Alert tone="warning" title="This agent cannot place calls yet">
                    <ul className="m-0 pl-5">
                        {readiness.blocking.map((problem) => (
                            <li key={problem}>{problem}</li>
                        ))}
                    </ul>
                </Alert>
            )}

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "agent" && <AgentTab onSaved={() => setRefreshKey((key) => key + 1)} />}
            {tab === "calls" && <CallsTab refreshKey={refreshKey} />}

            {placing && (
                <PlaceCallModal
                    onClose={() => setPlacing(false)}
                    onPlaced={() => {
                        setPlacing(false);
                        setTab("calls");
                        setRefreshKey((key) => key + 1);
                    }}
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

function AgentTab({ onSaved }) {
    const toast = useToast();
    const [config, setConfig] = useState(null);
    const [secrets, setSecrets] = useState({ account_sid: "", auth_token: "", api_key: "" });
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);
    const [clearing, setClearing] = useState("");

    useEffect(() => {
        api.get("/voice/agent/")
            .then((response) => setConfig(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your calling settings.")));
    }, []);

    const update = (patch) => setConfig((current) => ({ ...current, ...patch }));

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const {
                id, created_at, updated_at,
                has_carrier_credentials, has_api_key, account_sid_hint, inbound_webhook_url,
                ...rest
            } = config;
            const response = await api.patch("/voice/agent/", {
                ...rest,
                // A blank secret means "leave the stored one alone", so the
                // empty ones are dropped rather than sent as "".
                ...Object.fromEntries(Object.entries(secrets).filter(([, value]) => value)),
            });
            setConfig(response.data);
            setSecrets({ account_sid: "", auth_token: "", api_key: "" });
            toast.success("Calling settings saved.");
            onSaved?.();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these settings."));
        } finally {
            setSaving(false);
        }
    };

    const clear = async (target) => {
        setClearing(target);
        try {
            const response = await api.post("/voice/agent/clear-secret/", { target });
            setConfig(response.data);
            toast.success(target === "carrier" ? "Twilio credentials removed." : "API key removed.");
            onSaved?.();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to remove that."));
        } finally {
            setClearing("");
        }
    };

    if (!config) return error ? <Alert>{error}</Alert> : <Spinner />;

    return (
        <div className="max-w-3xl">
            <Alert>{error}</Alert>

            <Card className="mb-6">
                <CardHeader
                    title="Status"
                    description="Off by default. A machine speaking in your business's voice cannot be unsaid afterwards."
                    className="mb-4"
                />
                <Toggle
                    checked={config.is_enabled}
                    onChange={(event) => update({ is_enabled: event.target.checked })}
                    label="Let the agent place and answer calls"
                    description="It will not switch on until there are carrier credentials and a number to call from."
                />
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Twilio"
                    description="The account the calls are billed to, and the number they come from."
                    className="mb-4"
                />

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field
                        label="Account SID"
                        hint={
                            config.has_carrier_credentials
                                ? `Stored (${config.account_sid_hint}). Type a new one to replace it.`
                                : "From the Twilio console dashboard."
                        }
                    >
                        <Input
                            value={secrets.account_sid}
                            onChange={(event) => setSecrets({ ...secrets, account_sid: event.target.value })}
                            placeholder={config.has_carrier_credentials ? "••••••••••••" : "AC…"}
                            autoComplete="off"
                        />
                    </Field>

                    <Field label="Auth Token" hint="Never shown again once saved.">
                        <Input
                            type="password"
                            value={secrets.auth_token}
                            onChange={(event) => setSecrets({ ...secrets, auth_token: event.target.value })}
                            placeholder={config.has_carrier_credentials ? "••••••••••••" : "Your auth token"}
                            autoComplete="off"
                        />
                    </Field>
                </div>

                <Field label="Call from" hint="A voice-capable Twilio number, in international format.">
                    <Input
                        value={config.from_number}
                        onChange={(event) => update({ from_number: event.target.value })}
                        placeholder="+14155550100"
                    />
                </Field>

                {config.has_carrier_credentials && (
                    <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        icon={KeyRound}
                        loading={clearing === "carrier"}
                        onClick={() => clear("carrier")}
                    >
                        Remove Twilio credentials
                    </Button>
                )}
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Answering the phone"
                    description="Separate from placing calls — a business can be happy to dial out and not at all happy for a machine to be the first voice a customer hears."
                    className="mb-4"
                />

                <Toggle
                    checked={config.answer_inbound}
                    onChange={(event) => update({ answer_inbound: event.target.checked })}
                    label="Answer calls to this number"
                    description="Off, an incoming call is hung up quietly rather than left ringing."
                />

                {config.answer_inbound && (
                    <div className="mt-4">
                        <Field label="Greeting when somebody rings in" hint="Blank uses the outbound greeting.">
                            <Textarea
                                rows={2}
                                value={config.inbound_greeting}
                                onChange={(event) => update({ inbound_greeting: event.target.value })}
                                placeholder="Acme Cycles, how can I help?"
                            />
                        </Field>

                        <Field
                            label="Paste this into Twilio"
                            hint='On your number in the Twilio console, under "A call comes in", choose Webhook + HTTP POST.'
                            className="mb-0"
                        >
                            <Input
                                readOnly
                                value={config.inbound_webhook_url || "Set VOICE_PUBLIC_BASE_URL to see this URL."}
                                onFocus={(event) => event.target.select()}
                                className="font-mono text-xs"
                            />
                        </Field>
                    </div>
                )}
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="How it sounds"
                    description="What the person on the other end actually hears."
                    className="mb-4"
                />

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="Voice" hint="Twilio's text-to-speech voice.">
                        <Select value={config.voice} onChange={(event) => update({ voice: event.target.value })}>
                            <option value="Polly.Aditi">Aditi — Indian English / Hindi</option>
                            <option value="Polly.Kajal-Neural">Kajal (neural) — Indian English, most natural</option>
                            <option value="Polly.Raveena">Raveena — Indian English</option>
                            <option value="Polly.Joanna-Neural">Joanna (neural) — US English</option>
                            <option value="Polly.Amy-Neural">Amy (neural) — British English</option>
                            <option value="woman">Basic — woman</option>
                            <option value="man">Basic — man</option>
                        </Select>
                    </Field>

                    <Field label="Language heard" hint="What the speech recogniser expects.">
                        <Select value={config.language} onChange={(event) => update({ language: event.target.value })}>
                            <option value="en-IN">English (India)</option>
                            <option value="hi-IN">Hindi (India)</option>
                            <option value="en-US">English (United States)</option>
                            <option value="en-GB">English (United Kingdom)</option>
                        </Select>
                    </Field>
                </div>

                <Field label="Display name">
                    <Input
                        value={config.display_name}
                        onChange={(event) => update({ display_name: event.target.value })}
                        maxLength={80}
                    />
                </Field>

                <Field label="Greeting" hint="The first thing said when somebody picks up.">
                    <Textarea
                        rows={2}
                        value={config.greeting}
                        onChange={(event) => update({ greeting: event.target.value })}
                    />
                </Field>

                <Field
                    label="Words to expect"
                    hint="Product names, your company name, anything a recogniser would otherwise mangle. Comma separated."
                >
                    <Input
                        value={config.speech_hints}
                        onChange={(event) => update({ speech_hints: event.target.value })}
                        placeholder="MultiSender, Ravi, Bandra, invoice"
                    />
                </Field>

                <Field
                    label="Voicemail message"
                    hint="Left when an answering machine picks up. Blank means hang up silently — and skips the few seconds of machine detection on every call."
                >
                    <Textarea
                        rows={2}
                        value={config.voicemail_message}
                        onChange={(event) => update({ voicemail_message: event.target.value })}
                        placeholder="Hello, this is Acme. We'll try you again later."
                    />
                </Field>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="What it knows"
                    description="Everything here is sent on every call, along with the knowledge base from the AI Agent page."
                    className="mb-4"
                />

                <Field label="About the business" hint="Who you are and how the agent should come across.">
                    <Textarea
                        rows={4}
                        value={config.business_context}
                        onChange={(event) => update({ business_context: event.target.value })}
                        placeholder="We are a family-run bicycle shop in Pune. Warm, brief, never pushy."
                    />
                </Field>

                <Field
                    label="Default reason for calling"
                    hint="Used when a call does not give its own. A call with no purpose is a call nobody wants to receive."
                >
                    <Textarea
                        rows={3}
                        value={config.default_goal}
                        onChange={(event) => update({ default_goal: event.target.value })}
                        placeholder="Check whether their bicycle service is still needed, and book a slot if so."
                    />
                </Field>

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="Model">
                        <Select value={config.model} onChange={(event) => update({ model: event.target.value })}>
                            <option value="claude-opus-5">Claude Opus 5 — most capable</option>
                            <option value="claude-sonnet-5">Claude Sonnet 5 — balanced</option>
                            <option value="claude-haiku-4-5">Claude Haiku 4.5 — fastest</option>
                        </Select>
                    </Field>

                    <Field
                        label="Thinking effort"
                        hint="Lower answers faster. On a live call, silence is worse than a plainer answer."
                    >
                        <Select value={config.effort} onChange={(event) => update({ effort: event.target.value })}>
                            <option value="low">Low — quickest replies</option>
                            <option value="medium">Medium</option>
                            <option value="high">High — slowest, most careful</option>
                        </Select>
                    </Field>
                </div>

                <Field
                    label="API key"
                    hint={
                        config.has_api_key
                            ? "A key is stored. Type a new one to replace it."
                            : "Your own Anthropic key. Leave blank to use the deployment's."
                    }
                >
                    <div className="flex gap-2">
                        <Input
                            type="password"
                            value={secrets.api_key}
                            onChange={(event) => setSecrets({ ...secrets, api_key: event.target.value })}
                            placeholder={config.has_api_key ? "••••••••••••••••" : "sk-ant-…"}
                            autoComplete="off"
                        />
                        {config.has_api_key && (
                            <Button
                                type="button"
                                variant="secondary"
                                icon={KeyRound}
                                loading={clearing === "api_key"}
                                onClick={() => clear("api_key")}
                                className="shrink-0"
                            >
                                Remove
                            </Button>
                        )}
                    </div>
                </Field>

                <Toggle
                    checked={config.answer_from_knowledge_only}
                    onChange={(event) => update({ answer_from_knowledge_only: event.target.checked })}
                    label="Answer only from the knowledge base"
                    description="Off, the agent may answer from general knowledge — and may invent a price out loud, in your voice."
                />
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="What it may do"
                    description="Each of these is a real action taken while somebody is on the line."
                    className="mb-4"
                />

                <div className="space-y-4">
                    <Toggle
                        checked={config.allow_transfer}
                        onChange={(event) => update({ allow_transfer: event.target.checked })}
                        label="Transfer to a person"
                        description="The agent hands the call over when somebody asks for a human, or when it is out of its depth."
                    />

                    {config.allow_transfer && (
                        <Field label="Transfer calls to" className="mb-0">
                            <Input
                                value={config.transfer_number}
                                onChange={(event) => update({ transfer_number: event.target.value })}
                                placeholder="+919876543210"
                            />
                        </Field>
                    )}

                    <Toggle
                        checked={config.allow_whatsapp_followup}
                        onChange={(event) => update({ allow_whatsapp_followup: event.target.checked })}
                        label="Send a WhatsApp message during the call"
                        description="For anything easier read than heard — an address, a link, a reference number."
                    />

                    <Toggle
                        checked={config.allow_callback_scheduling}
                        onChange={(event) => update({ allow_callback_scheduling: event.target.checked })}
                        label="Book a callback"
                        description="When now is a bad time, the agent arranges a second call rather than pressing on."
                    />

                    <Toggle
                        checked={config.record_calls}
                        onChange={(event) => update({ record_calls: event.target.checked })}
                        label="Record calls"
                        description="Recording without telling the other person is illegal in many places. Say so in the greeting."
                    />
                </div>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="When it stops"
                    description="A call that will not end is worse than one that ends early."
                    className="mb-4"
                />

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="Exchanges per call" hint="0 for no limit.">
                        <Input
                            type="number"
                            min="0"
                            value={config.max_turns}
                            onChange={(event) => update({ max_turns: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Longest call (seconds)" hint="0 for no limit.">
                        <Input
                            type="number"
                            min="0"
                            value={config.max_call_seconds}
                            onChange={(event) => update({ max_call_seconds: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Wait for a reply (seconds)" hint="How long before it asks again.">
                        <Input
                            type="number"
                            min="1"
                            value={config.silence_timeout_seconds}
                            onChange={(event) => update({ silence_timeout_seconds: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Silences before hanging up">
                        <Input
                            type="number"
                            min="1"
                            value={config.max_silent_turns}
                            onChange={(event) => update({ max_silent_turns: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Calls per day" hint="0 means unlimited.">
                        <Input
                            type="number"
                            min="0"
                            value={config.daily_call_limit}
                            onChange={(event) => update({ daily_call_limit: Number(event.target.value) })}
                        />
                    </Field>
                </div>
            </Card>

            <div className="flex justify-end">
                <Button onClick={save} loading={saving}>
                    Save settings
                </Button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

function CallsTab({ refreshKey }) {
    const [calls, setCalls] = useState(null);
    const [summary, setSummary] = useState(null);
    const [error, setError] = useState("");
    const [selected, setSelected] = useState(null);
    const [tick, setTick] = useState(0);

    const load = useCallback(() => {
        Promise.all([api.get("/voice/calls/"), api.get("/voice/calls/summary/")])
            .then(([list, totals]) => {
                setCalls(list.data.results ?? list.data);
                setSummary(totals.data);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load calls.")));
    }, []);

    useEffect(load, [load, refreshKey, tick]);

    // A call in progress changes every few seconds and nothing pushes that to
    // the browser, so the list refreshes itself while any call is still live.
    useEffect(() => {
        const live = (calls ?? []).some((call) =>
            ["QUEUED", "RINGING", "IN_PROGRESS"].includes(call.status)
        );
        if (!live) return undefined;
        const timer = setInterval(() => setTick((value) => value + 1), 5000);
        return () => clearInterval(timer);
    }, [calls]);

    if (error) return <Alert>{error}</Alert>;
    if (!calls) return <Spinner />;

    const columns = [
        {
            key: "to_number",
            header: "Number",
            render: (call) => (
                <div>
                    <p className="font-semibold text-ink-900 m-0">{call.to_number}</p>
                    {call.contact_name && <p className="text-xs text-ink-500 m-0">{call.contact_name}</p>}
                </div>
            ),
        },
        { key: "status", header: "Status", render: (call) => <StatusBadge status={call.status} /> },
        {
            key: "outcome",
            header: "Outcome",
            hideBelow: "md",
            render: (call) =>
                call.outcome ? (
                    <Badge tone={OUTCOME_TONE[call.outcome]}>{OUTCOME_LABEL[call.outcome] ?? call.outcome}</Badge>
                ) : (
                    <span className="text-ink-400">—</span>
                ),
        },
        { key: "turns_used", header: "Exchanges", align: "right", hideBelow: "lg" },
        {
            key: "duration_seconds",
            header: "Length",
            align: "right",
            hideBelow: "sm",
            render: (call) => duration(call.duration_seconds),
        },
        {
            key: "created_at",
            header: "When",
            align: "right",
            render: (call) => new Date(call.created_at).toLocaleString(),
        },
    ];

    return (
        <div>
            {summary && (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-6">
                    <StatCard label="Calls" value={summary.calls} icon={PhoneCall} tone="brand" />
                    <StatCard label="Live now" value={summary.live} icon={Phone} tone="blue" />
                    <StatCard
                        label="Talk time"
                        value={duration(summary.talk_time_seconds)}
                        icon={PhoneCall}
                        tone="violet"
                    />
                    <StatCard
                        label="Average exchanges"
                        value={summary.average_turns}
                        hint="Per call"
                        icon={Settings2}
                    />
                </div>
            )}

            <DataTable
                columns={columns}
                rows={calls}
                onRowClick={(call) => setSelected(call.id)}
                empty={
                    <EmptyState
                        icon={PhoneOff}
                        title="No calls yet"
                        description="Place one from the button above to hear what the agent sounds like."
                    />
                }
            />

            {selected && <CallDetailModal callId={selected} onClose={() => setSelected(null)} onChanged={load} />}
        </div>
    );
}

function CallDetailModal({ callId, onClose, onChanged }) {
    const toast = useToast();
    const [call, setCall] = useState(null);
    const [error, setError] = useState("");
    const [hangingUp, setHangingUp] = useState(false);

    const load = useCallback(() => {
        api.get(`/voice/calls/${callId}/`)
            .then((response) => setCall(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load this call.")));
    }, [callId]);

    useEffect(load, [load]);

    // A live call's transcript grows while it is open.
    useEffect(() => {
        if (!call || !["QUEUED", "RINGING", "IN_PROGRESS"].includes(call.status)) return undefined;
        const timer = setInterval(load, 3000);
        return () => clearInterval(timer);
    }, [call, load]);

    const hangUp = async () => {
        setHangingUp(true);
        try {
            await api.post(`/voice/calls/${callId}/hangup/`);
            toast.success("Call ended.");
            load();
            onChanged?.();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to end this call."));
        } finally {
            setHangingUp(false);
        }
    };

    const live = call && ["QUEUED", "RINGING", "IN_PROGRESS"].includes(call.status);

    return (
        <Modal
            title={call ? `Call to ${call.to_number}` : "Call"}
            description={call?.goal || undefined}
            onClose={onClose}
            width="lg"
            footer={
                <div className="flex justify-between items-center w-full gap-3">
                    {live ? (
                        <Button variant="danger" icon={PhoneOff} onClick={hangUp} loading={hangingUp}>
                            End call
                        </Button>
                    ) : (
                        <span />
                    )}
                    <Button variant="secondary" onClick={onClose}>
                        Close
                    </Button>
                </div>
            }
        >
            <Alert>{error}</Alert>
            {!call ? (
                <Spinner />
            ) : (
                <div>
                    <div className="flex flex-wrap gap-2 mb-4">
                        <StatusBadge status={call.status} />
                        {call.outcome && (
                            <Badge tone={OUTCOME_TONE[call.outcome]}>
                                {OUTCOME_LABEL[call.outcome] ?? call.outcome}
                            </Badge>
                        )}
                        <Badge tone="gray">{call.direction === "INBOUND" ? "They called us" : "We called them"}</Badge>
                        <Badge tone="gray">{duration(call.duration_seconds)}</Badge>
                        <Badge tone="gray">{call.turns_used} exchanges</Badge>
                    </div>

                    {call.error && <Alert>{call.error}</Alert>}

                    {call.summary && (
                        <Card className="mb-4">
                            <CardHeader title="Summary" className="mb-4" />
                            <p className="text-sm text-ink-700 whitespace-pre-line m-0">{call.summary}</p>
                        </Card>
                    )}

                    {Object.keys(call.collected ?? {}).length > 0 && (
                        <Card className="mb-4">
                            <CardHeader title="What the agent captured" className="mb-4" />
                            <dl className="grid sm:grid-cols-2 gap-3 m-0">
                                {Object.entries(call.collected).map(([key, value]) => (
                                    <div key={key}>
                                        <dt className="text-xs font-semibold uppercase tracking-wide text-ink-400">
                                            {key.replace(/_/g, " ")}
                                        </dt>
                                        <dd className="text-sm text-ink-800 m-0 whitespace-pre-line">{String(value)}</dd>
                                    </div>
                                ))}
                            </dl>
                        </Card>
                    )}

                    <p className="text-sm font-semibold text-ink-700 mb-2">Transcript</p>
                    {call.turns.length === 0 ? (
                        <p className="text-sm text-ink-500">Nothing was said on this call.</p>
                    ) : (
                        <ol className="list-none p-0 m-0 space-y-2">
                            {call.turns.map((turn) => {
                                const style = ROLE_STYLE[turn.role] ?? ROLE_STYLE.SYSTEM;
                                return (
                                    <li key={turn.id} className={`rounded-lg border px-3 py-2 ${style.className}`}>
                                        <div className="flex items-baseline justify-between gap-3">
                                            <span className="text-xs font-bold uppercase tracking-wide">
                                                {style.label}
                                            </span>
                                            {turn.confidence != null && (
                                                <span className="text-xs opacity-70">
                                                    heard {Math.round(turn.confidence * 100)}%
                                                </span>
                                            )}
                                        </div>
                                        <p className="text-sm m-0 whitespace-pre-line">{turn.text}</p>
                                    </li>
                                );
                            })}
                        </ol>
                    )}

                    {call.recording_url && (
                        <p className="text-sm mt-4 mb-0">
                            <a href={`${call.recording_url}.mp3`} target="_blank" rel="noreferrer">
                                Listen to the recording
                            </a>
                        </p>
                    )}
                </div>
            )}
        </Modal>
    );
}

function PlaceCallModal({ onClose, onPlaced }) {
    const toast = useToast();
    const [number, setNumber] = useState("");
    const [goal, setGoal] = useState("");
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    const submit = async () => {
        setSaving(true);
        setError("");
        try {
            await api.post("/voice/calls/call/", { to_number: number.trim(), goal });
            toast.success("Calling now.");
            onPlaced();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to place this call."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal
            title="Place a call"
            description="The agent dials, introduces itself, and takes it from there."
            onClose={onClose}
            footer={
                <div className="flex justify-end gap-3">
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button icon={Phone} onClick={submit} loading={saving} disabled={!number.trim()}>
                        Call now
                    </Button>
                </div>
            }
        >
            <Alert>{error}</Alert>

            <Field label="Number to call" hint="International format, starting with + and the country code." required>
                <Input
                    value={number}
                    onChange={(event) => setNumber(event.target.value)}
                    placeholder="+919876543210"
                    autoFocus
                />
            </Field>

            <Field
                label="Why you are calling"
                hint="Leave blank to use the agent's default. Be specific — this is the whole brief it gets."
                className="mb-0"
            >
                <Textarea
                    rows={3}
                    value={goal}
                    onChange={(event) => setGoal(event.target.value)}
                    placeholder="Confirm their service appointment on Thursday at 4pm, and reschedule if that no longer suits."
                />
            </Field>
        </Modal>
    );
}
