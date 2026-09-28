import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
    Bot,
    Clock,
    GitBranch,
    ListChecks,
    Pause,
    Copy,
    Pencil,
    Play,
    Plus,
    RotateCcw,
    Smartphone,
    Sparkles,
    Trash2,
    Upload,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge, { StatusBadge } from "../../components/ui/Badge";
import Button, { IconButton } from "../../components/ui/Button";
import Card from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import Spinner from "../../components/ui/Spinner";
import { CheckboxField, Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * Creating, configuring and publishing chatbots.
 *
 * The form is deliberately long and deliberately one screen: every field on it
 * changes what a customer is told, and splitting it across a wizard would hide
 * the fallback message — the one most people forget and most need.
 */

const BOT_TYPES = [
    {
        id: "HYBRID",
        label: "Hybrid — rules first, then AI",
        hint: "Recommended. Exact answers where you wrote one, the AI for everything else.",
    },
    { id: "RULE_BASED", label: "Rules only", hint: "Answers only what you have written down. Never calls a model." },
    { id: "AI_BASED", label: "AI only", hint: "Every message goes to the AI agent and your knowledge base." },
];

const LANGUAGES = [
    { id: "AUTO", label: "Match the customer" },
    { id: "ENGLISH", label: "English" },
    { id: "HINDI", label: "Hindi" },
    { id: "HINGLISH", label: "Hinglish" },
];

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const HANDOFF_CONDITIONS = [
    { key: "customer_request", label: "The customer asks for a person", type: "bool" },
    { key: "repeated_fallback", label: "The bot cannot answer this many times in a row", type: "number" },
    { key: "negative_sentiment", label: "The customer sounds unhappy", type: "bool" },
    { key: "ai_unavailable", label: "The AI provider is unreachable", type: "bool" },
    { key: "max_bot_replies", label: "The bot has sent this many replies (0 = no limit)", type: "number" },
    { key: "keywords", label: "The message contains any of these words", type: "text" },
];

function blankBot(accountId) {
    return {
        name: "",
        description: "",
        whatsapp_account: accountId || "",
        bot_type: "HYBRID",
        welcome_message: "Hi! Thanks for getting in touch. How can I help?",
        fallback_message: "Sorry, I didn't quite get that. Could you rephrase it, or type 'agent' to reach a person?",
        unsupported_message: "Sorry, I currently understand text messages best. Please type your question.",
        outside_hours_message:
            "Thanks for contacting us. Our team is currently offline. We will respond during business hours.",
        ai_enabled: true,
        knowledge_base: "",
        system_prompt: "",
        persona: "",
        language: "AUTO",
        max_ai_response_chars: 600,
        history_messages: 10,
        min_relevance: 0.15,
        response_delay_min_seconds: 0,
        response_delay_max_seconds: 0,
        human_handoff_enabled: true,
        handoff_message: "Let me get a colleague to help with that — someone will be with you shortly.",
        handoff_conditions: {},
        working_hours_enabled: false,
        conversation_timeout_minutes: 1440,
        defer_to_automation: false,
    };
}

export default function ChatbotBots({ bots, accounts, onChanged, onOpenTab, onSelectBot, can }) {
    const toast = useToast();
    const [editing, setEditing] = useState(null);
    const [confirm, setConfirm] = useState(null);
    const [busyId, setBusyId] = useState(null);
    const [knowledgeBases, setKnowledgeBases] = useState([]);

    useEffect(() => {
        api.get("/ai/knowledge-bases/", { params: { page_size: 100 } })
            .then((response) => setKnowledgeBases(response.data.results ?? response.data ?? []))
            .catch(() => setKnowledgeBases([]));
    }, []);

    const [templatePicker, setTemplatePicker] = useState(false);

    const act = async (bot, path, label) => {
        setBusyId(bot.id);
        try {
            await api.post(`/chatbot/bots/${bot.id}/${path}/`);
            toast.success(label);
            onChanged();
        } catch (error) {
            toast.error(apiErrorMessage(error, `Could not ${label.toLowerCase()}.`));
        } finally {
            setBusyId(null);
        }
    };

    /**
     * Copy a bot's configuration into a new draft.
     *
     * The copy is made on the server -- rules, hours and the draft flow all
     * come with it, and none of the conversations do. Doing it here, by reading
     * and re-posting, would copy only the fields this screen happens to know
     * about, and would quietly stop copying anything added later.
     */
    const duplicate = async (bot) => {
        setBusyId(bot.id);
        try {
            const response = await api.post(`/chatbot/bots/${bot.id}/duplicate/`);
            toast.success(`Created "${response.data.name}".`);
            onChanged();
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not duplicate this bot."));
        } finally {
            setBusyId(null);
        }
    };

    const remove = async (bot) => {
        try {
            await api.delete(`/chatbot/bots/${bot.id}/`);
            toast.success(`"${bot.name}" deleted.`);
            onChanged();
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not delete that chatbot."));
        }
    };

    const connected = accounts.filter((account) => account.status === "CONNECTED");

    // A number answers with one bot, so the numbers that already have a live
    // one are the numbers a new bot cannot go live on. Worth knowing while
    // choosing rather than at Publish: with a single device it never comes up,
    // but a workspace running four numbers would otherwise fill in a long form
    // and only then be told to go and pause something.
    const takenAccountIds = new Set(
        bots.filter((bot) => bot.status === "PUBLISHED").map((bot) => bot.whatsapp_account),
    );
    const liveBotNameFor = (accountId) =>
        bots.find((bot) => bot.status === "PUBLISHED" && bot.whatsapp_account === accountId)?.name;
    // Open the form on a number that can actually go live, falling back to the
    // first connected one so the field is never empty when there is any choice
    // at all.
    const suggestedAccountId =
        connected.find((account) => !takenAccountIds.has(account.id))?.id ?? connected[0]?.id;

    // Why there is no "Create chatbot" button matters more than the fact that
    // there isn't one: an empty state that says "attach a bot" and then offers
    // nothing to click reads as a broken page rather than as a permission.
    const emptyStateReason = !can("CHATBOT_CREATE")
        ? "Your role can work chatbot conversations but not set them up. Someone with the Manager role on the Team page can create one — or give you that role."
        : connected.length
          ? "Attach a bot to one of your connected numbers and it will start answering the moment you publish it."
          : "Connect a WhatsApp number under Devices first — a chatbot needs a number to answer on.";

    if (!bots.length) {
        return (
            <>
                <EmptyState
                    icon={Bot}
                    title="No chatbots yet"
                    description={emptyStateReason}
                    action={
                        can("CHATBOT_CREATE") && (
                            <Button
                                icon={Plus}
                                disabled={!accounts.length}
                                onClick={() => setEditing(blankBot(suggestedAccountId))}
                            >
                                Create chatbot
                            </Button>
                        )
                    }
                />
                {templatePicker && (
                <TemplatePicker
                    bots={bots}
                    onClose={() => setTemplatePicker(false)}
                    onApplied={() => {
                        setTemplatePicker(false);
                        onChanged();
                        onOpenTab?.("flows");
                    }}
                />
            )}

            {editing && (
                    <BotForm
                        value={editing}
                        accounts={accounts}
                        liveBotNameFor={liveBotNameFor}
                        knowledgeBases={knowledgeBases}
                        onClose={() => setEditing(null)}
                        onSaved={() => {
                            setEditing(null);
                            onChanged();
                        }}
                    />
                )}
            </>
        );
    }

    return (
        <div>
            <div className="flex justify-end gap-2 mb-4">
                {can("CHATBOT_EDIT") && (
                    <Button
                        variant="secondary"
                        icon={Sparkles}
                        disabled={!bots.length}
                        title={bots.length ? "Start from a ready-made flow" : "Create a chatbot first"}
                        onClick={() => setTemplatePicker(true)}
                    >
                        Start from a template
                    </Button>
                )}
                {can("CHATBOT_CREATE") && (
                    <Button icon={Plus} disabled={!accounts.length} onClick={() => setEditing(blankBot(suggestedAccountId))}>
                        Create chatbot
                    </Button>
                )}
            </div>

            <div className="grid gap-4">
                {bots.map((bot) => (
                    <Card key={bot.id}>
                        <div className="flex flex-col lg:flex-row lg:items-start gap-4">
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 flex-wrap mb-1">
                                    <h3 className="text-base font-bold text-ink-900 m-0 truncate">{bot.name}</h3>
                                    <StatusBadge status={bot.status} />
                                    <Badge tone={bot.bot_type === "AI_BASED" ? "violet" : "blue"}>
                                        {BOT_TYPES.find((entry) => entry.id === bot.bot_type)?.label.split(" —")[0] ||
                                            bot.bot_type}
                                    </Badge>
                                    {bot.published_version_number && (
                                        <span className="text-xs text-ink-500 tabular-nums">
                                            v{bot.published_version_number}
                                        </span>
                                    )}
                                </div>

                                {bot.description && (
                                    <p className="text-sm text-ink-500 m-0 mb-2">{bot.description}</p>
                                )}

                                <div className="flex items-center gap-4 flex-wrap text-xs text-ink-500">
                                    <span className="inline-flex items-center gap-1.5">
                                        <Smartphone size={13} aria-hidden="true" />
                                        {bot.account_name}
                                        {bot.account_phone ? ` · ${bot.account_phone}` : ""}
                                        <StatusBadge status={bot.account_status} />
                                    </span>
                                    <span className="inline-flex items-center gap-1.5">
                                        <ListChecks size={13} aria-hidden="true" />
                                        {bot.rule_count} {bot.rule_count === 1 ? "rule" : "rules"}
                                    </span>
                                    {bot.knowledge_base_name && (
                                        <span className="inline-flex items-center gap-1.5">
                                            <Bot size={13} aria-hidden="true" />
                                            {bot.knowledge_base_name}
                                        </span>
                                    )}
                                    {bot.response_delay_max_seconds > 0 && (
                                        <span className="inline-flex items-center gap-1.5">
                                            <Clock size={13} aria-hidden="true" />
                                            {bot.response_delay_min_seconds}–{bot.response_delay_max_seconds}s delay
                                        </span>
                                    )}
                                </div>

                                {bot.account_status !== "CONNECTED" && (
                                    <Alert tone="warning" className="mt-3">
                                        {bot.account_name} is {bot.account_status.toLowerCase()}. This bot cannot send
                                        anything until the number is reconnected — replies are recorded as failed with
                                        the reason, not silently dropped.
                                    </Alert>
                                )}

                                {bot.has_legacy_automation && bot.status === "PUBLISHED" && !bot.defer_to_automation && (
                                    <Alert tone="info" className="mt-3">
                                        This number also has older automation rules or workflows. While this bot is
                                        published it answers instead of them. Turn on "Let existing automation answer
                                        first" if you want the old rules to keep working.
                                    </Alert>
                                )}
                            </div>

                            <div className="flex flex-wrap items-center gap-2 shrink-0">
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={ListChecks}
                                    onClick={() => {
                                        onSelectBot(bot.id);
                                        onOpenTab("rules");
                                    }}
                                >
                                    Rules
                                </Button>
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={GitBranch}
                                    onClick={() => {
                                        onSelectBot(bot.id);
                                        onOpenTab("flows");
                                    }}
                                >
                                    Flow
                                </Button>

                                {can("CHATBOT_PUBLISH") &&
                                    (bot.status === "PUBLISHED" ? (
                                        <Button
                                            size="sm"
                                            variant="secondary"
                                            icon={Pause}
                                            loading={busyId === bot.id}
                                            onClick={() => act(bot, "pause", "Paused")}
                                        >
                                            Pause
                                        </Button>
                                    ) : bot.published_version_number ? (
                                        <Button
                                            size="sm"
                                            icon={Play}
                                            loading={busyId === bot.id}
                                            onClick={() => act(bot, "resume", "Resumed")}
                                        >
                                            Resume
                                        </Button>
                                    ) : (
                                        <Button
                                            size="sm"
                                            icon={Upload}
                                            loading={busyId === bot.id}
                                            onClick={() => act(bot, "publish", "Published")}
                                        >
                                            Publish
                                        </Button>
                                    ))}

                                {can("CHATBOT_PUBLISH") && bot.published_version_number && (
                                    <Button
                                        size="sm"
                                        variant="secondary"
                                        icon={Upload}
                                        loading={busyId === bot.id}
                                        onClick={() => act(bot, "publish", "Published")}
                                        title="Freeze the current rules, flow and settings and put them live"
                                    >
                                        Publish changes
                                    </Button>
                                )}

                                {can("CHATBOT_EDIT") && (
                                    <IconButton icon={Pencil} title="Edit" onClick={() => setEditing(bot)} />
                                )}
                                {can("CHATBOT_CREATE") && (
                                    <IconButton
                                        icon={Copy}
                                        title="Duplicate this bot"
                                        disabled={busyId === bot.id}
                                        onClick={() => duplicate(bot)}
                                    />
                                )}
                                {can("CHATBOT_PUBLISH") && (
                                    <IconButton
                                        icon={RotateCcw}
                                        title="Version history"
                                        onClick={() => setConfirm({ kind: "versions", bot })}
                                    />
                                )}
                                {can("CHATBOT_DELETE") && (
                                    <IconButton
                                        icon={Trash2}
                                        title="Delete"
                                        onClick={() => setConfirm({ kind: "delete", bot })}
                                    />
                                )}
                            </div>
                        </div>
                    </Card>
                ))}
            </div>

            {editing && (
                <BotForm
                    value={editing}
                    accounts={accounts}
                    liveBotNameFor={liveBotNameFor}
                    knowledgeBases={knowledgeBases}
                    onClose={() => setEditing(null)}
                    onSaved={() => {
                        setEditing(null);
                        onChanged();
                    }}
                />
            )}

            {confirm?.kind === "delete" && (
                <ConfirmDialog
                    title={`Delete "${confirm.bot.name}"?`}
                    message="Its rules, flow and version history go with it. Conversations it has already had are kept, and stop being answered automatically."
                    confirmLabel="Delete chatbot"
                    onClose={() => setConfirm(null)}
                    onConfirm={async () => {
                        await remove(confirm.bot);
                        setConfirm(null);
                    }}
                />
            )}

            {confirm?.kind === "versions" && (
                <VersionHistory bot={confirm.bot} onClose={() => setConfirm(null)} onChanged={onChanged} />
            )}
        </div>
    );
}

function BotForm({ value, accounts, liveBotNameFor, knowledgeBases, onClose, onSaved }) {
    const toast = useToast();
    const [form, setForm] = useState({ ...value, handoff_conditions: value.handoff_conditions || {} });
    const [hours, setHours] = useState(null);
    const [saving, setSaving] = useState(false);
    const [errors, setErrors] = useState({});

    const isNew = !value.id;
    const set = (patch) => setForm((current) => ({ ...current, ...patch }));
    const setCondition = (key, next) =>
        set({ handoff_conditions: { ...form.handoff_conditions, [key]: next } });

    useEffect(() => {
        if (isNew) {
            setHours(WEEKDAYS.map((_, weekday) => ({ weekday, is_open: weekday < 5, opens_at: "09:00", closes_at: "18:00" })));
            return;
        }
        api.get(`/chatbot/bots/${value.id}/working-hours/`)
            .then((response) => {
                const rows = response.data || [];
                setHours(
                    WEEKDAYS.map((_, weekday) => {
                        const existing = rows.find((row) => row.weekday === weekday);
                        return existing
                            ? {
                                  weekday,
                                  is_open: existing.is_open,
                                  opens_at: existing.opens_at.slice(0, 5),
                                  closes_at: existing.closes_at.slice(0, 5),
                              }
                            : { weekday, is_open: false, opens_at: "09:00", closes_at: "18:00" };
                    }),
                );
            })
            .catch(() => setHours(WEEKDAYS.map((_, weekday) => ({ weekday, is_open: true, opens_at: "09:00", closes_at: "18:00" }))));
    }, [value.id, isNew]);

    const save = async () => {
        setSaving(true);
        setErrors({});
        const payload = {
            ...form,
            knowledge_base: form.knowledge_base || null,
            whatsapp_account: Number(form.whatsapp_account),
        };
        delete payload.id;

        try {
            const response = isNew
                ? await api.post("/chatbot/bots/", payload)
                : await api.patch(`/chatbot/bots/${value.id}/`, payload);

            // The hours are their own endpoint because they are a list, not a
            // field — saved after the bot so a brand-new one has an id to hang
            // them on.
            if (hours) {
                await api.put(`/chatbot/bots/${response.data.id}/working-hours/`, hours);
            }
            toast.success(isNew ? "Chatbot created. Publish it when you're ready." : "Chatbot saved.");
            onSaved();
        } catch (error) {
            const data = error?.response?.data;
            if (data && typeof data === "object") setErrors(data);
            toast.error(apiErrorMessage(error, "Could not save that chatbot."));
        } finally {
            setSaving(false);
        }
    };

    const fieldError = (name) => {
        const value = errors[name];
        return Array.isArray(value) ? value.join(" ") : value;
    };

    return (
        <Modal
            title={isNew ? "Create a chatbot" : `Edit "${value.name}"`}
            description="Changes take effect for customers when you publish."
            width="xl"
            onClose={onClose}
            footer={
                <div className="flex justify-end gap-2">
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button loading={saving} onClick={save}>
                        {isNew ? "Create chatbot" : "Save changes"}
                    </Button>
                </div>
            }
        >
            <Section title="The basics">
                <Field label="Name" required error={fieldError("name")}>
                    <Input
                        value={form.name}
                        onChange={(event) => set({ name: event.target.value })}
                        placeholder="Support Bot"
                        invalid={Boolean(fieldError("name"))}
                    />
                </Field>
                <Field label="Description" hint="For your team, never shown to customers.">
                    <Input
                        value={form.description}
                        onChange={(event) => set({ description: event.target.value })}
                        placeholder="Answers pricing and delivery questions on the support line."
                    />
                </Field>

                <Field
                    label="WhatsApp number"
                    required
                    error={fieldError("whatsapp_account")}
                    hint={
                        accounts.length > 1
                            ? "One bot answers per number. Run as many bots as you have numbers — give each one its own."
                            : undefined
                    }
                >
                    <Select
                        value={form.whatsapp_account}
                        onChange={(event) => set({ whatsapp_account: event.target.value })}
                        invalid={Boolean(fieldError("whatsapp_account"))}
                    >
                        <option value="">Choose a number…</option>
                        {accounts.map((account) => {
                            // Naming the bot that holds the number, rather than
                            // just marking it taken, is what makes the message
                            // actionable: pausing it is the next step, and you
                            // cannot pause something you cannot identify.
                            const heldBy = liveBotNameFor?.(account.id);
                            // A published bot is of course "live" on its own
                            // number, and telling it so while editing it would
                            // be nonsense. Guarded on `value.id` because a new
                            // bot has no number of its own yet — only a
                            // suggested one, which may well be taken.
                            const isSelf = Boolean(value.id) && String(account.id) === String(value.whatsapp_account);
                            return (
                                <option key={account.id} value={account.id}>
                                    {account.name}
                                    {account.phone_number ? ` · ${account.phone_number}` : ""}
                                    {account.status !== "CONNECTED" ? ` (${account.status.toLowerCase()})` : ""}
                                    {heldBy && !isSelf ? ` — "${heldBy}" is live here` : ""}
                                </option>
                            );
                        })}
                    </Select>
                </Field>

                <Field label="How it decides what to say">
                    <Select value={form.bot_type} onChange={(event) => set({ bot_type: event.target.value })}>
                        {BOT_TYPES.map((entry) => (
                            <option key={entry.id} value={entry.id}>
                                {entry.label}
                            </option>
                        ))}
                    </Select>
                    <span className="block text-xs text-ink-500 mt-1.5">
                        {BOT_TYPES.find((entry) => entry.id === form.bot_type)?.hint}
                    </span>
                </Field>
            </Section>

            <Section title="What it says">
                <Field label="Welcome message" hint="Sent on the customer's first message. Leave blank to send nothing.">
                    <Textarea
                        rows={2}
                        value={form.welcome_message}
                        onChange={(event) => set({ welcome_message: event.target.value })}
                    />
                </Field>
                <Field label="Fallback" hint="When nothing matched and the AI could not answer.">
                    <Textarea
                        rows={2}
                        value={form.fallback_message}
                        onChange={(event) => set({ fallback_message: event.target.value })}
                    />
                </Field>
                <Field label="When it can't read the message" hint="A sticker, a location, a photo with no caption.">
                    <Textarea
                        rows={2}
                        value={form.unsupported_message}
                        onChange={(event) => set({ unsupported_message: event.target.value })}
                    />
                </Field>
                <Field label="Reply language">
                    <Select value={form.language} onChange={(event) => set({ language: event.target.value })}>
                        {LANGUAGES.map((entry) => (
                            <option key={entry.id} value={entry.id}>
                                {entry.label}
                            </option>
                        ))}
                    </Select>
                    <span className="block text-xs text-ink-500 mt-1.5">
                        "Match the customer" detects English, Hindi and Hinglish and answers in the same one.
                    </span>
                </Field>
            </Section>

            <Section title="AI">
                <CheckboxField
                    label="Let the AI answer what the rules don't"
                    description="Uses your AI agent's provider, key and model. Switch it off for a rules-only bot."
                    checked={form.ai_enabled}
                    onChange={(event) => set({ ai_enabled: event.target.checked })}
                    className="mb-4"
                />
                {form.ai_enabled && (
                    /* The provider, key and model are workspace-wide and live on
                       the AI Agent page; the documents live on the Knowledge base
                       page. Linking rather than duplicating them here, because two
                       places to set one API key is one place to set it wrong. */
                    <p className="text-xs text-ink-500 -mt-2 mb-4">
                        The provider, key and model come from{" "}
                        <Link to="/ai" className="font-semibold text-brand-700">
                            AI Agent
                        </Link>
                        . Documents live in your{" "}
                        <Link to="/knowledge" className="font-semibold text-brand-700">
                            Knowledge base
                        </Link>
                        .
                    </p>
                )}
                {form.ai_enabled && (
                    <>
                        <Field label="Knowledge base" hint="Blank uses your workspace's default.">
                            <Select
                                value={form.knowledge_base || ""}
                                onChange={(event) => set({ knowledge_base: event.target.value || "" })}
                            >
                                <option value="">Default</option>
                                {knowledgeBases.map((base) => (
                                    <option key={base.id} value={base.id}>
                                        {base.name}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field
                            label="Who this bot is"
                            hint="Added on top of the platform's own safety rules, never instead of them."
                        >
                            <Textarea
                                rows={3}
                                value={form.system_prompt}
                                onChange={(event) => set({ system_prompt: event.target.value })}
                                placeholder="You are the customer support assistant for Acme Electronics in Mumbai."
                            />
                        </Field>
                        <Field label="Tone">
                            <Input
                                value={form.persona}
                                onChange={(event) => set({ persona: event.target.value })}
                                placeholder="warm, brief, never pushy"
                            />
                        </Field>
                        <div className="grid sm:grid-cols-3 gap-x-4">
                            <Field label="Longest reply" hint="Characters.">
                                <Input
                                    type="number"
                                    min={100}
                                    value={form.max_ai_response_chars}
                                    onChange={(event) => set({ max_ai_response_chars: Number(event.target.value) })}
                                />
                            </Field>
                            <Field label="Messages of history" hint="How far back it reads.">
                                <Input
                                    type="number"
                                    min={0}
                                    max={50}
                                    value={form.history_messages}
                                    onChange={(event) => set({ history_messages: Number(event.target.value) })}
                                />
                            </Field>
                            <Field label="Relevance floor" hint="0–1. Higher answers less, and guesses less.">
                                <Input
                                    type="number"
                                    step="0.05"
                                    min={0}
                                    max={1}
                                    value={form.min_relevance}
                                    onChange={(event) => set({ min_relevance: Number(event.target.value) })}
                                />
                            </Field>
                        </div>
                    </>
                )}
            </Section>

            <Section title="Handing over to a person">
                <CheckboxField
                    label="Let this bot hand conversations to your team"
                    checked={form.human_handoff_enabled}
                    onChange={(event) => set({ human_handoff_enabled: event.target.checked })}
                    className="mb-4"
                />
                {form.human_handoff_enabled && (
                    <>
                        <Field label="What to say when it hands over">
                            <Textarea
                                rows={2}
                                value={form.handoff_message}
                                onChange={(event) => set({ handoff_message: event.target.value })}
                            />
                        </Field>
                        <p className="text-sm font-semibold text-ink-700 mb-2">Hand over when…</p>
                        <div className="space-y-3 mb-4">
                            {HANDOFF_CONDITIONS.map((condition) => {
                                const current = form.handoff_conditions[condition.key];
                                if (condition.type === "bool") {
                                    return (
                                        <CheckboxField
                                            key={condition.key}
                                            label={condition.label}
                                            checked={current ?? condition.key !== "negative_sentiment"}
                                            onChange={(event) => setCondition(condition.key, event.target.checked)}
                                        />
                                    );
                                }
                                if (condition.type === "number") {
                                    return (
                                        <div key={condition.key} className="flex items-center gap-3">
                                            <Input
                                                type="number"
                                                min={0}
                                                className="w-24"
                                                value={current ?? (condition.key === "repeated_fallback" ? 3 : 0)}
                                                onChange={(event) =>
                                                    setCondition(condition.key, Number(event.target.value))
                                                }
                                            />
                                            <span className="text-sm text-ink-700">{condition.label}</span>
                                        </div>
                                    );
                                }
                                return (
                                    <Field key={condition.key} label={condition.label} className="mb-0">
                                        <Input
                                            value={current ?? ""}
                                            onChange={(event) => setCondition(condition.key, event.target.value)}
                                            placeholder="complaint, manager, escalate"
                                        />
                                    </Field>
                                );
                            })}
                        </div>
                    </>
                )}
            </Section>

            <Section title="Timing">
                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="Shortest reply delay" hint="Seconds. 0 replies instantly.">
                        <Input
                            type="number"
                            min={0}
                            max={120}
                            value={form.response_delay_min_seconds}
                            onChange={(event) => set({ response_delay_min_seconds: Number(event.target.value) })}
                        />
                    </Field>
                    <Field label="Longest reply delay" hint="Randomised between the two, so it reads less mechanical.">
                        <Input
                            type="number"
                            min={0}
                            max={120}
                            value={form.response_delay_max_seconds}
                            onChange={(event) => set({ response_delay_max_seconds: Number(event.target.value) })}
                        />
                    </Field>
                </div>
                <Field
                    label="Start a new conversation after"
                    hint="Minutes of silence. 0 keeps one conversation forever."
                >
                    <Input
                        type="number"
                        min={0}
                        value={form.conversation_timeout_minutes}
                        onChange={(event) => set({ conversation_timeout_minutes: Number(event.target.value) })}
                    />
                </Field>

                <CheckboxField
                    label="Only answer during working hours"
                    description="Separate from your team's hours — the bot is usually wanted precisely when nobody is at a desk."
                    checked={form.working_hours_enabled}
                    onChange={(event) => set({ working_hours_enabled: event.target.checked })}
                    className="mb-4"
                />

                {form.working_hours_enabled && hours && (
                    <>
                        <div className="space-y-2 mb-4">
                            {hours.map((row, index) => (
                                <div key={row.weekday} className="flex items-center gap-3">
                                    <label className="flex items-center gap-2 w-32 shrink-0 text-sm text-ink-700 cursor-pointer">
                                        <input
                                            type="checkbox"
                                            className="w-4 h-4 accent-brand-600 cursor-pointer"
                                            checked={row.is_open}
                                            onChange={(event) =>
                                                setHours(
                                                    hours.map((entry, position) =>
                                                        position === index
                                                            ? { ...entry, is_open: event.target.checked }
                                                            : entry,
                                                    ),
                                                )
                                            }
                                        />
                                        {WEEKDAYS[row.weekday]}
                                    </label>
                                    <Input
                                        type="time"
                                        className="w-32"
                                        disabled={!row.is_open}
                                        value={row.opens_at}
                                        onChange={(event) =>
                                            setHours(
                                                hours.map((entry, position) =>
                                                    position === index
                                                        ? { ...entry, opens_at: event.target.value }
                                                        : entry,
                                                ),
                                            )
                                        }
                                    />
                                    <span className="text-ink-400">to</span>
                                    <Input
                                        type="time"
                                        className="w-32"
                                        disabled={!row.is_open}
                                        value={row.closes_at}
                                        onChange={(event) =>
                                            setHours(
                                                hours.map((entry, position) =>
                                                    position === index
                                                        ? { ...entry, closes_at: event.target.value }
                                                        : entry,
                                                ),
                                            )
                                        }
                                    />
                                </div>
                            ))}
                        </div>
                        <Field label="Outside hours, say" className="mb-0">
                            <Textarea
                                rows={2}
                                value={form.outside_hours_message}
                                onChange={(event) => set({ outside_hours_message: event.target.value })}
                            />
                        </Field>
                    </>
                )}
            </Section>

            <Section title="Existing automation" last>
                <CheckboxField
                    label="Let existing automation rules and workflows answer first"
                    description="On, this bot only takes what your older keyword rules and workflows didn't. Off, it answers instead of them."
                    checked={form.defer_to_automation}
                    onChange={(event) => set({ defer_to_automation: event.target.checked })}
                />
            </Section>
        </Modal>
    );
}

function Section({ title, children, last = false }) {
    return (
        <section className={last ? "" : "mb-6 pb-6 border-b border-ink-100"}>
            <h3 className="text-sm font-bold text-ink-900 uppercase tracking-wide m-0 mb-4">{title}</h3>
            {children}
        </section>
    );
}

function VersionHistory({ bot, onClose, onChanged }) {
    const toast = useToast();
    const [versions, setVersions] = useState(null);
    const [busy, setBusy] = useState(null);

    useEffect(() => {
        api.get(`/chatbot/bots/${bot.id}/versions/`)
            .then((response) => setVersions(response.data || []))
            .catch(() => setVersions([]));
    }, [bot.id]);

    const rollback = async (version) => {
        setBusy(version.id);
        try {
            await api.post(`/chatbot/bots/${bot.id}/rollback/`, { version_id: version.id });
            toast.success(`Rolled back to v${version.version}.`);
            onChanged();
            onClose();
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not roll back."));
        } finally {
            setBusy(null);
        }
    };

    return (
        <Modal
            title={`Versions of "${bot.name}"`}
            description="Every publish is kept. Rolling back puts an old one in front of customers again."
            onClose={onClose}
            width="lg"
        >
            {versions === null ? (
                <div className="flex justify-center py-8">
                    <Spinner />
                </div>
            ) : versions.length === 0 ? (
                <p className="text-sm text-ink-500 m-0">This bot has never been published.</p>
            ) : (
                <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                    {versions.map((version) => (
                        <li key={version.id} className="flex items-center justify-between gap-3 py-3">
                            <div className="min-w-0">
                                <p className="text-sm font-semibold text-ink-900 m-0">
                                    v{version.version}
                                    {version.is_live && (
                                        <span className="ml-2 text-xs font-bold text-brand-700">Live</span>
                                    )}
                                    {!version.published_at && (
                                        <span className="ml-2 text-xs font-bold text-ink-400">Draft</span>
                                    )}
                                </p>
                                <p className="text-xs text-ink-500 m-0 mt-0.5">
                                    {version.published_at
                                        ? `Published ${new Date(version.published_at).toLocaleString()}`
                                        : "Not published"}
                                    {version.published_by_username ? ` by ${version.published_by_username}` : ""}
                                    {` · ${version.rules?.length || 0} rules · ${
                                        version.flow?.nodes?.length || 0
                                    } flow nodes`}
                                </p>
                            </div>
                            {version.published_at && !version.is_live && (
                                <Button
                                    size="sm"
                                    variant="secondary"
                                    icon={RotateCcw}
                                    loading={busy === version.id}
                                    onClick={() => rollback(version)}
                                >
                                    Roll back
                                </Button>
                            )}
                        </li>
                    ))}
                </ul>
            )}
        </Modal>
    );
}


/**
 * Choosing a starter flow, and which bot to put it in.
 *
 * The catalogue comes from the server rather than being listed here, because
 * the server is what builds and validates the graph -- a template this screen
 * knew about and the API did not would be a card that fails on click.
 *
 * Applying to a bot that already has a flow asks first. The endpoint refuses it
 * with a 409 unless `replace` is set, and this is where that confirmation
 * belongs: the draft is the one version with no history behind it, so replacing
 * it is not something to recover from afterwards.
 */
function TemplatePicker({ bots, onClose, onApplied }) {
    const toast = useToast();
    const [catalogue, setCatalogue] = useState([]);
    const [loading, setLoading] = useState(true);
    const [botId, setBotId] = useState(bots[0]?.id ? String(bots[0].id) : "");
    const [chosen, setChosen] = useState(null);
    const [applying, setApplying] = useState(false);
    const [confirmReplace, setConfirmReplace] = useState(null);

    useEffect(() => {
        api.get("/chatbot/bots/templates/")
            .then((response) => setCatalogue(response.data || []))
            .catch(() => setCatalogue([]))
            .finally(() => setLoading(false));
    }, []);

    const apply = async (templateId, replace = false) => {
        setApplying(true);
        try {
            await api.post(`/chatbot/bots/${botId}/apply-template/`, { template: templateId, replace });
            toast.success("Template applied. Open the canvas to edit it.");
            onApplied();
        } catch (error) {
            if (error?.response?.status === 409) {
                setConfirmReplace(templateId);
            } else {
                toast.error(apiErrorMessage(error, "Could not apply that template."));
            }
        } finally {
            setApplying(false);
        }
    };

    const groups = [...new Set(catalogue.map((entry) => entry.category))];

    return (
        <>
            <Modal
                title="Start from a template"
                description="A working conversation you can edit, rather than an empty canvas."
                width="lg"
                onClose={onClose}
                footer={
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                }
            >
                <Field label="Put it in" hint="The template replaces this bot's draft flow, not its rules.">
                    <Select value={botId} onChange={(event) => setBotId(event.target.value)}>
                        {bots.map((bot) => (
                            <option key={bot.id} value={bot.id}>
                                {bot.name}
                            </option>
                        ))}
                    </Select>
                </Field>

                {loading ? (
                    <div className="flex justify-center py-10">
                        <Spinner />
                    </div>
                ) : (
                    <div className="space-y-4 mt-4">
                        {groups.map((group) => (
                            <div key={group}>
                                <p className="text-[10px] font-bold uppercase tracking-wider text-ink-400 mb-2">{group}</p>
                                <div className="grid sm:grid-cols-2 gap-2">
                                    {catalogue
                                        .filter((entry) => entry.category === group)
                                        .map((entry) => (
                                            <button
                                                key={entry.id}
                                                type="button"
                                                disabled={applying || !botId}
                                                onClick={() => {
                                                    setChosen(entry.id);
                                                    apply(entry.id);
                                                }}
                                                className={`text-left p-3 rounded-xl border transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed ${
                                                    chosen === entry.id
                                                        ? "border-brand-400 bg-brand-50"
                                                        : "border-ink-200 hover:border-brand-300 hover:bg-ink-50"
                                                }`}
                                            >
                                                <span className="block text-sm font-semibold text-ink-900">{entry.name}</span>
                                                <span className="block text-xs text-ink-500 mt-0.5 leading-snug">
                                                    {entry.description}
                                                </span>
                                            </button>
                                        ))}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </Modal>

            {confirmReplace && (
                <ConfirmDialog
                    title="Replace the existing flow?"
                    message="This bot already has a draft flow. Applying a template discards it, and there is no version to roll back to for a draft."
                    confirmLabel="Replace it"
                    tone="danger"
                    onConfirm={() => {
                        const templateId = confirmReplace;
                        setConfirmReplace(null);
                        apply(templateId, true);
                    }}
                    onClose={() => setConfirmReplace(null)}
                />
            )}
        </>
    );
}
