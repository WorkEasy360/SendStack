import { useCallback, useEffect, useMemo, useState } from "react";
import {
    ArrowRight,
    Brain,
    CircleDot,
    Clock,
    FileText,
    GitBranch,
    HelpCircle,
    LayoutGrid,
    List,
    MessageSquare,
    Pencil,
    Plus,
    Save,
    Square,
    Tag,
    Trash2,
    UserCheck,
    Variable,
    Wrench,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button, { IconButton } from "../../components/ui/Button";
import Card from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import ActionBuilder from "./ActionBuilder";
import BotPicker from "./BotPicker";
import ChatbotCanvas from "./ChatbotCanvas";
import { DEFAULT_TRIGGER, TRIGGERS, describeTrigger, triggerMeta, triggerOf } from "../../utils/chatbotGraph";

/**
 * The flow builder.
 *
 * Two views of one graph, and the toggle is not a preference — they are good
 * at different things.
 *
 * **Canvas** is the default and is where a conversation gets designed: the
 * branches are arrows, so "what happens if they tap Pricing" is something you
 * look at rather than work out.
 *
 * **List** draws the same nodes in reachable order from Start with every edge
 * as a dropdown of real node ids. It stays because it is the faster way to
 * audit a flow somebody else drew, and because an edge chosen from a list of
 * existing nodes cannot be left dangling in the first place.
 *
 * Both write the same JSON to the same endpoint, which validates it. Neither
 * view is the source of truth; the stored draft is.
 */

const NODE_TYPES = [
    { id: "START", label: "Start", icon: CircleDot, actions: [], hint: "What makes a conversation enter this flow." },
    { id: "MESSAGE", label: "Message", icon: MessageSquare, actions: ["SEND_MESSAGE"], hint: "Say something." },
    {
        id: "QUESTION",
        label: "Question",
        icon: HelpCircle,
        actions: ["ASK_QUESTION"],
        hint: "Ask, then wait for the customer's answer.",
    },
    { id: "CONDITION", label: "Condition", icon: GitBranch, actions: [], hint: "Branch on something you know." },
    { id: "AI", label: "AI", icon: Brain, actions: ["AI_REPLY"], hint: "Let the AI answer." },
    {
        id: "KNOWLEDGE",
        label: "Knowledge base",
        icon: FileText,
        actions: ["KNOWLEDGE_ANSWER"],
        hint: "Answer from your documents.",
    },
    { id: "MEDIA", label: "File", icon: FileText, actions: ["SEND_MEDIA"], hint: "Send an image, video or PDF." },
    { id: "DELAY", label: "Pause", icon: Clock, actions: [], hint: "Wait before the next message." },
    { id: "TAG", label: "Tag", icon: Tag, actions: ["ADD_TAG", "REMOVE_TAG"], hint: "Label the contact." },
    { id: "VARIABLE", label: "Remember", icon: Variable, actions: ["SET_VARIABLE"], hint: "Store a value." },
    {
        id: "HANDOFF",
        label: "Hand over",
        icon: UserCheck,
        actions: ["HANDOFF", "ASSIGN_AGENT"],
        hint: "Give the conversation to a person.",
    },
    {
        id: "TOOL",
        label: "Tool or API",
        icon: Wrench,
        actions: ["CALL_TOOL", "CALL_API", "TRIGGER_CAMPAIGN", "TRIGGER_AUTOMATION"],
        hint: "Look something up or start something.",
    },
    {
        id: "END",
        label: "End",
        icon: Square,
        actions: ["END_CONVERSATION", "SEND_MESSAGE"],
        hint: "Finish the conversation.",
    },
];

const CONDITION_FIELDS = [
    { id: "last_message", label: "The message" },
    { id: "button_clicked", label: "The button tapped" },
    { id: "variable", label: "A remembered value" },
    { id: "contact_tag", label: "The contact's tags" },
    { id: "language", label: "The language" },
    { id: "ai_replies", label: "How many AI replies so far" },
    { id: "bot_replies", label: "How many bot replies so far" },
    { id: "messages_received", label: "How many messages received" },
    { id: "within_hours", label: "Whether we're within hours" },
    { id: "state", label: "The conversation state" },
];

const OPERATORS = [
    { id: "contains", label: "contains" },
    { id: "not_contains", label: "does not contain" },
    { id: "equals", label: "is exactly" },
    { id: "not_equals", label: "is not" },
    { id: "starts_with", label: "starts with" },
    { id: "matches", label: "matches the pattern" },
    { id: "exists", label: "is set" },
    { id: "not_exists", label: "is not set" },
    { id: "greater_than", label: "is more than" },
    { id: "less_than", label: "is less than" },
];

const typeOf = (id) => NODE_TYPES.find((entry) => entry.id === id) || NODE_TYPES[1];

function nextId(nodes, type) {
    const stem = type.toLowerCase();
    let counter = 1;
    while (nodes.some((node) => node.id === `${stem}_${counter}`)) counter += 1;
    return `${stem}_${counter}`;
}

/** Nodes in the order a conversation reaches them, with anything orphaned last. */
function inReachOrder(nodes) {
    const byId = Object.fromEntries(nodes.map((node) => [node.id, node]));
    const start = nodes.find((node) => node.type === "START");
    if (!start) return nodes;

    const seen = new Set();
    const ordered = [];
    const queue = [start.id];
    while (queue.length) {
        const id = queue.shift();
        if (!id || seen.has(id) || !byId[id]) continue;
        seen.add(id);
        ordered.push(byId[id]);
        const node = byId[id];
        [node.next, node.true_next, node.false_next].forEach((edge) => edge && queue.push(edge));
    }
    return [...ordered, ...nodes.filter((node) => !seen.has(node.id))];
}

export default function ChatbotFlow({ bots, selected, onSelectBot, onChanged, can }) {
    const toast = useToast();
    const [nodes, setNodes] = useState([]);
    const [version, setVersion] = useState(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [dirty, setDirty] = useState(false);
    const [editing, setEditing] = useState(null);
    const [deleting, setDeleting] = useState(null);
    const [error, setError] = useState("");
    const [options, setOptions] = useState({});
    const [view, setView] = useState("canvas");

    const load = useCallback(async () => {
        if (!selected) {
            setNodes([]);
            setLoading(false);
            return;
        }
        setLoading(true);
        try {
            const response = await api.get(`/chatbot/bots/${selected.id}/flow/`);
            setNodes(response.data.flow?.nodes || []);
            setVersion(response.data.version);
            setDirty(false);
            setError("");
        } catch (requestError) {
            setError(apiErrorMessage(requestError, "Could not load the flow."));
        } finally {
            setLoading(false);
        }
    }, [selected]);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        const settle = (promise) => promise.then((response) => response.data.results ?? response.data ?? []).catch(() => []);
        Promise.all([
            settle(api.get("/ai/knowledge-bases/", { params: { page_size: 100 } })),
            settle(api.get("/teams/members/", { params: { page_size: 100 } })),
            settle(api.get("/campaigns/", { params: { page_size: 50 } })),
            settle(api.get("/automation/workflows/", { params: { page_size: 50 } })),
            settle(api.get("/chatbot/assets/", { params: { page_size: 100 } })),
        ]).then(([knowledgeBases, members, campaigns, workflows, assets]) =>
            setOptions({
                knowledgeBases,
                agents: members.map((member) => ({ id: member.user, username: member.user_username || member.username })),
                campaigns,
                workflows,
                assets,
                chatbots: bots,
            }),
        );
    }, [bots]);

    const ordered = useMemo(() => inReachOrder(nodes), [nodes]);

    const write = (next) => {
        setNodes(next);
        setDirty(true);
    };

    const addNode = (type) => {
        const id = nextId(nodes, type);
        const node = { id, type, label: "", actions: [], next: "", position: { x: 0, y: nodes.length * 120 } };
        if (type === "CONDITION") {
            node.condition = { field: "last_message", operator: "contains", name: "", value: "" };
            node.true_next = "";
            node.false_next = "";
        }
        if (type === "DELAY") node.seconds = 3;

        // Hooked onto the end of the chain automatically. A node somebody has
        // to remember to connect is a node they will forget to connect, and
        // the API refuses an unreachable one.
        const tail = ordered.filter((entry) => entry.type !== "CONDITION").reverse().find((entry) => !entry.next);
        const next = tail && tail.type !== "END" ? nodes.map((entry) => (entry.id === tail.id ? { ...entry, next: id } : entry)) : nodes;
        write([...next, node]);
        setEditing(node);
    };

    const updateNode = (id, patch) => write(nodes.map((node) => (node.id === id ? { ...node, ...patch } : node)));

    const removeNode = (node) => {
        // Every edge pointing at it is cleared too, so the graph never
        // references a node that is gone.
        write(
            nodes
                .filter((entry) => entry.id !== node.id)
                .map((entry) => ({
                    ...entry,
                    next: entry.next === node.id ? "" : entry.next,
                    true_next: entry.true_next === node.id ? "" : entry.true_next,
                    false_next: entry.false_next === node.id ? "" : entry.false_next,
                })),
        );
    };

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            await api.put(`/chatbot/bots/${selected.id}/flow/`, { flow: { nodes } });
            toast.success("Flow saved as a draft. Publish the bot to make it live.");
            setDirty(false);
            onChanged();
        } catch (requestError) {
            const detail = requestError?.response?.data?.flow;
            setError(Array.isArray(detail) ? detail.join(" ") : detail || apiErrorMessage(requestError, "Could not save the flow."));
        } finally {
            setSaving(false);
        }
    };

    if (!bots.length) {
        return <EmptyState icon={GitBranch} title="No chatbots yet" description="Create a chatbot first." />;
    }

    const viewToggle = (
        <div className="inline-flex rounded-lg border border-ink-200 overflow-hidden" role="group" aria-label="Flow view">
            {[
                { id: "canvas", label: "Canvas", icon: LayoutGrid },
                { id: "list", label: "List", icon: List },
            ].map((entry) => {
                const Icon = entry.icon;
                return (
                    <button
                        key={entry.id}
                        type="button"
                        onClick={() => setView(entry.id)}
                        aria-pressed={view === entry.id}
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-semibold cursor-pointer transition-colors ${
                            view === entry.id ? "bg-brand-600 text-white" : "bg-surface text-ink-600 hover:bg-ink-50"
                        }`}
                    >
                        <Icon size={13} />
                        {entry.label}
                    </button>
                );
            })}
        </div>
    );

    if (view === "canvas" && selected) {
        return (
            <div>
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3">
                    <BotPicker bots={bots} selected={selected} onSelect={onSelectBot} />
                    {viewToggle}
                </div>
                {/*
                 * Keyed on the bot so switching bots remounts rather than
                 * feeding a second graph into a canvas that already owns one --
                 * see the note at the top of ChatbotCanvas.
                 */}
                <ChatbotCanvas
                    key={selected.id}
                    bot={selected}
                    can={can}
                    onChanged={onChanged}
                    onBack={() => setView("list")}
                />
            </div>
        );
    }

    return (
        <div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
                <BotPicker bots={bots} selected={selected} onSelect={onSelectBot} />
                <div className="flex items-center gap-2">
                    {viewToggle}
                    {version && <span className="text-xs text-ink-500">Draft v{version}</span>}
                    {can("CHATBOT_EDIT") && (
                        <Button size="sm" icon={Save} loading={saving} disabled={!dirty} onClick={save}>
                            {dirty ? "Save draft" : "Saved"}
                        </Button>
                    )}
                </div>
            </div>

            {error && (
                <Alert tone="error" className="mb-4">
                    {error}
                </Alert>
            )}

            {loading ? (
                <div className="flex justify-center py-16">
                    <Spinner />
                </div>
            ) : nodes.length === 0 ? (
                <EmptyState
                    icon={GitBranch}
                    title="No flow yet"
                    description="A flow is a conversation you have drawn out: greet, ask what they need, branch on the answer. Rules and the AI still work without one."
                    action={
                        can("CHATBOT_EDIT") && (
                            <Button
                                icon={Plus}
                                onClick={() =>
                                    write([
                                        {
                                            id: "start",
                                            type: "START",
                                            label: "Start",
                                            actions: [],
                                            next: "",
                                            trigger: { ...DEFAULT_TRIGGER },
                                            position: { x: 0, y: 0 },
                                        },
                                    ])
                                }
                            >
                                Start a flow
                            </Button>
                        )
                    }
                />
            ) : (
                <>
                    <div className="space-y-1">
                        {ordered.map((node, index) => {
                            const meta = typeOf(node.type);
                            const Icon = meta.icon;
                            const unreachable =
                                node.type !== "START" &&
                                !nodes.some(
                                    (entry) =>
                                        entry.next === node.id ||
                                        entry.true_next === node.id ||
                                        entry.false_next === node.id,
                                );
                            return (
                                <div key={node.id}>
                                    <Card padded={false} className={unreachable ? "border-amber-300" : ""}>
                                        <div className="flex items-start gap-3 p-4">
                                            <span
                                                className={`shrink-0 w-9 h-9 rounded-lg flex items-center justify-center ${
                                                    node.type === "START"
                                                        ? "bg-brand-50 text-brand-600"
                                                        : node.type === "END"
                                                          ? "bg-ink-100 text-ink-500"
                                                          : node.type === "CONDITION"
                                                            ? "bg-amber-50 text-amber-600"
                                                            : "bg-blue-50 text-blue-600"
                                                }`}
                                            >
                                                <Icon size={17} aria-hidden="true" />
                                            </span>

                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2 flex-wrap">
                                                    <span className="text-sm font-bold text-ink-900">
                                                        {node.label || meta.label}
                                                    </span>
                                                    <code className="text-[11px] text-ink-400">{node.id}</code>
                                                    {unreachable && (
                                                        <span className="text-[11px] font-bold text-amber-700">
                                                            Nothing leads here
                                                        </span>
                                                    )}
                                                </div>
                                                <p className="text-xs text-ink-500 m-0 mt-1">
                                                    {describeNode(node)}
                                                </p>
                                            </div>

                                            {can("CHATBOT_EDIT") && (
                                                <div className="flex gap-1 shrink-0">
                                                    <IconButton
                                                        icon={Pencil}
                                                        title="Edit node"
                                                        onClick={() => setEditing(node)}
                                                    />
                                                    {node.type !== "START" && (
                                                        <IconButton
                                                            icon={Trash2}
                                                            title="Delete node"
                                                            onClick={() => setDeleting(node)}
                                                        />
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                    </Card>

                                    {index < ordered.length - 1 && (
                                        <div className="flex justify-center py-1">
                                            <ArrowRight size={14} className="text-ink-300 rotate-90" aria-hidden="true" />
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>

                    {can("CHATBOT_EDIT") && (
                        <div className="mt-5">
                            <p className="text-xs font-bold text-ink-500 uppercase tracking-wide mb-2">Add a step</p>
                            <div className="flex flex-wrap gap-2">
                                {NODE_TYPES.filter((entry) => entry.id !== "START").map((entry) => (
                                    <Button
                                        key={entry.id}
                                        size="sm"
                                        variant="secondary"
                                        icon={entry.icon}
                                        title={entry.hint}
                                        onClick={() => addNode(entry.id)}
                                    >
                                        {entry.label}
                                    </Button>
                                ))}
                            </div>
                        </div>
                    )}
                </>
            )}

            {editing && (
                <NodeEditor
                    node={nodes.find((entry) => entry.id === editing.id) || editing}
                    nodes={nodes}
                    options={{ ...options, provider: selected?.account_provider }}
                    onChange={(patch) => updateNode(editing.id, patch)}
                    onClose={() => setEditing(null)}
                />
            )}

            {deleting && (
                <ConfirmDialog
                    title={`Delete "${deleting.label || deleting.id}"?`}
                    message="Anything pointing at it will be disconnected."
                    confirmLabel="Delete step"
                    onClose={() => setDeleting(null)}
                    onConfirm={() => {
                        removeNode(deleting);
                        setDeleting(null);
                    }}
                />
            )}
        </div>
    );
}

function describeNode(node) {
    if (node.type === "START") {
        return `Starts ${describeTrigger(triggerOf(node))} → ${node.next || "nothing"}`;
    }
    if (node.type === "CONDITION") {
        const field = CONDITION_FIELDS.find((entry) => entry.id === node.condition?.field)?.label || node.condition?.field;
        const operator = OPERATORS.find((entry) => entry.id === node.condition?.operator)?.label || node.condition?.operator;
        return `If ${field} ${operator} ${node.condition?.value ?? ""} → ${node.true_next || "nothing"}, otherwise → ${
            node.false_next || "nothing"
        }`;
    }
    if (node.type === "DELAY") return `Wait ${node.seconds || 0}s, then → ${node.next || "nothing"}`;
    if (node.type === "END") return "The conversation ends here.";

    const steps = node.actions?.length || 0;
    const summary = steps ? `${steps} ${steps === 1 ? "step" : "steps"}` : "no steps yet";
    return `${summary} → ${node.next || "nothing"}`;
}

/**
 * The Start node's trigger, in the list view's editor.
 *
 * Shares its vocabulary with the canvas through `utils/chatbotGraph` rather
 * than restating it: two screens that describe the same setting differently is
 * how people end up believing there are two settings.
 */
function TriggerFields({ trigger, onChange }) {
    const meta = triggerMeta(trigger.type);
    const set = (patch) => onChange({ trigger: { ...trigger, ...patch } });

    return (
        <>
            <Field
                label="Start this flow when"
                className="mb-4"
                hint="Messages that do not match are left to your rules and the AI."
            >
                <Select
                    value={trigger.type}
                    onChange={(event) => set({ type: event.target.value, pattern: "", case_sensitive: false })}
                >
                    {TRIGGERS.map((entry) => (
                        <option key={entry.id} value={entry.id}>
                            {entry.label}
                        </option>
                    ))}
                </Select>
            </Field>

            {meta.needsPattern && (
                <Field label={meta.patternLabel} hint={meta.patternHint} className="mb-0">
                    <Input
                        value={trigger.pattern}
                        placeholder={meta.placeholder}
                        onChange={(event) => set({ pattern: event.target.value })}
                    />
                </Field>
            )}
        </>
    );
}

function NodeEditor({ node, nodes, options, onChange, onClose }) {
    const meta = typeOf(node.type);
    const targets = nodes.filter((entry) => entry.id !== node.id && entry.type !== "START");

    const NodeSelect = ({ label, value, onSelect, hint }) => (
        <Field label={label} hint={hint}>
            <Select value={value || ""} onChange={(event) => onSelect(event.target.value)}>
                <option value="">— stop here —</option>
                {targets.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                        {entry.label || typeOf(entry.type).label} ({entry.id})
                    </option>
                ))}
            </Select>
        </Field>
    );

    return (
        <Modal
            title={`${meta.label} step`}
            description={meta.hint}
            width="xl"
            onClose={onClose}
            footer={
                <div className="flex justify-end">
                    <Button onClick={onClose}>Done</Button>
                </div>
            }
        >
            <Field label="Name" hint="For you. Customers never see it.">
                <Input
                    value={node.label || ""}
                    onChange={(event) => onChange({ label: event.target.value })}
                    placeholder={meta.label}
                />
            </Field>

            {node.type === "START" && (
                <div className="border border-ink-200 rounded-xl p-4 mb-5 bg-ink-50/40">
                    <TriggerFields trigger={triggerOf(node)} onChange={onChange} />
                </div>
            )}

            {node.type === "DELAY" && (
                <Field label="Wait for" hint="Seconds, 1 to 120. The pause is applied to the next message, not by holding a worker.">
                    <Input
                        type="number"
                        min={1}
                        max={120}
                        value={node.seconds || 3}
                        onChange={(event) => onChange({ seconds: Number(event.target.value) })}
                    />
                </Field>
            )}

            {node.type === "CONDITION" && (
                <div className="border border-ink-200 rounded-xl p-4 mb-5 bg-ink-50/40">
                    <div className="grid sm:grid-cols-2 gap-x-4">
                        <Field label="Look at" className="mb-4">
                            <Select
                                value={node.condition?.field || "last_message"}
                                onChange={(event) =>
                                    onChange({ condition: { ...node.condition, field: event.target.value } })
                                }
                            >
                                {CONDITION_FIELDS.map((entry) => (
                                    <option key={entry.id} value={entry.id}>
                                        {entry.label}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field label="Test" className="mb-4">
                            <Select
                                value={node.condition?.operator || "contains"}
                                onChange={(event) =>
                                    onChange({ condition: { ...node.condition, operator: event.target.value } })
                                }
                            >
                                {OPERATORS.map((entry) => (
                                    <option key={entry.id} value={entry.id}>
                                        {entry.label}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    </div>

                    {node.condition?.field === "variable" && (
                        <Field label="Variable name" className="mb-4">
                            <Input
                                value={node.condition?.name || ""}
                                onChange={(event) =>
                                    onChange({ condition: { ...node.condition, name: event.target.value } })
                                }
                                placeholder="shoe_size"
                            />
                        </Field>
                    )}

                    {!["exists", "not_exists"].includes(node.condition?.operator) && (
                        <Field label="Value" className="mb-0">
                            <Input
                                value={node.condition?.value ?? ""}
                                onChange={(event) =>
                                    onChange({ condition: { ...node.condition, value: event.target.value } })
                                }
                                placeholder="urgent"
                            />
                        </Field>
                    )}
                </div>
            )}

            {meta.actions.length > 0 && (
                <div className="mb-5">
                    <p className="text-xs font-bold text-ink-500 uppercase tracking-wide m-0 mb-3">What happens here</p>
                    <ActionBuilder
                        actions={node.actions || []}
                        onChange={(actions) => onChange({ actions })}
                        allowed={meta.actions}
                        options={options}
                    />
                </div>
            )}

            <div className="pt-4 border-t border-ink-100">
                <p className="text-xs font-bold text-ink-500 uppercase tracking-wide m-0 mb-3">Then go to</p>
                {node.type === "CONDITION" ? (
                    <div className="grid sm:grid-cols-2 gap-x-4">
                        <NodeSelect
                            label="If it's true"
                            value={node.true_next}
                            onSelect={(value) => onChange({ true_next: value })}
                        />
                        <NodeSelect
                            label="If it's false"
                            value={node.false_next}
                            onSelect={(value) => onChange({ false_next: value })}
                        />
                    </div>
                ) : node.type === "END" ? (
                    <p className="text-sm text-ink-500 m-0">Nothing — this ends the conversation.</p>
                ) : (
                    <NodeSelect
                        label="Next step"
                        value={node.next}
                        onSelect={(value) => onChange({ next: value })}
                        hint={
                            node.type === "QUESTION"
                                ? "Reached after the customer answers."
                                : "Leave blank to stop and wait for whatever they say next."
                        }
                    />
                )}
            </div>
        </Modal>
    );
}
