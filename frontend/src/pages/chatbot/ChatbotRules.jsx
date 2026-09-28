import { useCallback, useEffect, useState } from "react";
import { ArrowDown, ArrowUp, FlaskConical, ListChecks, Pencil, Plus, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button, { IconButton } from "../../components/ui/Button";
import Card from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import Spinner from "../../components/ui/Spinner";
import { CheckboxField, Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import ActionBuilder, { newAction } from "./ActionBuilder";
import BotPicker from "./BotPicker";

/**
 * The rule builder: "when a customer says X, do Y".
 *
 * Rules are shown in the order they are evaluated, which is the order that
 * matters and the one people get wrong — a catch-all at priority 1 silences
 * every rule below it, and seeing the list in execution order is what makes
 * that obvious rather than mysterious.
 */

const MATCH_TYPES = [
    { id: "CONTAINS", label: "Contains", placeholder: "price" },
    { id: "KEYWORD", label: "Any of these words", placeholder: "price, cost, how much" },
    { id: "EXACT", label: "Is exactly", placeholder: "hi" },
    { id: "STARTS_WITH", label: "Starts with", placeholder: "order " },
    { id: "ENDS_WITH", label: "Ends with", placeholder: "?" },
    { id: "REGEX", label: "Matches a pattern", placeholder: "^order\\s+#?\\d{4,}$" },
    { id: "BUTTON", label: "A button was tapped", placeholder: "see_pricing" },
    { id: "ANY", label: "Any message (a catch-all)", placeholder: "" },
];

function blankRule(chatbotId) {
    return {
        chatbot: chatbotId,
        name: "",
        match_type: "CONTAINS",
        pattern: "",
        case_sensitive: false,
        priority: 10,
        cooldown_seconds: 0,
        is_active: true,
        actions: [newAction("SEND_MESSAGE")],
    };
}

export default function ChatbotRules({ bots, selected, onSelectBot, onChanged, can }) {
    const toast = useToast();
    const [rules, setRules] = useState([]);
    const [loading, setLoading] = useState(true);
    const [editing, setEditing] = useState(null);
    const [deleting, setDeleting] = useState(null);
    const [testing, setTesting] = useState(false);
    const [options, setOptions] = useState({});

    const load = useCallback(async () => {
        if (!selected) {
            setRules([]);
            setLoading(false);
            return;
        }
        setLoading(true);
        try {
            const response = await api.get("/chatbot/rules/", {
                params: { chatbot: selected.id, page_size: 100 },
            });
            setRules(response.data.results ?? response.data ?? []);
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not load the rules."));
        } finally {
            setLoading(false);
        }
    }, [selected, toast]);

    useEffect(() => {
        load();
    }, [load]);

    // The pickers inside the action builder. Fetched once for the tab rather
    // than per rule form, and every failure degrades to an empty list rather
    // than blocking the editor.
    useEffect(() => {
        const settle = (promise) => promise.then((response) => response.data.results ?? response.data ?? []).catch(() => []);
        Promise.all([
            settle(api.get("/ai/knowledge-bases/", { params: { page_size: 100 } })),
            settle(api.get("/teams/members/", { params: { page_size: 100 } })),
            settle(api.get("/campaigns/", { params: { page_size: 50 } })),
            settle(api.get("/automation/workflows/", { params: { page_size: 50 } })),
            settle(api.get("/chatbot/assets/", { params: { page_size: 100 } })),
        ]).then(([knowledgeBases, members, campaigns, workflows, assets]) => {
            setOptions({
                knowledgeBases,
                agents: members.map((member) => ({ id: member.user, username: member.user_username || member.username })),
                campaigns,
                workflows,
                assets,
                chatbots: bots,
            });
        });
    }, [bots]);

    const move = async (rule, direction) => {
        const ordered = [...rules];
        const index = ordered.findIndex((entry) => entry.id === rule.id);
        const target = index + direction;
        if (target < 0 || target >= ordered.length) return;

        [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
        // Renumbered in tens so there is always room to slot something between
        // two rules without renumbering the whole list again.
        const order = ordered.map((entry, position) => ({ id: entry.id, priority: (position + 1) * 10 }));
        setRules(ordered.map((entry, position) => ({ ...entry, priority: (position + 1) * 10 })));

        try {
            await api.post("/chatbot/rules/reorder/", { order });
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not reorder the rules."));
            load();
        }
    };

    const remove = async (rule) => {
        try {
            await api.delete(`/chatbot/rules/${rule.id}/`);
            toast.success("Rule deleted.");
            load();
            onChanged();
        } catch (error) {
            toast.error(apiErrorMessage(error, "Could not delete that rule."));
        }
    };

    if (!bots.length) {
        return (
            <EmptyState
                icon={ListChecks}
                title="No chatbots yet"
                description="Create a chatbot first — rules belong to one."
            />
        );
    }

    return (
        <div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
                <BotPicker bots={bots} selected={selected} onSelect={onSelectBot} />
                <div className="flex gap-2">
                    <Button size="sm" variant="secondary" icon={FlaskConical} onClick={() => setTesting(true)}>
                        Test a message
                    </Button>
                    {can("CHATBOT_EDIT") && (
                        <Button size="sm" icon={Plus} onClick={() => setEditing(blankRule(selected.id))}>
                            Add rule
                        </Button>
                    )}
                </div>
            </div>

            {selected?.status === "PUBLISHED" && (
                <Alert tone="info" className="mb-4">
                    This bot is live. Rule changes reach customers when you publish again from the Bots tab.
                </Alert>
            )}

            {loading ? (
                <div className="flex justify-center py-16">
                    <Spinner />
                </div>
            ) : rules.length === 0 ? (
                <EmptyState
                    icon={ListChecks}
                    title="No rules yet"
                    description="A rule answers a specific thing exactly the way you want. Anything without a rule falls through to the AI."
                    action={
                        can("CHATBOT_EDIT") && (
                            <Button icon={Plus} onClick={() => setEditing(blankRule(selected.id))}>
                                Write your first rule
                            </Button>
                        )
                    }
                />
            ) : (
                <div className="space-y-3">
                    {rules.map((rule, index) => (
                        <Card key={rule.id} className={rule.is_active ? "" : "opacity-60"}>
                            <div className="flex items-start gap-3">
                                <div className="flex flex-col gap-0.5 shrink-0 pt-0.5">
                                    <button
                                        type="button"
                                        aria-label="Move up"
                                        disabled={index === 0}
                                        onClick={() => move(rule, -1)}
                                        className="w-7 h-6 inline-flex items-center justify-center rounded text-ink-400 hover:bg-ink-100 hover:text-ink-700 disabled:opacity-30 disabled:cursor-not-allowed bg-transparent border-none cursor-pointer"
                                    >
                                        <ArrowUp size={14} />
                                    </button>
                                    <button
                                        type="button"
                                        aria-label="Move down"
                                        disabled={index === rules.length - 1}
                                        onClick={() => move(rule, 1)}
                                        className="w-7 h-6 inline-flex items-center justify-center rounded text-ink-400 hover:bg-ink-100 hover:text-ink-700 disabled:opacity-30 disabled:cursor-not-allowed bg-transparent border-none cursor-pointer"
                                    >
                                        <ArrowDown size={14} />
                                    </button>
                                </div>

                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap mb-1">
                                        <span className="w-6 h-6 shrink-0 rounded-md bg-ink-900 text-ink-50 text-xs font-bold flex items-center justify-center tabular-nums">
                                            {index + 1}
                                        </span>
                                        <h3 className="text-sm font-bold text-ink-900 m-0 truncate">{rule.name}</h3>
                                        {!rule.is_active && <Badge tone="gray">Off</Badge>}
                                        {rule.match_type === "ANY" && <Badge tone="amber">Catch-all</Badge>}
                                    </div>

                                    <p className="text-sm text-ink-600 m-0">
                                        <span className="text-ink-400">When the message </span>
                                        {MATCH_TYPES.find((entry) => entry.id === rule.match_type)?.label.toLowerCase()}
                                        {rule.pattern && (
                                            <code className="ml-1.5 px-1.5 py-0.5 rounded bg-ink-100 text-ink-800 text-xs">
                                                {rule.pattern}
                                            </code>
                                        )}
                                    </p>

                                    <p className="text-xs text-ink-500 m-0 mt-1.5">
                                        {rule.actions.length} {rule.actions.length === 1 ? "step" : "steps"}
                                        {rule.cooldown_seconds > 0 && ` · waits ${rule.cooldown_seconds}s between firings`}
                                        {` · matched ${rule.match_count}×`}
                                        {rule.last_matched_at &&
                                            ` · last ${new Date(rule.last_matched_at).toLocaleDateString()}`}
                                    </p>
                                </div>

                                {can("CHATBOT_EDIT") && (
                                    <div className="flex gap-1 shrink-0">
                                        <IconButton icon={Pencil} title="Edit" onClick={() => setEditing(rule)} />
                                        <IconButton icon={Trash2} title="Delete" onClick={() => setDeleting(rule)} />
                                    </div>
                                )}
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            {editing && (
                <RuleForm
                    value={editing}
                    options={{ ...options, provider: selected?.account_provider }}
                    onClose={() => setEditing(null)}
                    onSaved={() => {
                        setEditing(null);
                        load();
                        onChanged();
                    }}
                />
            )}

            {deleting && (
                <ConfirmDialog
                    title={`Delete "${deleting.name}"?`}
                    message="Customers stop getting this answer as soon as you publish again."
                    confirmLabel="Delete rule"
                    onClose={() => setDeleting(null)}
                    onConfirm={async () => {
                        await remove(deleting);
                        setDeleting(null);
                    }}
                />
            )}

            {testing && <RuleTester bot={selected} onClose={() => setTesting(false)} />}
        </div>
    );
}

function RuleForm({ value, options, onClose, onSaved }) {
    const toast = useToast();
    const [form, setForm] = useState(value);
    const [saving, setSaving] = useState(false);
    const [errors, setErrors] = useState({});

    const isNew = !value.id;
    const set = (patch) => setForm((current) => ({ ...current, ...patch }));
    const matchType = MATCH_TYPES.find((entry) => entry.id === form.match_type);

    const save = async () => {
        setSaving(true);
        setErrors({});
        try {
            if (isNew) await api.post("/chatbot/rules/", form);
            else await api.patch(`/chatbot/rules/${value.id}/`, form);
            toast.success(isNew ? "Rule added." : "Rule saved.");
            onSaved();
        } catch (error) {
            const data = error?.response?.data;
            if (data && typeof data === "object") setErrors(data);
            toast.error(apiErrorMessage(error, "Could not save that rule."));
        } finally {
            setSaving(false);
        }
    };

    const fieldError = (name) => {
        const message = errors[name];
        return Array.isArray(message) ? message.join(" ") : message;
    };

    return (
        <Modal
            title={isNew ? "New rule" : `Edit "${value.name}"`}
            width="xl"
            onClose={onClose}
            footer={
                <div className="flex justify-end gap-2">
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button loading={saving} onClick={save}>
                        {isNew ? "Add rule" : "Save rule"}
                    </Button>
                </div>
            }
        >
            <Field label="Name" required error={fieldError("name")} hint="For your team. Customers never see it.">
                <Input
                    value={form.name}
                    onChange={(event) => set({ name: event.target.value })}
                    placeholder="Pricing enquiry"
                    invalid={Boolean(fieldError("name"))}
                />
            </Field>

            <div className="border border-ink-200 rounded-xl p-4 mb-5 bg-ink-50/40">
                <p className="text-xs font-bold text-ink-500 uppercase tracking-wide m-0 mb-3">When</p>
                <div className="grid sm:grid-cols-2 gap-x-4">
                    <Field label="The message" className="mb-4">
                        <Select value={form.match_type} onChange={(event) => set({ match_type: event.target.value })}>
                            {MATCH_TYPES.map((entry) => (
                                <option key={entry.id} value={entry.id}>
                                    {entry.label}
                                </option>
                            ))}
                        </Select>
                    </Field>
                    {form.match_type !== "ANY" && (
                        <Field
                            label={form.match_type === "BUTTON" ? "Button id" : "This"}
                            error={fieldError("pattern")}
                            className="mb-4"
                        >
                            <Input
                                value={form.pattern}
                                onChange={(event) => set({ pattern: event.target.value })}
                                placeholder={matchType?.placeholder}
                                invalid={Boolean(fieldError("pattern"))}
                            />
                        </Field>
                    )}
                </div>

                {form.match_type === "KEYWORD" && (
                    <p className="text-xs text-ink-500 m-0 mb-3">
                        Separate words with commas. Matched as whole words, so "price" will not fire on "surprise".
                    </p>
                )}
                {form.match_type === "ANY" && (
                    <p className="text-xs text-ink-500 m-0 mb-3">
                        Answers everything that reaches it. Put this last, on the highest number, or it will silence
                        every rule below it.
                    </p>
                )}

                <CheckboxField
                    label="Match upper and lower case exactly"
                    checked={form.case_sensitive}
                    onChange={(event) => set({ case_sensitive: event.target.checked })}
                />
            </div>

            <div className="mb-5">
                <p className="text-xs font-bold text-ink-500 uppercase tracking-wide m-0 mb-3">Then</p>
                {fieldError("actions") && (
                    <Alert tone="error" className="mb-3">
                        {fieldError("actions")}
                    </Alert>
                )}
                <ActionBuilder
                    actions={form.actions}
                    onChange={(actions) => set({ actions })}
                    options={options}
                />
            </div>

            <div className="grid sm:grid-cols-2 gap-x-4">
                <Field
                    label="Wait between firings"
                    hint="Seconds. Stops a customer typing the same word five times getting five identical replies."
                >
                    <Input
                        type="number"
                        min={0}
                        value={form.cooldown_seconds}
                        onChange={(event) => set({ cooldown_seconds: Number(event.target.value) })}
                    />
                </Field>
                <Field label="Priority" hint="Lower runs first.">
                    <Input
                        type="number"
                        value={form.priority}
                        onChange={(event) => set({ priority: Number(event.target.value) })}
                    />
                </Field>
            </div>

            <CheckboxField
                label="Active"
                description="Off keeps the rule but stops it answering."
                checked={form.is_active}
                onChange={(event) => set({ is_active: event.target.checked })}
            />
        </Modal>
    );
}

/**
 * "What would this bot do if somebody sent…?"
 *
 * Runs against the API, which runs the engine's own matcher over the engine's
 * own rule source — so the answer is what would actually happen, not a second
 * implementation's guess at it. Nothing is sent.
 */
function RuleTester({ bot, onClose }) {
    const [message, setMessage] = useState("");
    const [usePublished, setUsePublished] = useState(true);
    const [result, setResult] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    const run = async () => {
        setBusy(true);
        setError("");
        try {
            const response = await api.post(`/chatbot/bots/${bot.id}/test-rules/`, {
                message,
                use_published: usePublished,
            });
            setResult(response.data);
        } catch (requestError) {
            setError(apiErrorMessage(requestError, "Could not run the test."));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            title="Test a message"
            description="Nothing is sent. This runs the same matcher a real message runs through."
            width="lg"
            onClose={onClose}
        >
            <Field label="What the customer sends">
                <Input
                    value={message}
                    onChange={(event) => setMessage(event.target.value)}
                    onKeyDown={(event) => event.key === "Enter" && message.trim() && run()}
                    placeholder="what is your price?"
                    autoFocus
                />
            </Field>

            <CheckboxField
                label="Test the published version"
                description="Off tests your unpublished draft instead."
                checked={usePublished}
                onChange={(event) => setUsePublished(event.target.checked)}
                className="mb-4"
            />

            <Button loading={busy} disabled={!message.trim()} onClick={run} icon={FlaskConical}>
                Run
            </Button>

            {error && (
                <Alert tone="error" className="mt-4">
                    {error}
                </Alert>
            )}

            {result && (
                <div className="mt-5 pt-5 border-t border-ink-100">
                    <p className="text-xs text-ink-500 m-0 mb-3">Checked against the {result.source}.</p>

                    {result.matched_rule ? (
                        <Alert tone="success">
                            <strong>{result.matched_rule.name}</strong> answers this, running{" "}
                            {result.actions.length} {result.actions.length === 1 ? "step" : "steps"}.
                        </Alert>
                    ) : result.would_reach_ai ? (
                        <Alert tone="info">
                            No rule matched, so this would go to the AI and your knowledge base.
                        </Alert>
                    ) : (
                        <Alert tone="warning">
                            No rule matched and AI answering is off, so the customer would get the fallback message.
                        </Alert>
                    )}

                    <ul className="list-none p-0 m-0 mt-4 divide-y divide-ink-100">
                        {result.rules_checked.map((rule) => (
                            <li key={rule.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                                <span className={rule.matched ? "font-semibold text-ink-900" : "text-ink-500"}>
                                    {rule.name}
                                </span>
                                {rule.skipped ? (
                                    <Badge tone="gray">Off</Badge>
                                ) : rule.matched ? (
                                    <Badge tone="green">Matches</Badge>
                                ) : (
                                    <span className="text-xs text-ink-400">no</span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </Modal>
    );
}
