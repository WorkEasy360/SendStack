import { useEffect, useState } from "react";
import { Activity, BookOpen, Bot, Gauge, KeyRound, Pencil, Plus, Search, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import {
    AI_PROVIDERS,
    BASE_URL_PLACEHOLDER,
    FREEFORM_PROVIDERS,
    KEY_HINT,
    KEY_PLACEHOLDER,
    MODEL_SUGGESTIONS,
    baseUrlProblem,
    modelForProvider,
    modelNameProblem,
    modelOptions,
} from "../../constants/aiProviders";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import { FeatureLocked } from "../../components/billing/FeatureLock";
import { useEntitlements } from "../../context/EntitlementsContext";
import Spinner from "../../components/ui/Spinner";
import Tabs from "../../components/ui/Tabs";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * The AI agent: what it is allowed to say, what it may say it from, and what
 * it has cost.
 *
 * Split into tabs because they are read at different times. Status answers "is
 * it working?" and so comes first and is where somebody lands; the agent's
 * behaviour is set up once and rarely revisited; the knowledge base is edited
 * whenever the business changes; usage is checked when somebody asks what the
 * bill is or why the agent went quiet.
 */

const TABS = [
    // Status first: the question people arrive with is "is it working?", and
    // it is the one question the settings form cannot answer -- a perfectly
    // filled-in form still answers nobody if every document failed to parse or
    // the number is disconnected.
    { id: "status", label: "Status", icon: Activity },
    { id: "agent", label: "Agent", icon: Bot },
    { id: "knowledge", label: "Knowledge base", icon: BookOpen },
    { id: "usage", label: "Usage", icon: Gauge },
];

const OUTCOME_LABEL = {
    ANSWERED: "Answered",
    HANDOFF: "Handed to a person",
    SUGGESTED: "Suggested to an agent",
    SUMMARISED: "Summarised",
    ERROR: "Error",
};

const OUTCOME_TONE = {
    ANSWERED: "green",
    HANDOFF: "blue",
    SUGGESTED: "violet",
    SUMMARISED: "gray",
    ERROR: "red",
};

export default function AIAgent() {
    const [tab, setTab] = useState("status");
    const { hasFeature } = useEntitlements();

    // Shown, not hidden. Somebody who cannot see that the agent exists will
    // never ask for it -- and the backend refuses every AI call regardless of
    // what this renders, so hiding it would buy no safety either.
    if (!hasFeature("ai_agent")) {
        return (
            <div>
                <PageHeader
                    title="AI agent"
                    description="Answers customers from the knowledge you give it, and hands over to a person when it cannot."
                />
                <FeatureLocked
                    title="AI Agent"
                    description="Let the assistant answer customers from your own documents, draft replies for your team, and summarise long threads."
                />
            </div>
        );
    }

    return (
        <div>
            <PageHeader
                title="AI agent"
                description="Answers customers from the knowledge you give it, and hands over to a person when it cannot."
            />

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "status" && <StatusTab onOpenTab={setTab} />}
            {tab === "agent" && <AgentTab />}
            {tab === "knowledge" && <KnowledgeTab />}
            {tab === "usage" && <UsageTab />}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

const ACCOUNT_TONE = {
    CONNECTED: "green",
    CONNECTING: "amber",
    QR_READY: "amber",
    DISCONNECTED: "gray",
    DISCONNECTING: "gray",
    EXPIRED: "gray",
    ERROR: "red",
};

/** One labelled number with an optional note under it. */
function Stat({ label, value, note, tone }) {
    return (
        <div>
            <p className="text-xs font-semibold text-ink-500 m-0">{label}</p>
            <p className={`text-2xl font-bold mt-1 mb-0 tabular-nums ${tone === "red" ? "text-red-600" : "text-ink-900"}`}>
                {value}
            </p>
            {note && <p className="text-xs text-ink-400 mt-1 mb-0">{note}</p>}
        </div>
    );
}

function StatusTab({ onOpenTab }) {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/ai/status/")
            .then((response) => setData(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the agent's status.")));
    }, []);

    if (error) return <Alert>{error}</Alert>;
    if (!data) return <Spinner />;

    const { agent, knowledge, whatsapp, automation, usage } = data;
    // A number nobody can be answered on, or a knowledge base nothing can be
    // answered from, is as broken as a missing key — so the headline verdict
    // is the conjunction, not just the agent's own switch.
    const answering = agent.is_ready && whatsapp.connected > 0 && knowledge.ready > 0;

    return (
        <div className="space-y-6">
            <Alert tone={answering ? "success" : agent.is_enabled ? "warning" : "info"}>
                {answering ? (
                    <>
                        The assistant is answering customers on {whatsapp.connected}{" "}
                        {whatsapp.connected === 1 ? "number" : "numbers"}, using <strong>{agent.model}</strong>{" "}
                        and {knowledge.ready} {knowledge.ready === 1 ? "document" : "documents"}.
                    </>
                ) : (
                    <>
                        {agent.problem || "The assistant is not answering anyone yet."}
                        {agent.is_ready && whatsapp.connected === 0 && " No WhatsApp number is connected."}
                        {agent.is_ready && whatsapp.connected > 0 && knowledge.ready === 0 &&
                            " There are no readable documents to answer from."}
                    </>
                )}
            </Alert>

            <Card>
                <CardHeader
                    title="AI agent"
                    description="What the agent will use for the next customer who messages."
                    className="mb-4"
                />
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                    <Stat
                        label="Status"
                        value={agent.status === "CONNECTED" ? "Connected" : agent.status === "OFF" ? "Off" : "Error"}
                        tone={agent.status === "ERROR" ? "red" : undefined}
                    />
                    <Stat label="Provider" value={agent.provider} />
                    <Stat label="Model" value={agent.model || "—"} />
                    <Stat
                        label="API key"
                        value={agent.api_key_source === "tenant" ? "Yours" : agent.api_key_source === "environment" ? "Server" : "None"}
                        note={agent.api_key_source === "environment" ? "From the server's environment" : ""}
                        tone={agent.api_key_source === "none" ? "red" : undefined}
                    />
                </div>
            </Card>

            <Card>
                <CardHeader
                    title="Knowledge base"
                    description={
                        knowledge.retrieval_mode === "hybrid"
                            ? `Retrieval is hybrid — keywords blended with meaning (${knowledge.embeddings.model}).`
                            : "Retrieval is by keyword. Turn on AI_EMBEDDINGS_ENABLED to blend in semantic search."
                    }
                    className="mb-4"
                />
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                    <Stat label="Ready" value={knowledge.ready} note={`${knowledge.chunks} searchable pieces`} />
                    <Stat label="Processing" value={knowledge.pending + knowledge.processing} />
                    <Stat label="Failed" value={knowledge.failed} tone={knowledge.failed ? "red" : undefined} />
                    <Stat
                        label="Embedded"
                        value={knowledge.embeddings.enabled ? knowledge.embeddings.embedded : "Off"}
                    />
                </div>

                {knowledge.failed_documents.length > 0 && (
                    <ul className="list-none p-0 mt-4 mb-0 space-y-1">
                        {knowledge.failed_documents.map((document) => (
                            <li key={document.id} className="text-xs text-red-600">
                                <strong>{document.original_filename || document.title}</strong> — {document.error}
                            </li>
                        ))}
                    </ul>
                )}

                <Button variant="secondary" className="mt-4" onClick={() => onOpenTab("knowledge")}>
                    Manage documents
                </Button>
            </Card>

            <Card padded={false}>
                <div className="p-5">
                    <CardHeader
                        title="WhatsApp numbers"
                        description="Every connected number is answered by the agent. Connecting a new one needs no setup here."
                        className="mb-0"
                    />
                </div>
                {whatsapp.accounts.length === 0 ? (
                    <p className="px-5 pb-5 mt-0 text-sm text-ink-500">No WhatsApp numbers are connected yet.</p>
                ) : (
                    <ul className="list-none p-0 m-0 divide-y divide-ink-100 border-t border-ink-100">
                        {whatsapp.accounts.map((account) => (
                            <li key={account.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
                                <span className="text-sm font-medium text-ink-900">{account.name}</span>
                                <span className="text-xs text-ink-500 tabular-nums">{account.phone_number || "—"}</span>
                                <Badge tone={ACCOUNT_TONE[account.status] || "gray"}>{account.status}</Badge>
                                <span className="text-xs text-ink-400">{account.connection_method}</span>
                                {account.status === "CONNECTED" && (
                                    <span className="text-xs text-green-700 ml-auto">AI enabled</span>
                                )}
                            </li>
                        ))}
                    </ul>
                )}
            </Card>

            <Card>
                <CardHeader
                    title="Automation"
                    description="Rules run before the agent does. The first thing that answers a message ends it, so a message a rule claimed never reaches the agent."
                    className="mb-4"
                />
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                    <Stat label="Active rules" value={automation.rules} />
                    <Stat label="Workflows" value={automation.workflows} />
                    <Stat label="Follow-ups" value={automation.follow_ups} />
                    <Stat label="Agent runs" value="Last" note="After every rule above" />
                </div>
            </Card>

            <Card>
                <CardHeader
                    title="Recent activity"
                    description="What the agent has actually done."
                    className="mb-4"
                />
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                    <Stat label="Calls (24h)" value={usage.calls_last_24h} note={`${usage.calls} all time`} />
                    <Stat label="Input tokens" value={usage.input_tokens.toLocaleString()} />
                    <Stat label="Output tokens" value={usage.output_tokens.toLocaleString()} />
                    <Stat
                        label="Errors"
                        value={usage.by_outcome?.ERROR || 0}
                        tone={usage.by_outcome?.ERROR ? "red" : undefined}
                    />
                </div>

                {usage.last_answer && (
                    <div className="mt-4 pt-4 border-t border-ink-100">
                        <p className="text-xs font-semibold text-ink-500 m-0">Last customer answer</p>
                        <p className="text-sm text-ink-700 mt-1 mb-0">“{usage.last_answer.question}”</p>
                        <p className="text-sm text-ink-900 mt-1 mb-0">{usage.last_answer.answer || "— handed over —"}</p>
                    </div>
                )}

                {usage.recent_errors.length > 0 && (
                    <ul className="list-none p-0 mt-4 mb-0 space-y-1">
                        {usage.recent_errors.map((entry, index) => (
                            <li key={index} className="text-xs text-red-600">
                                {new Date(entry.created_at).toLocaleString()} — {entry.error}
                            </li>
                        ))}
                    </ul>
                )}
            </Card>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

function AgentTab() {
    const toast = useToast();
    const [config, setConfig] = useState(null);
    const [bases, setBases] = useState([]);
    const [apiKey, setApiKey] = useState("");
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);
    const [clearingKey, setClearingKey] = useState(false);

    useEffect(() => {
        api.get("/ai/agent/")
            .then((response) => setConfig(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your AI settings.")));
        api.get("/ai/knowledge-bases/")
            .then((response) => setBases(response.data.results || []))
            .catch(() => setBases([]));
    }, []);

    const update = (patch) => setConfig((current) => ({ ...current, ...patch }));

    // Changing provider takes the model with it. The vendors' model names are
    // not interchangeable, so leaving the old one behind would put the form in
    // a state that cannot be saved and looks like it should be. For a provider
    // the server can name a model for, the box is left blank instead — see
    // modelForProvider.
    const switchProvider = (provider) => update({ provider, model: modelForProvider(provider) });

    const modelProblem = config ? modelNameProblem(config.provider, config.model) : "";
    const urlProblem = config ? baseUrlProblem(config.provider, config.base_url) : "";

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const response = await api.patch("/ai/agent/", {
                is_enabled: config.is_enabled,
                display_name: config.display_name,
                provider: config.provider,
                model: config.model,
                base_url: config.base_url,
                business_context: config.business_context,
                temperature: config.temperature,
                max_output_tokens: config.max_output_tokens,
                history_messages: config.history_messages,
                knowledge_chunks: config.knowledge_chunks,
                answer_from_knowledge_only: config.answer_from_knowledge_only,
                knowledge_only_message: config.knowledge_only_message,
                reply_when_assigned: config.reply_when_assigned,
                max_replies_per_conversation: config.max_replies_per_conversation,
                handoff_enabled: config.handoff_enabled,
                handoff_message: config.handoff_message,
                knowledge_base: config.knowledge_base || null,
                reply_scope: config.reply_scope,
                fallback_action: config.fallback_action,
                min_relevance: config.min_relevance,
                crm_logging_enabled: config.crm_logging_enabled,
                crm_tagging_enabled: config.crm_tagging_enabled,
                // Blank means "leave the stored key alone" -- the API treats it
                // that way, so sending it unconditionally is safe.
                ...(apiKey ? { api_key: apiKey } : {}),
            });
            setConfig(response.data);
            setApiKey("");
            toast.success("AI settings saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these settings."));
        } finally {
            setSaving(false);
        }
    };

    const clearKey = async () => {
        setClearingKey(true);
        try {
            const response = await api.post("/ai/agent/clear-key/");
            setConfig(response.data);
            toast.success("API key removed.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to remove the key."));
        } finally {
            setClearingKey(false);
        }
    };

    if (!config) return error ? <Alert>{error}</Alert> : <Spinner />;

    return (
        <div className="max-w-3xl">
            <Alert>{error}</Alert>

            {config.is_enabled && !config.is_ready && (
                <Alert tone="warning" title="The agent is on but has nothing to answer with">
                    Add an API key below — until then the agent stays silent rather than replying badly, and every
                    attempt is logged under Usage as an error.
                </Alert>
            )}

            <Card className="mb-6">
                <CardHeader
                    title="Status"
                    description="Off by default. An agent that answers confidently from nothing is worse than no agent at all."
                    className="mb-4"
                />
                <Toggle
                    checked={config.is_enabled}
                    onChange={(event) => update({ is_enabled: event.target.checked })}
                    label="Let the agent reply on its own"
                    description="It answers in the inbox and at an AI reply step in a workflow. Suggestions and summaries work either way."
                />
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Who it is"
                    description="How the agent introduces itself and what it knows about you."
                    className="mb-4"
                />

                <Field label="Display name">
                    <Input
                        value={config.display_name}
                        onChange={(event) => update({ display_name: event.target.value })}
                        maxLength={80}
                    />
                </Field>

                <Field
                    label="About the business"
                    hint="Who you are, what you sell, and how the agent should sound. Sent with every request."
                >
                    <Textarea
                        rows={5}
                        value={config.business_context}
                        onChange={(event) => update({ business_context: event.target.value })}
                        placeholder="We are a family-run bakery in Pune. Warm and brief. Never quote a price that isn't in the knowledge base."
                    />
                </Field>

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field
                        label="Provider"
                        hint="Who bills you and whose key this is."
                    >
                        <Select
                            value={config.provider}
                            onChange={(event) => switchProvider(event.target.value)}
                        >
                            {AI_PROVIDERS.map((option) => (
                                <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                        </Select>
                    </Field>

                    <Field label="Model" error={modelProblem}>
                        {FREEFORM_PROVIDERS.includes(config.provider) ? (
                            <>
                                {/* Free text, not a list: neither OpenRouter nor a custom
                                    endpoint's own models are a list this codebase can keep
                                    current, and the API takes any name. */}
                                <Input
                                    list="ai-agent-models"
                                    value={config.model}
                                    onChange={(event) => update({ model: event.target.value })}
                                    placeholder={
                                        config.provider === "CUSTOM" ? "gpt-4o, llama3.3, deepseek-chat…" : "anthropic/claude-sonnet-4.5"
                                    }
                                    invalid={Boolean(modelProblem)}
                                    autoComplete="off"
                                    spellCheck={false}
                                />
                                <datalist id="ai-agent-models">
                                    {(MODEL_SUGGESTIONS[config.provider] || []).map((option) => (
                                        <option key={option.value} value={option.value}>{option.label}</option>
                                    ))}
                                </datalist>
                            </>
                        ) : (
                            <Select value={config.model} onChange={(event) => update({ model: event.target.value })}>
                                {modelOptions(config.provider, config.model).map((option) => (
                                    <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                            </Select>
                        )}
                    </Field>
                </div>

                {config.provider === "CUSTOM" && (
                    <Field
                        label="Base URL"
                        error={urlProblem}
                        hint="The OpenAI-compatible endpoint to send requests to — everything up to (not including) /chat/completions."
                    >
                        <Input
                            value={config.base_url || ""}
                            onChange={(event) => update({ base_url: event.target.value })}
                            placeholder={BASE_URL_PLACEHOLDER}
                            invalid={Boolean(urlProblem)}
                            autoComplete="off"
                            spellCheck={false}
                        />
                    </Field>
                )}

                <Field
                    label="API key"
                    hint={
                        config.has_api_key
                            ? "A key is stored. Type a new one to replace it — it is never shown again."
                            : `${KEY_HINT[config.provider] || ""} Leave blank to use the one this deployment was set up with.`
                    }
                >
                    <div className="flex gap-2">
                        <Input
                            type="password"
                            value={apiKey}
                            onChange={(event) => setApiKey(event.target.value)}
                            placeholder={
                                config.has_api_key
                                    ? "••••••••••••••••"
                                    : KEY_PLACEHOLDER[config.provider] || ""
                            }
                            autoComplete="off"
                        />
                        {config.has_api_key && (
                            <Button
                                type="button"
                                variant="secondary"
                                icon={KeyRound}
                                onClick={clearKey}
                                loading={clearingKey}
                                className="shrink-0"
                            >
                                Remove
                            </Button>
                        )}
                    </div>
                    <p className="text-xs text-ink-400 mt-2 mb-0">
                        {config.is_ready ? (
                            <Badge tone="green">Ready to answer</Badge>
                        ) : (
                            <Badge tone="amber">No key — the agent cannot answer</Badge>
                        )}
                    </p>
                </Field>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="How careful it is"
                    description="The defaults are deliberately timid. Loosen them knowingly."
                    className="mb-4"
                />

                {/* The three questions that actually decide how this behaves,
                    kept together and above everything technical. */}
                <Field
                    label="Which knowledge base should it answer from?"
                    hint="Only documents in this base are ever used. Leave it on every base if you only have one."
                >
                    <Select
                        value={config.knowledge_base || ""}
                        onChange={(event) =>
                            update({ knowledge_base: event.target.value ? Number(event.target.value) : null })
                        }
                    >
                        <option value="">Every knowledge base</option>
                        {bases.map((base) => (
                            <option key={base.id} value={base.id}>
                                {base.name}
                                {base.document_count ? ` (${base.document_count} documents)` : " (empty)"}
                            </option>
                        ))}
                    </Select>
                </Field>

                <Field label="When should it reply?">
                    <Select
                        value={config.reply_scope || "UNASSIGNED"}
                        onChange={(event) => update({ reply_scope: event.target.value })}
                    >
                        <option value="UNASSIGNED">Only when nobody on your team has the chat</option>
                        <option value="EVERY">Every incoming message</option>
                        <option value="OUTSIDE_HOURS">Only outside your business hours</option>
                    </Select>
                </Field>

                <Field
                    label="If the answer isn't in your documents"
                    hint="It never guesses. This is what the customer gets instead."
                >
                    <Select
                        value={config.fallback_action || "HANDOFF"}
                        onChange={(event) => update({ fallback_action: event.target.value })}
                    >
                        <option value="HANDOFF">Hand the chat to a person</option>
                        <option value="SAY_UNKNOWN">Say we don't have that information</option>
                        <option value="ASK_SUPPORT">Ask them to contact support</option>
                        <option value="NOTIFY_TEAM">Tell them we'll get back to them, and notify the team</option>
                    </Select>
                </Field>

                <div className="mb-4">
                    <Toggle
                        checked={config.knowledge_only_enforced || config.answer_from_knowledge_only}
                        onChange={(event) => update({ answer_from_knowledge_only: event.target.checked })}
                        disabled={config.knowledge_only_enforced}
                        label="Answer only from the knowledge base"
                        description={
                            config.knowledge_only_enforced
                                ? "Switched on for this whole installation and cannot be turned off here. Questions your documents do not cover are refused rather than guessed at."
                                : "Off, the agent may answer from general knowledge — and may invent a price or a policy in your voice."
                        }
                    />
                </div>

                {/* Only worth showing when it is the sentence customers will
                    actually receive -- outside knowledge-only mode the
                    fallback above is what they get. */}
                {(config.knowledge_only_enforced || config.answer_from_knowledge_only) && (
                    <Field
                        label="What to say when the answer isn't in the knowledge base"
                        hint="Keep it plain. A refusal that hedges reads as a soft yes, and invites the next question rather than answering this one."
                    >
                        <Input
                            maxLength={500}
                            value={config.knowledge_only_message ?? ""}
                            onChange={(event) => update({ knowledge_only_message: event.target.value })}
                            placeholder="I couldn't find this information in the Knowledge Base."
                        />
                    </Field>
                )}

                <div className="mb-4">
                    <Toggle
                        checked={config.reply_when_assigned}
                        onChange={(event) => update({ reply_when_assigned: event.target.checked })}
                        label="Reply even when a person has taken the chat"
                        description="Off, the agent stays out of the way once a team member owns the conversation."
                    />
                </div>

                <div className="mb-4">
                    <Toggle
                        checked={config.crm_logging_enabled}
                        onChange={(event) => update({ crm_logging_enabled: event.target.checked })}
                        label="Record what customers ask about"
                        description="Adds a line to the customer's timeline — pricing enquiry, refund enquiry, and so on."
                    />
                </div>

                <div className="mb-4">
                    <Toggle
                        checked={config.crm_tagging_enabled}
                        onChange={(event) => update({ crm_tagging_enabled: event.target.checked })}
                        label="Tag customers by what they asked about"
                        description="Off by default: a tag changes how somebody is segmented and campaigned to, which should be your decision."
                    />
                </div>

                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="Replies per conversation" hint="0 for no limit. Reached, the chat goes to a person.">
                        <Input
                            type="number"
                            min="0"
                            value={config.max_replies_per_conversation}
                            onChange={(event) => update({ max_replies_per_conversation: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Creativity" hint="0 sticks to the source. 1 is loose. 0.2 suits support.">
                        <Input
                            type="number"
                            min="0"
                            max="1"
                            step="0.1"
                            value={config.temperature}
                            onChange={(event) => update({ temperature: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Messages of history it reads" hint="Enough to follow the thread, bounded to keep replies cheap.">
                        <Input
                            type="number"
                            min="0"
                            value={config.history_messages}
                            onChange={(event) => update({ history_messages: Number(event.target.value) })}
                        />
                    </Field>

                    <Field label="Knowledge pieces per answer" hint="How much of the knowledge base each answer may draw on.">
                        <Input
                            type="number"
                            min="1"
                            value={config.knowledge_chunks}
                            onChange={(event) => update({ knowledge_chunks: Number(event.target.value) })}
                        />
                    </Field>

                    <Field
                        label="How close a match it needs"
                        hint="0 to 1. Below this it uses your fallback instead of answering — and costs nothing, because no request is made."
                    >
                        <Input
                            type="number"
                            min="0"
                            max="1"
                            step="0.05"
                            value={config.min_relevance ?? 0.15}
                            onChange={(event) => update({ min_relevance: Number(event.target.value) })}
                        />
                    </Field>
                </div>

                <Field label="Longest reply (tokens)" hint="Roughly 400 tokens is a short paragraph.">
                    <Input
                        type="number"
                        min="1"
                        value={config.max_output_tokens}
                        onChange={(event) => update({ max_output_tokens: Number(event.target.value) })}
                    />
                </Field>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Handing over"
                    description="What happens when the agent cannot help."
                    className="mb-4"
                />

                <div className="mb-4">
                    <Toggle
                        checked={config.handoff_enabled}
                        onChange={(event) => update({ handoff_enabled: event.target.checked })}
                        label="Hand over to a person when unsure"
                        description="Off, the agent keeps trying — and a customer who needs a human never gets one."
                    />
                </div>

                {config.handoff_enabled && (
                    <Field label="What the customer is told" required>
                        <Textarea
                            rows={2}
                            value={config.handoff_message}
                            onChange={(event) => update({ handoff_message: event.target.value })}
                        />
                    </Field>
                )}
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
// Knowledge base
// ---------------------------------------------------------------------------

function KnowledgeTab() {
    const toast = useToast();
    const [documents, setDocuments] = useState(null);
    const [error, setError] = useState("");
    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);

    const reload = async () => {
        try {
            const response = await api.get("/ai/knowledge/");
            setDocuments(response.data.results || []);
            setError("");
        } catch (err) {
            setDocuments([]);
            setError(apiErrorMessage(err, "Unable to load the knowledge base."));
        }
    };

    useEffect(() => {
        reload();
    }, []);

    const submit = async (form) => {
        const payload = {
            title: form.title,
            content: form.content,
            source_url: form.source_url,
            is_active: form.is_active,
        };
        if (form.id) await api.patch(`/ai/knowledge/${form.id}/`, payload);
        else await api.post("/ai/knowledge/", payload);
        await reload();
        toast.success(form.id ? "Document updated." : "Document added.");
        setEditing(null);
    };

    const confirmDelete = async () => {
        try {
            await api.delete(`/ai/knowledge/${pendingDelete.id}/`);
            await reload();
            toast.success("Document deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this document."));
        } finally {
            setPendingDelete(null);
        }
    };

    if (documents === null) return <Spinner />;

    return (
        <div>
            <Alert tone="info" title="What the agent is allowed to say">
                Everything here is searched for each question, and the closest pieces are handed to the agent. With
                &ldquo;answer only from the knowledge base&rdquo; on, anything not written here is something the agent
                will refuse to answer — which is the point.
            </Alert>

            <Alert>{error}</Alert>

            <div className="flex justify-between items-center gap-3 mb-4">
                <p className="text-sm text-ink-500 m-0">
                    A document is split into searchable pieces when you save it, so a correction takes effect on the
                    next message.
                </p>
                <Button
                    icon={Plus}
                    className="shrink-0"
                    onClick={() => setEditing({ title: "", content: "", source_url: "", is_active: true })}
                >
                    Add document
                </Button>
            </div>

            {documents.length === 0 ? (
                <EmptyState
                    icon={BookOpen}
                    title="No knowledge yet"
                    description="Paste in a FAQ, a price list, or your delivery and refund policy. The agent answers from these and nothing else."
                    action={
                        <Button
                            icon={Plus}
                            onClick={() => setEditing({ title: "", content: "", source_url: "", is_active: true })}
                        >
                            Add document
                        </Button>
                    }
                />
            ) : (
                <div className="space-y-3 mb-6">
                    {documents.map((document) => (
                        <Card key={document.id}>
                            <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <h3 className="text-sm font-bold text-ink-900 m-0">{document.title}</h3>
                                        {document.is_active ? (
                                            <Badge tone="green">In use</Badge>
                                        ) : (
                                            <Badge tone="gray">Ignored</Badge>
                                        )}
                                        <span className="text-xs text-ink-400">
                                            {document.chunk_count} piece{document.chunk_count === 1 ? "" : "s"}
                                        </span>
                                    </div>
                                    <p className="text-sm text-ink-500 mt-1 mb-0 line-clamp-2">{document.content}</p>
                                    {document.source_url && (
                                        <p className="text-xs text-ink-400 mt-1 mb-0 truncate">{document.source_url}</p>
                                    )}
                                </div>
                                <div className="flex gap-1 shrink-0">
                                    <Button size="xs" variant="ghost" icon={Pencil} onClick={() => setEditing(document)}>
                                        Edit
                                    </Button>
                                    <Button
                                        size="xs"
                                        variant="ghost"
                                        icon={Trash2}
                                        onClick={() => setPendingDelete(document)}
                                    >
                                        Delete
                                    </Button>
                                </div>
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            <KnowledgeSearch />

            {editing && <DocumentModal initial={editing} onSubmit={submit} onClose={() => setEditing(null)} />}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete this document"
                    message={`Delete “${pendingDelete.title}”? The agent stops being able to answer from it straight away.`}
                    confirmLabel="Delete"
                    onConfirm={confirmDelete}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

function DocumentModal({ initial, onSubmit, onClose }) {
    const [form, setForm] = useState(initial);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            await onSubmit(form);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save this document."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title={form.id ? "Edit document" : "Add document"} onClose={onClose} width="lg">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Title" required>
                    <Input
                        value={form.title}
                        onChange={(event) => setForm({ ...form, title: event.target.value })}
                        placeholder="Delivery and returns"
                        autoFocus
                        required
                    />
                </Field>

                <Field
                    label="Content"
                    required
                    hint="Plain language, the way you would explain it to a customer. Short sections beat one long wall."
                >
                    <Textarea
                        rows={14}
                        value={form.content}
                        onChange={(event) => setForm({ ...form, content: event.target.value })}
                        required
                    />
                </Field>

                <Field label="Source link" hint="Optional. Where this came from, so you can check it later.">
                    <Input
                        type="url"
                        value={form.source_url}
                        onChange={(event) => setForm({ ...form, source_url: event.target.value })}
                        placeholder="https://example.com/faq"
                    />
                </Field>

                <div className="mb-5">
                    <Toggle
                        checked={form.is_active}
                        onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                        label="In use"
                        description="Turn off to keep the document without the agent answering from it."
                    />
                </div>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Add document"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

/**
 * What the agent would retrieve for a question.
 *
 * The way to find out why it answered as it did without messaging a customer
 * to find out.
 */
function KnowledgeSearch() {
    const [query, setQuery] = useState("");
    const [results, setResults] = useState(null);
    const [searching, setSearching] = useState(false);
    const [error, setError] = useState("");

    const run = async (event) => {
        event.preventDefault();
        if (!query.trim()) return;
        setSearching(true);
        setError("");
        try {
            const response = await api.post("/ai/knowledge/search/", { query, limit: 5 });
            setResults(response.data.results || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to search the knowledge base."));
        } finally {
            setSearching(false);
        }
    };

    return (
        <Card>
            <CardHeader
                title="Try a question"
                description="Shows the pieces the agent would be given. Nothing is sent to anybody."
                className="mb-4"
            />

            <form onSubmit={run} className="flex gap-2">
                <Input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Do you deliver on Sundays?"
                />
                <Button type="submit" variant="secondary" icon={Search} loading={searching} className="shrink-0">
                    Search
                </Button>
            </form>

            <Alert className="mb-0 mt-3">{error}</Alert>

            {results !== null && !error && (
                <div className="mt-3">
                    {results.length === 0 ? (
                        <p className="text-sm text-ink-500 m-0">
                            Nothing matched. With &ldquo;answer only from the knowledge base&rdquo; on, the agent would
                            hand this question to a person.
                        </p>
                    ) : (
                        <ul className="list-none p-0 m-0 space-y-2">
                            {results.map((result, index) => (
                                <li
                                    key={`${result.document_id}-${index}`}
                                    className="rounded-lg border border-ink-200 bg-ink-50 px-3 py-2"
                                >
                                    <p className="text-xs font-semibold text-ink-700 m-0 mb-1">
                                        {result.document_title}
                                    </p>
                                    <p className="text-sm text-ink-600 m-0">{result.content}</p>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}
        </Card>
    );
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

function UsageTab() {
    const [summary, setSummary] = useState(null);
    const [logs, setLogs] = useState([]);
    const [error, setError] = useState("");

    useEffect(() => {
        Promise.all([api.get("/ai/usage/summary/"), api.get("/ai/usage/")])
            .then(([summaryRes, logsRes]) => {
                setSummary(summaryRes.data);
                setLogs(logsRes.data.results || []);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load AI usage.")));
    }, []);

    if (error) return <Alert>{error}</Alert>;
    if (!summary) return <Spinner />;

    const stats = [
        { label: "Calls", value: summary.calls },
        { label: "Input tokens", value: summary.input_tokens.toLocaleString() },
        { label: "Output tokens", value: summary.output_tokens.toLocaleString() },
        { label: "Errors", value: summary.by_outcome?.ERROR || 0 },
    ];

    return (
        <div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                {stats.map((stat) => (
                    <Card key={stat.label}>
                        <p className="text-xs font-semibold text-ink-500 m-0">{stat.label}</p>
                        <p className="text-2xl font-bold text-ink-900 mt-1 mb-0 tabular-nums">{stat.value}</p>
                    </Card>
                ))}
            </div>

            {logs.length === 0 ? (
                <EmptyState
                    icon={Gauge}
                    title="Nothing yet"
                    description="Every call the agent makes is recorded here — answers, handovers, suggestions, and the failures — so a quiet agent is explainable."
                />
            ) : (
                <Card padded={false}>
                    <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                        {logs.map((log) => (
                            <li key={log.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
                                <Badge tone={OUTCOME_TONE[log.outcome] || "gray"}>
                                    {OUTCOME_LABEL[log.outcome] || log.outcome}
                                </Badge>
                                <span className="text-xs text-ink-500">{log.model}</span>
                                <span className="text-xs text-ink-400 tabular-nums">
                                    {log.input_tokens.toLocaleString()} in / {log.output_tokens.toLocaleString()} out
                                </span>
                                {log.latency_ms > 0 && (
                                    <span className="text-xs text-ink-400 tabular-nums">{log.latency_ms} ms</span>
                                )}
                                {log.error && <span className="text-xs text-red-600 truncate">{log.error}</span>}
                                <span className="text-xs text-ink-400 ml-auto tabular-nums">
                                    {new Date(log.created_at).toLocaleString()}
                                </span>
                            </li>
                        ))}
                    </ul>
                </Card>
            )}
        </div>
    );
}
