import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
    Background,
    Controls,
    Handle,
    MiniMap,
    Position,
    ReactFlow,
    ReactFlowProvider,
    applyEdgeChanges,
    applyNodeChanges,
    useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
    AlertTriangle,
    AlignLeft,
    Brain,
    CheckCircle2,
    ChevronLeft,
    CircleDot,
    Clock,
    Copy,
    Download,
    FileText,
    GitBranch,
    Hash,
    HelpCircle,
    LayoutGrid,
    MessageSquare,
    MessagesSquare,
    MousePointerClick,
    Play,
    Plus,
    Redo2,
    Regex,
    Rocket,
    Save,
    Sparkles,
    Square,
    Tag,
    Trash2,
    Type,
    Undo2,
    Upload,
    UserCheck,
    Variable,
    Wrench,
    X,
    Zap,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useDarkMode } from "../../utils/theme";
import Alert from "../../components/ui/Alert";
import Button, { IconButton } from "../../components/ui/Button";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import Modal from "../../components/ui/Modal";
import Spinner from "../../components/ui/Spinner";
import { CheckboxField, Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import ActionBuilder from "./ActionBuilder";
import ChatbotPreview from "./ChatbotPreview";
import {
    DEFAULT_TRIGGER,
    TRIGGERS,
    autoLayout,
    buildEdge,
    describeNode,
    describeTrigger,
    fromGraph,
    localProblems,
    newNodeId,
    optionsOf,
    outputsOf,
    toGraph,
    triggerMeta,
    triggerOf,
} from "../../utils/chatbotGraph";

/**
 * The visual flow builder.
 *
 * Three columns — what you can add, what you have drawn, and the settings of
 * the one thing you have selected — over one graph that is **never** the source
 * of truth. Every save posts the whole flow to `PUT /chatbot/bots/:id/flow/`,
 * which validates it and stores it; every publish freezes a version of it. The
 * canvas is a way of writing that JSON, not a second place the conversation
 * lives.
 *
 * The stored graph is loaded once, on mount (`useMemo` below), and the canvas
 * owns it from then on. That, rather than any re-entrancy guard, is what stops
 * our own output arriving back as new input and resetting every node's position
 * on each keystroke — the same approach `components/workflow/WorkflowBuilder`
 * takes, for the same reason. Switching to another bot remounts.
 */

const NODE_TYPES = [
    {
        id: "MESSAGE", label: "Send a message", icon: MessageSquare, group: "Say something",
        actions: ["SEND_MESSAGE"], tone: "blue",
        hint: "Text, with optional buttons.",
    },
    {
        id: "QUESTION", label: "Ask a question", icon: HelpCircle, group: "Say something",
        actions: ["ASK_QUESTION"], tone: "violet",
        hint: "Ask, wait for the answer, then branch on it.",
    },
    {
        id: "MEDIA", label: "Send a file", icon: FileText, group: "Say something",
        actions: ["SEND_MEDIA"], tone: "blue",
        hint: "An image, video, audio file or PDF.",
    },
    {
        id: "AI", label: "AI answers", icon: Brain, group: "Think",
        actions: ["AI_REPLY"], tone: "violet",
        hint: "Let the model write the reply.",
    },
    {
        id: "KNOWLEDGE", label: "Answer from documents", icon: Sparkles, group: "Think",
        actions: ["KNOWLEDGE_ANSWER"], tone: "violet",
        hint: "Retrieve from your knowledge base, then answer.",
    },
    {
        id: "CONDITION", label: "Condition", icon: GitBranch, group: "Decide",
        actions: [], tone: "slate",
        hint: "Branch on something you already know.",
    },
    {
        id: "DELAY", label: "Pause", icon: Clock, group: "Decide",
        actions: [], tone: "amber",
        hint: "Wait before the next message.",
    },
    {
        id: "VARIABLE", label: "Remember a value", icon: Variable, group: "Remember",
        actions: ["SET_VARIABLE"], tone: "emerald",
        hint: "Store something for later in the conversation.",
    },
    {
        id: "TAG", label: "Tag the customer", icon: Tag, group: "Remember",
        actions: ["ADD_TAG", "REMOVE_TAG"], tone: "emerald",
        hint: "Label the contact record.",
    },
    {
        id: "TOOL", label: "Tool or API", icon: Wrench, group: "Do",
        actions: ["CALL_TOOL", "CALL_API", "TRIGGER_CAMPAIGN", "TRIGGER_AUTOMATION"], tone: "cyan",
        hint: "Look something up, or start something.",
    },
    {
        id: "HANDOFF", label: "Hand to a person", icon: UserCheck, group: "Do",
        actions: ["HANDOFF", "ASSIGN_AGENT"], tone: "cyan",
        hint: "Give the conversation to an agent. The bot stops replying.",
    },
    {
        id: "END", label: "End the conversation", icon: Square, group: "Do",
        actions: ["END_CONVERSATION", "SEND_MESSAGE"], tone: "slate",
        hint: "Finish, and close the session.",
    },
];

const START_TYPE = {
    id: "START", label: "Starts when", icon: CircleDot, actions: [], tone: "brand",
    hint: "What makes a conversation enter this flow.",
};

/**
 * An icon per trigger. Kept here rather than in `chatbotGraph` so that module
 * stays free of components — it is imported by tests and by the list view,
 * neither of which wants a dependency on an icon set.
 */
const TRIGGER_ICONS = {
    ANY_MESSAGE: MessagesSquare,
    NEW_CONVERSATION: Zap,
    KEYWORD: Hash,
    CONTAINS: AlignLeft,
    EXACT: Type,
    STARTS_WITH: AlignLeft,
    REGEX: Regex,
    BUTTON: MousePointerClick,
};

const PALETTE_GROUPS = ["Say something", "Think", "Decide", "Remember", "Do"];

const TONES = {
    brand: "bg-brand-50 border-brand-300 text-brand-700",
    blue: "bg-blue-50 border-blue-300 text-blue-700",
    violet: "bg-violet-50 border-violet-300 text-violet-700",
    slate: "bg-ink-100 border-ink-300 text-ink-700",
    amber: "bg-amber-50 border-amber-300 text-amber-700",
    emerald: "bg-emerald-50 border-emerald-300 text-emerald-700",
    cyan: "bg-cyan-50 border-cyan-300 text-cyan-700",
};

const CONDITION_FIELDS = [
    { id: "last_message", label: "The message" },
    { id: "button_clicked", label: "The option tapped" },
    { id: "variable", label: "A remembered value" },
    { id: "contact_tag", label: "The contact's tags" },
    { id: "language", label: "The language" },
    { id: "ai_replies", label: "AI replies so far" },
    { id: "bot_replies", label: "Bot replies so far" },
    { id: "messages_received", label: "Messages received" },
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

const metaFor = (type) => (type === "START" ? START_TYPE : NODE_TYPES.find((entry) => entry.id === type) || NODE_TYPES[0]);

/** A blank node of one type, already carrying the shape its type requires. */
function blankNode(id, type) {
    const node = { id, type, label: "", actions: [], next: "", position: { x: 0, y: 0 } };
    if (type === "START") node.trigger = { ...DEFAULT_TRIGGER };
    if (type === "CONDITION") {
        node.condition = { field: "last_message", operator: "contains", name: "", value: "" };
        node.true_next = "";
        node.false_next = "";
    }
    if (type === "DELAY") node.seconds = 3;
    if (type === "QUESTION") {
        node.routes = [];
        node.actions = [{ type: "ASK_QUESTION", body: "", save_to: "", buttons: [] }];
    }
    return node;
}

// ---------------------------------------------------------------------------
// The node on the canvas
// ---------------------------------------------------------------------------

function FlowNode({ id, data, selected }) {
    const node = data.raw;
    const meta = metaFor(node.type);
    const Icon = meta.icon;
    const outputs = outputsOf(node);
    const problem = data.problem;

    return (
        <div
            className={`w-[240px] rounded-xl border-2 bg-surface shadow-card transition-shadow ${
                selected ? "border-brand-500 shadow-lg" : problem ? "border-red-300" : "border-ink-200"
            }`}
        >
            {node.type !== "START" && (
                <Handle type="target" id="in" position={Position.Left} className="!w-3 !h-3 !bg-ink-400 !border-2 !border-surface" />
            )}

            <div className={`flex items-center gap-2 px-3 py-2 rounded-t-[10px] border-b ${TONES[meta.tone]}`}>
                <Icon size={14} aria-hidden="true" />
                <span className="text-xs font-bold uppercase tracking-wide truncate">
                    {node.label || meta.label}
                </span>
                {problem && <AlertTriangle size={13} className="ml-auto shrink-0 text-red-500" aria-label="Needs attention" />}
            </div>

            <div className="px-3 py-2.5">
                <p className="text-xs text-ink-600 m-0 line-clamp-3 break-words">{describeNode(node)}</p>
                <p className="text-[10px] text-ink-400 mt-1.5 mb-0 font-mono">{id}</p>
            </div>

            {/*
             * One labelled row per output, in the body rather than floating on
             * the border. A question can have four, and four unlabelled dots
             * stacked down an edge is exactly the thing that makes people wire
             * the wrong branch.
             */}
            {outputs.length > 0 && (
                <div className="border-t border-ink-100">
                    {outputs.map((output) => (
                        <div
                            key={output.handle}
                            className="relative flex items-center justify-end gap-1.5 px-3 py-1.5 text-[11px] border-b border-ink-50 last:border-b-0"
                        >
                            <span
                                className={
                                    output.tone === "no"
                                        ? "text-red-500 font-semibold"
                                        : output.tone === "yes"
                                          ? "text-emerald-600 font-semibold"
                                          : output.tone === "default"
                                            ? "text-ink-400"
                                            : "text-ink-600 font-medium"
                                }
                            >
                                {output.label}
                            </span>
                            <Handle
                                type="source"
                                id={output.handle}
                                position={Position.Right}
                                className={`!w-3 !h-3 !border-2 !border-surface ${
                                    output.tone === "no"
                                        ? "!bg-red-400"
                                        : output.tone === "yes"
                                          ? "!bg-emerald-500"
                                          : output.tone === "default"
                                            ? "!bg-ink-300"
                                            : "!bg-brand-500"
                                }`}
                                style={{ position: "absolute", right: -6, top: "50%" }}
                            />
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

const nodeTypes = { chatbot: FlowNode };

// ---------------------------------------------------------------------------
// The builder
// ---------------------------------------------------------------------------

function Canvas({ bot, onBack, onChanged, can }) {
    const dark = useDarkMode();
    const toast = useToast();
    const wrapper = useRef(null);
    const { screenToFlowPosition } = useReactFlow();

    const [loading, setLoading] = useState(true);
    const [nodes, setNodes] = useState([]);
    const [edges, setEdges] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [version, setVersion] = useState(null);
    const [error, setError] = useState("");

    const [saveState, setSaveState] = useState("saved"); // saved | dirty | saving | failed
    const [report, setReport] = useState(null);
    const [showReport, setShowReport] = useState(false);
    const [publishing, setPublishing] = useState(false);
    const [preview, setPreview] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(null);
    const [paletteOpen, setPaletteOpen] = useState(false);
    const [options, setOptions] = useState({});

    // Undo/redo. Snapshots of `{nodes, edges}` rather than a command log: the
    // graph is small, and a diff-based history is a second model of the same
    // thing that gets out of step with the first.
    const history = useRef({ past: [], future: [] });
    const [historySize, setHistorySize] = useState({ past: 0, future: 0 });

    const editable = can("CHATBOT_EDIT");

    // -- loading ------------------------------------------------------------

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        api.get(`/chatbot/bots/${bot.id}/flow/`)
            .then((response) => {
                if (cancelled) return;
                const graph = toGraph(response.data.flow?.nodes || []);
                setNodes(graph.nodes);
                setEdges(graph.edges);
                setVersion(response.data.version);
                setError("");
            })
            .catch((requestError) => {
                if (!cancelled) setError(apiErrorMessage(requestError, "Could not load this flow."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [bot.id]);

    useEffect(() => {
        const settle = (promise) =>
            promise.then((response) => response.data.results ?? response.data ?? []).catch(() => []);
        Promise.all([
            settle(api.get("/ai/knowledge-bases/", { params: { page_size: 100 } })),
            settle(api.get("/teams/members/", { params: { page_size: 100 } })),
            settle(api.get("/campaigns/", { params: { page_size: 50 } })),
            settle(api.get("/automation/workflows/", { params: { page_size: 50 } })),
            settle(api.get("/chatbot/assets/", { params: { page_size: 100 } })),
            settle(api.get("/chatbot/bots/", { params: { page_size: 100 } })),
        ]).then(([knowledgeBases, agents, campaigns, workflows, assets, bots]) => {
            setOptions({ knowledgeBases, agents, campaigns, workflows, assets, bots });
        });
    }, []);

    // -- editing ------------------------------------------------------------

    const flowNodes = useMemo(() => fromGraph(nodes, edges), [nodes, edges]);
    const problems = useMemo(() => localProblems(flowNodes), [flowNodes]);
    const problemByNode = useMemo(() => {
        const map = {};
        problems.forEach((item) => {
            if (item.node_id) map[item.node_id] = item.message;
        });
        return map;
    }, [problems]);

    const selected = flowNodes.find((node) => node.id === selectedId) || null;

    const remember = useCallback(() => {
        history.current.past.push({ nodes, edges });
        if (history.current.past.length > 50) history.current.past.shift();
        history.current.future = [];
        setHistorySize({ past: history.current.past.length, future: 0 });
    }, [nodes, edges]);

    const markDirty = useCallback(() => setSaveState((state) => (state === "saving" ? state : "dirty")), []);

    const onNodesChange = useCallback(
        (changes) => {
            setNodes((current) => applyNodeChanges(changes, current));
            // A drag reports dozens of position changes and one with
            // `dragging: false` at the end. Only the last one is a change worth
            // saving or undoing — the rest are the same gesture in progress.
            if (changes.some((change) => change.type === "position" && change.dragging === false)) markDirty();
            if (changes.some((change) => change.type === "remove")) markDirty();
        },
        [markDirty],
    );

    const onEdgesChange = useCallback(
        (changes) => {
            setEdges((current) => applyEdgeChanges(changes, current));
            if (changes.some((change) => change.type === "remove")) markDirty();
        },
        [markDirty],
    );

    /**
     * What may be joined to what.
     *
     * The rules mirror the server's own graph checks, so the canvas refuses
     * what `flow.validate` would refuse rather than letting somebody draw it
     * and discover on save. Nothing may lead into Start (it is where a
     * conversation enters), nothing may leave End, and a node may not connect
     * to itself.
     */
    const isValidConnection = useCallback(
        (connection) => {
            if (connection.source === connection.target) return false;
            const target = nodes.find((node) => node.id === connection.target);
            const source = nodes.find((node) => node.id === connection.source);
            if (!target || !source) return false;
            if (target.data.raw.type === "START") return false;
            if (source.data.raw.type === "END") return false;
            return true;
        },
        [nodes],
    );

    const onConnect = useCallback(
        (connection) => {
            remember();
            const source = nodes.find((node) => node.id === connection.source);
            const output = outputsOf(source?.data.raw).find((entry) => entry.handle === connection.sourceHandle);
            setEdges((current) => [
                // One edge per handle: a node cannot go two places at once, so
                // a second connection from the same output replaces the first
                // rather than adding one the flow JSON cannot express.
                ...current.filter(
                    (edge) => !(edge.source === connection.source && edge.sourceHandle === connection.sourceHandle),
                ),
                buildEdge(
                    connection.source,
                    connection.sourceHandle,
                    connection.target,
                    output?.label || "",
                    output?.tone || "default",
                ),
            ]);
            markDirty();
        },
        [nodes, remember, markDirty],
    );

    const addNode = useCallback(
        (type, position) => {
            remember();
            const id = newNodeId(flowNodes, type);
            const node = blankNode(id, type);
            setNodes((current) => [
                ...current,
                { id, type: "chatbot", position: position || { x: 80, y: 80 }, data: { raw: node, nodeType: type } },
            ]);
            setSelectedId(id);
            markDirty();
            setPaletteOpen(false);
        },
        [flowNodes, remember, markDirty],
    );

    /**
     * Choose what starts this flow.
     *
     * Two jobs in one action, because from the palette they are the same
     * gesture: a graph with no Start node gets one carrying this trigger, and
     * a graph that already has one has its trigger changed. The alternative —
     * a Start node you add from the palette like any other, and a trigger you
     * then set in the panel — makes the first thing anybody does on an empty
     * canvas a two-step, and there is no such thing as a second Start node for
     * the palette to offer anyway.
     */
    const setTrigger = useCallback(
        (type, position) => {
            remember();
            const existing = nodes.find((node) => node.data.raw.type === "START");

            if (existing) {
                setNodes((current) =>
                    current.map((node) =>
                        node.id === existing.id
                            ? {
                                  ...node,
                                  data: {
                                      ...node.data,
                                      // The pattern is dropped with the type it belonged to: a
                                      // keyword list left behind by switching to "any message"
                                      // would silently come back if somebody switched away again.
                                      raw: { ...node.data.raw, trigger: { type, pattern: "", case_sensitive: false } },
                                  },
                              }
                            : node,
                    ),
                );
                setSelectedId(existing.id);
            } else {
                const id = newNodeId(flowNodes, "START");
                const node = blankNode(id, "START");
                node.trigger = { type, pattern: "", case_sensitive: false };
                setNodes((current) => [
                    ...current,
                    { id, type: "chatbot", position: position || { x: 80, y: 80 }, data: { raw: node, nodeType: "START" } },
                ]);
                setSelectedId(id);
            }

            markDirty();
            setPaletteOpen(false);
        },
        [nodes, flowNodes, remember, markDirty],
    );

    const updateNode = useCallback(
        (id, patch) => {
            setNodes((current) =>
                current.map((node) =>
                    node.id === id ? { ...node, data: { ...node.data, raw: { ...node.data.raw, ...patch } } } : node,
                ),
            );
            markDirty();
        },
        [markDirty],
    );

    /**
     * Drop an edge whose handle no longer exists.
     *
     * Reached when somebody deletes a button from a question that had a branch
     * on it. `fromGraph` would drop the route anyway, but leaving the edge
     * drawn would show an arrow that no longer means anything.
     */
    useEffect(() => {
        setEdges((current) => {
            const kept = current.filter((edge) => {
                const source = nodes.find((node) => node.id === edge.source);
                if (!source) return false;
                return outputsOf(source.data.raw).some((output) => output.handle === edge.sourceHandle);
            });
            return kept.length === current.length ? current : kept;
        });
    }, [nodes]);

    const removeNode = useCallback(
        (id) => {
            remember();
            setNodes((current) => current.filter((node) => node.id !== id));
            setEdges((current) => current.filter((edge) => edge.source !== id && edge.target !== id));
            setSelectedId(null);
            markDirty();
        },
        [remember, markDirty],
    );

    const duplicateNode = useCallback(
        (id) => {
            const source = nodes.find((node) => node.id === id);
            if (!source) return;
            remember();
            const newId = newNodeId(flowNodes, source.data.raw.type);
            // The copy keeps what the node *says* and drops where it *went*:
            // two nodes pointing at the same next step is usually right, but
            // two nodes pointing out of one question's options is not.
            const copy = { ...source.data.raw, id: newId, next: "", true_next: "", false_next: "", routes: [] };
            setNodes((current) => [
                ...current,
                {
                    id: newId,
                    type: "chatbot",
                    position: { x: source.position.x + 40, y: source.position.y + 60 },
                    data: { raw: copy, nodeType: copy.type },
                },
            ]);
            setSelectedId(newId);
            markDirty();
        },
        [nodes, flowNodes, remember, markDirty],
    );

    const undo = useCallback(() => {
        const previous = history.current.past.pop();
        if (!previous) return;
        history.current.future.push({ nodes, edges });
        setNodes(previous.nodes);
        setEdges(previous.edges);
        setHistorySize({ past: history.current.past.length, future: history.current.future.length });
        markDirty();
    }, [nodes, edges, markDirty]);

    const redo = useCallback(() => {
        const next = history.current.future.pop();
        if (!next) return;
        history.current.past.push({ nodes, edges });
        setNodes(next.nodes);
        setEdges(next.edges);
        setHistorySize({ past: history.current.past.length, future: history.current.future.length });
        markDirty();
    }, [nodes, edges, markDirty]);

    const tidy = useCallback(() => {
        remember();
        const laidOut = autoLayout(flowNodes);
        const positions = Object.fromEntries(laidOut.map((node) => [node.id, node.position]));
        setNodes((current) => current.map((node) => ({ ...node, position: positions[node.id] || node.position })));
        markDirty();
    }, [flowNodes, remember, markDirty]);

    // -- saving -------------------------------------------------------------

    const save = useCallback(
        async ({ quiet = false } = {}) => {
            if (!editable) return false;

            // A newly-added message/question is intentionally blank until the
            // author types into it.  Do not autosave that transient state and
            // turn a normal editing moment into a red API error.
            const incomplete = problems.find((item) => item.message.includes("add message text"));
            if (incomplete) {
                setSaveState("failed");
                setError(incomplete.message);
                if (!quiet) toast.error(incomplete.message);
                return false;
            }

            setSaveState("saving");
            try {
                const response = await api.put(`/chatbot/bots/${bot.id}/flow/`, {
                    flow: { nodes: fromGraph(nodes, edges) },
                });
                setVersion(response.data.version);
                setSaveState("saved");
                setError("");
                return true;
            } catch (requestError) {
                setSaveState("failed");
                const message = apiErrorMessage(requestError, "Could not save this flow.");
                setError(message);
                if (!quiet) toast.error(message);
                return false;
            }
        },
        [bot.id, nodes, edges, editable, problems, toast],
    );

    // Autosave, two seconds after the last edit. Deliberately not on every
    // change: a save is a full validation on the server, and one per keystroke
    // would both hammer it and flash "Save failed" at somebody halfway through
    // typing a message body.
    useEffect(() => {
        if (saveState !== "dirty" || !editable) return undefined;
        const timer = setTimeout(() => save({ quiet: true }), 2000);
        return () => clearTimeout(timer);
    }, [saveState, save, editable]);

    // The browser's own "are you sure" — the only thing that can interrupt a
    // tab close, and worth having because two seconds of autosave debounce is
    // two seconds of work that would otherwise go.
    useEffect(() => {
        if (saveState === "saved") return undefined;
        const warn = (event) => {
            event.preventDefault();
            event.returnValue = "";
        };
        window.addEventListener("beforeunload", warn);
        return () => window.removeEventListener("beforeunload", warn);
    }, [saveState]);

    // -- keyboard -----------------------------------------------------------

    useEffect(() => {
        const onKeyDown = (event) => {
            const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) || event.target.isContentEditable;
            const modifier = event.metaKey || event.ctrlKey;

            if (modifier && event.key.toLowerCase() === "s") {
                event.preventDefault();
                save();
                return;
            }
            if (modifier && event.key.toLowerCase() === "z" && !event.shiftKey) {
                if (typing) return;
                event.preventDefault();
                undo();
                return;
            }
            if (modifier && (event.key.toLowerCase() === "y" || (event.key.toLowerCase() === "z" && event.shiftKey))) {
                if (typing) return;
                event.preventDefault();
                redo();
                return;
            }
            if (typing) return;
            if (modifier && event.key.toLowerCase() === "d" && selectedId) {
                event.preventDefault();
                duplicateNode(selectedId);
            }
            if ((event.key === "Delete" || event.key === "Backspace") && selectedId) {
                const node = flowNodes.find((entry) => entry.id === selectedId);
                if (node && node.type !== "START") setConfirmDelete(node);
            }
        };
        window.addEventListener("keydown", onKeyDown);
        return () => window.removeEventListener("keydown", onKeyDown);
    }, [save, undo, redo, duplicateNode, selectedId, flowNodes]);

    // -- validate, publish, import/export ------------------------------------

    const check = useCallback(async () => {
        try {
            const response = await api.post(`/chatbot/bots/${bot.id}/validate/`, {
                flow: { nodes: fromGraph(nodes, edges) },
            });
            setReport(response.data);
            setShowReport(true);
            return response.data;
        } catch (requestError) {
            toast.error(apiErrorMessage(requestError, "Could not check this flow."));
            return null;
        }
    }, [bot.id, nodes, edges, toast]);

    const publish = useCallback(async () => {
        setPublishing(true);
        try {
            // Saved first, always: publish freezes what is *stored*, so
            // publishing an unsaved canvas would put the previous graph live
            // and tell somebody it was the one they were looking at.
            const saved = await save();
            if (!saved) return;

            await api.post(`/chatbot/bots/${bot.id}/publish/`, {});
            toast.success("Published. This bot is now answering on its number.");
            setReport(null);
            onChanged?.();
        } catch (requestError) {
            const data = requestError?.response?.data;
            if (data?.problems) {
                setReport(data);
                setShowReport(true);
            } else {
                toast.error(apiErrorMessage(requestError, "Could not publish this bot."));
            }
        } finally {
            setPublishing(false);
        }
    }, [bot.id, save, toast, onChanged]);

    const exportFlow = useCallback(async () => {
        try {
            const response = await api.get(`/chatbot/bots/${bot.id}/export/`);
            const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `${bot.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-flow.json`;
            link.click();
            URL.revokeObjectURL(url);
        } catch (requestError) {
            toast.error(apiErrorMessage(requestError, "Could not export this flow."));
        }
    }, [bot.id, bot.name, toast]);

    const importFlow = useCallback(
        async (file) => {
            try {
                const payload = JSON.parse(await file.text());
                const response = await api.post(`/chatbot/bots/${bot.id}/import/`, payload);
                const graph = toGraph(response.data.flow.nodes);
                remember();
                setNodes(graph.nodes);
                setEdges(graph.edges);
                setSelectedId(null);
                setSaveState("saved");
                toast.success(`Imported ${response.data.nodes} nodes.`);
            } catch (requestError) {
                toast.error(
                    requestError instanceof SyntaxError
                        ? "That file is not valid JSON."
                        : apiErrorMessage(requestError, "Could not import that flow."),
                );
            }
        },
        [bot.id, remember, toast],
    );

    // -- drag and drop from the palette --------------------------------------

    const onDrop = useCallback(
        (event) => {
            event.preventDefault();
            const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
            const trigger = event.dataTransfer.getData("application/chatbot-trigger");
            if (trigger) {
                setTrigger(trigger, position);
                return;
            }
            const type = event.dataTransfer.getData("application/chatbot-node");
            if (!type) return;
            addNode(type, position);
        },
        [addNode, setTrigger, screenToFlowPosition],
    );

    const startTrigger = useMemo(() => {
        const start = flowNodes.find((node) => node.type === "START");
        return start ? triggerOf(start) : null;
    }, [flowNodes]);

    const decorated = useMemo(
        () => nodes.map((node) => ({ ...node, data: { ...node.data, problem: problemByNode[node.id] } })),
        [nodes, problemByNode],
    );

    if (loading) {
        return (
            <div className="flex justify-center py-20">
                <Spinner />
            </div>
        );
    }

    return (
        <div className="flex flex-col h-[calc(100vh-9rem)] min-h-[520px] -mx-4 sm:-mx-6 border-y border-ink-200 bg-ink-50">
            <Toolbar
                bot={bot}
                version={version}
                saveState={saveState}
                problems={problems}
                historySize={historySize}
                editable={editable}
                canPublish={can("CHATBOT_PUBLISH")}
                publishing={publishing}
                onBack={onBack}
                onUndo={undo}
                onRedo={redo}
                onTidy={tidy}
                onSave={() => save()}
                onCheck={check}
                onPublish={publish}
                onPreview={() => setPreview(true)}
                onExport={exportFlow}
                onImport={importFlow}
                onOpenPalette={() => setPaletteOpen(true)}
            />

            {error && (
                <Alert tone="error" className="m-3 mb-0">
                    {error}
                </Alert>
            )}

            <div className="flex-1 flex min-h-0">
                <Palette
                    open={paletteOpen}
                    editable={editable}
                    trigger={startTrigger}
                    onClose={() => setPaletteOpen(false)}
                    onAdd={(type) => addNode(type)}
                    onTrigger={(type) => setTrigger(type)}
                />

                <div className="flex-1 min-w-0 relative" ref={wrapper} onDrop={onDrop} onDragOver={(event) => event.preventDefault()}>
                    {/*
                      * An empty canvas is the one screen that cannot say what to do
                      * next by showing you what is on it. `pointer-events-none` so
                      * this never eats the drop it is asking for.
                      */}
                    {nodes.length === 0 && (
                        <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none px-6">
                            <div className="text-center max-w-xs">
                                <CircleDot size={26} className="mx-auto text-ink-300" aria-hidden="true" />
                                <p className="text-sm font-semibold text-ink-700 mt-2 mb-1">Choose what starts this flow</p>
                                <p className="text-xs text-ink-500 m-0">
                                    Pick a trigger on the left — a new message, a keyword, a tapped option —
                                    and the Start step appears here.
                                </p>
                            </div>
                        </div>
                    )}
                    <ReactFlow
                        colorMode={dark ? "dark" : "light"}
                        nodes={decorated}
                        edges={edges}
                        nodeTypes={nodeTypes}
                        onNodesChange={editable ? onNodesChange : undefined}
                        onEdgesChange={editable ? onEdgesChange : undefined}
                        onConnect={editable ? onConnect : undefined}
                        isValidConnection={isValidConnection}
                        onNodeClick={(_, node) => setSelectedId(node.id)}
                        onPaneClick={() => setSelectedId(null)}
                        nodesDraggable={editable}
                        nodesConnectable={editable}
                        elementsSelectable
                        multiSelectionKeyCode="Shift"
                        deleteKeyCode={null}
                        fitView
                        minZoom={0.2}
                        maxZoom={1.75}
                        proOptions={{ hideAttribution: false }}
                    >
                        <Background gap={18} size={1} />
                        <Controls showInteractive={false} />
                        <MiniMap pannable zoomable className="!hidden sm:!block" />
                    </ReactFlow>
                </div>

                <Properties
                    node={selected}
                    options={options}
                    editable={editable}
                    problem={selected ? problemByNode[selected.id] : ""}
                    onChange={(patch) => selected && updateNode(selected.id, patch)}
                    onDuplicate={() => selected && duplicateNode(selected.id)}
                    onDelete={() => selected && setConfirmDelete(selected)}
                    onClose={() => setSelectedId(null)}
                />
            </div>

            {showReport && report && (
                <ReportDialog report={report} onClose={() => setShowReport(false)} onSelect={(nodeId) => {
                    setSelectedId(nodeId);
                    setShowReport(false);
                }} />
            )}

            {preview && (
                <ChatbotPreview
                    bot={bot}
                    saveState={saveState}
                    trigger={startTrigger}
                    onSave={() => save()}
                    onClose={() => setPreview(false)}
                />
            )}

            {confirmDelete && (
                <ConfirmDialog
                    title={`Delete ${confirmDelete.label || metaFor(confirmDelete.type).label}?`}
                    message="Any connections into or out of this node go with it."
                    confirmLabel="Delete"
                    tone="danger"
                    onConfirm={() => {
                        removeNode(confirmDelete.id);
                        setConfirmDelete(null);
                    }}
                    onClose={() => setConfirmDelete(null)}
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

const SAVE_LABEL = {
    saved: "Saved",
    dirty: "Unsaved changes",
    saving: "Saving…",
    failed: "Save failed",
};

function Toolbar({
    bot, version, saveState, problems, historySize, editable, canPublish, publishing,
    onBack, onUndo, onRedo, onTidy, onSave, onCheck, onPublish, onPreview, onExport, onImport, onOpenPalette,
}) {
    const fileInput = useRef(null);

    return (
        <div className="flex items-center gap-2 px-3 py-2 bg-surface border-b border-ink-200 overflow-x-auto">
            <IconButton icon={ChevronLeft} onClick={onBack} title="Back to the list view" aria-label="Back to the list view" />

            <div className="min-w-0">
                <p className="text-sm font-semibold text-ink-900 m-0 truncate">{bot.name}</p>
                <p className="text-[11px] text-ink-500 m-0">
                    {bot.status === "PUBLISHED" ? "Published" : bot.status === "PAUSED" ? "Paused" : "Draft"}
                    {version ? ` · editing v${version}` : ""}
                </p>
            </div>

            <button
                type="button"
                onClick={onOpenPalette}
                className="lg:hidden ml-2 inline-flex items-center gap-1 px-2 py-1.5 rounded-lg border border-ink-200 text-xs font-semibold text-ink-700 cursor-pointer"
            >
                <Plus size={14} /> Add
            </button>

            <div className="ml-auto flex items-center gap-1.5 shrink-0">
                <span
                    className={`hidden sm:inline text-[11px] font-medium mr-1 ${
                        saveState === "failed" ? "text-red-600" : saveState === "saved" ? "text-ink-400" : "text-amber-600"
                    }`}
                >
                    {SAVE_LABEL[saveState]}
                </span>

                {problems.length > 0 && (
                    <span className="hidden md:inline-flex items-center gap-1 text-[11px] font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5">
                        <AlertTriangle size={11} /> {problems.length}
                    </span>
                )}

                <IconButton icon={Undo2} onClick={onUndo} disabled={!editable || !historySize.past} title="Undo (Ctrl+Z)" aria-label="Undo" />
                <IconButton icon={Redo2} onClick={onRedo} disabled={!editable || !historySize.future} title="Redo (Ctrl+Shift+Z)" aria-label="Redo" />
                <IconButton icon={LayoutGrid} onClick={onTidy} disabled={!editable} title="Tidy up the layout" aria-label="Auto layout" />
                <IconButton icon={Download} onClick={onExport} title="Export as JSON" aria-label="Export" />
                <IconButton
                    icon={Upload}
                    onClick={() => fileInput.current?.click()}
                    disabled={!editable}
                    title="Import from JSON"
                    aria-label="Import"
                />
                <input
                    ref={fileInput}
                    type="file"
                    accept="application/json,.json"
                    className="hidden"
                    onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) onImport(file);
                        event.target.value = "";
                    }}
                />

                <Button variant="secondary" size="sm" icon={Play} onClick={onPreview}>
                    <span className="hidden sm:inline">Test</span>
                </Button>
                <Button variant="secondary" size="sm" icon={CheckCircle2} onClick={onCheck}>
                    <span className="hidden md:inline">Check</span>
                </Button>
                <Button variant="secondary" size="sm" icon={Save} onClick={onSave} disabled={!editable}>
                    <span className="hidden sm:inline">Save</span>
                </Button>
                <Button
                    size="sm"
                    icon={Rocket}
                    onClick={onPublish}
                    loading={publishing}
                    disabled={!canPublish}
                    title={canPublish ? "Freeze this flow and put it live" : "You do not have permission to publish"}
                >
                    Publish
                </Button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

function Palette({ open, editable, trigger, onClose, onAdd, onTrigger }) {
    const body = (
        <div className="p-3 space-y-4">
            {/*
              * Triggers come first, and are a different kind of thing to
              * everything below them: a step is added, a trigger is *chosen*.
              * One flow has exactly one, so these behave like a radio group
              * rather than a list of things to drag on — the current one is
              * marked, and picking another replaces it.
              */}
            <div>
                <p className="text-[10px] font-bold uppercase tracking-wider text-ink-400 mb-1.5 px-1">
                    Start this flow when…
                </p>
                <div className="space-y-1">
                    {TRIGGERS.map((entry) => {
                        const Icon = TRIGGER_ICONS[entry.id] || CircleDot;
                        const chosen = trigger?.type === entry.id;
                        return (
                            <button
                                key={entry.id}
                                type="button"
                                draggable={editable}
                                onDragStart={(event) => {
                                    event.dataTransfer.setData("application/chatbot-trigger", entry.id);
                                    event.dataTransfer.effectAllowed = "move";
                                }}
                                onClick={() => editable && onTrigger(entry.id)}
                                disabled={!editable}
                                title={entry.hint}
                                aria-pressed={chosen}
                                className={`w-full flex items-start gap-2 px-2 py-2 rounded-lg text-left border disabled:opacity-50 disabled:cursor-not-allowed transition-colors ${
                                    chosen
                                        ? "border-brand-300 bg-brand-50"
                                        : "border-transparent hover:border-ink-200 hover:bg-ink-50 cursor-grab active:cursor-grabbing"
                                }`}
                            >
                                <span className={`shrink-0 mt-0.5 w-6 h-6 rounded-md border flex items-center justify-center ${TONES.brand}`}>
                                    <Icon size={13} />
                                </span>
                                <span className="min-w-0">
                                    <span className="block text-xs font-semibold text-ink-800">{entry.label}</span>
                                    <span className="block text-[10px] text-ink-500 leading-snug">{entry.hint}</span>
                                </span>
                                {chosen && <CheckCircle2 size={13} className="ml-auto shrink-0 mt-0.5 text-brand-600" />}
                            </button>
                        );
                    })}
                </div>
                {!trigger && (
                    <p className="text-[10px] text-ink-500 px-1 mt-1.5 mb-0">
                        Pick one to place the Start step and begin.
                    </p>
                )}
            </div>

            <div className="border-t border-ink-100 pt-3 space-y-4">
                {PALETTE_GROUPS.map((group) => (
                    <div key={group}>
                        <p className="text-[10px] font-bold uppercase tracking-wider text-ink-400 mb-1.5 px-1">{group}</p>
                        <div className="space-y-1">
                            {NODE_TYPES.filter((entry) => entry.group === group).map((entry) => {
                                const Icon = entry.icon;
                                return (
                                    <button
                                        key={entry.id}
                                        type="button"
                                        draggable={editable}
                                        onDragStart={(event) => {
                                            event.dataTransfer.setData("application/chatbot-node", entry.id);
                                            event.dataTransfer.effectAllowed = "move";
                                        }}
                                        onClick={() => editable && onAdd(entry.id)}
                                        disabled={!editable}
                                        title={entry.hint}
                                        className="w-full flex items-start gap-2 px-2 py-2 rounded-lg text-left border border-transparent hover:border-ink-200 hover:bg-ink-50 disabled:opacity-50 disabled:cursor-not-allowed cursor-grab active:cursor-grabbing transition-colors"
                                    >
                                        <span className={`shrink-0 mt-0.5 w-6 h-6 rounded-md border flex items-center justify-center ${TONES[entry.tone]}`}>
                                            <Icon size={13} />
                                        </span>
                                        <span className="min-w-0">
                                            <span className="block text-xs font-semibold text-ink-800">{entry.label}</span>
                                            <span className="block text-[10px] text-ink-500 leading-snug">{entry.hint}</span>
                                        </span>
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                ))}
            </div>
            <p className="text-[10px] text-ink-400 px-1 pt-1 border-t border-ink-100">
                Drag onto the canvas, or click to drop one in the corner.
            </p>
        </div>
    );

    return (
        <>
            <aside className="hidden lg:block w-60 shrink-0 bg-surface border-r border-ink-200 overflow-y-auto">{body}</aside>

            {open && (
                <div className="lg:hidden fixed inset-0 z-40 flex">
                    <button type="button" className="flex-1 bg-black/40 cursor-default" onClick={onClose} aria-label="Close" />
                    <div className="w-72 max-w-[85vw] bg-surface overflow-y-auto">
                        <div className="flex items-center justify-between px-3 py-2 border-b border-ink-200">
                            <p className="text-sm font-semibold m-0">Triggers and steps</p>
                            <IconButton icon={X} onClick={onClose} aria-label="Close" />
                        </div>
                        {body}
                    </div>
                </div>
            )}
        </>
    );
}

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

function Properties({ node, options, editable, problem, onChange, onDuplicate, onDelete, onClose }) {
    if (!node) {
        return (
            <aside className="hidden xl:flex w-80 shrink-0 bg-surface border-l border-ink-200 items-center justify-center p-6">
                <p className="text-xs text-ink-400 text-center m-0">
                    Select a step to edit what it says and does.
                </p>
            </aside>
        );
    }

    const meta = metaFor(node.type);
    const Icon = meta.icon;

    const body = (
        <div className="p-3 space-y-3">
            {problem && (
                <Alert tone="warning" className="text-xs">
                    {problem}
                </Alert>
            )}

            <Field label="Name on the canvas" hint="Just for you — the customer never sees it.">
                <Input
                    value={node.label || ""}
                    placeholder={meta.label}
                    disabled={!editable}
                    onChange={(event) => onChange({ label: event.target.value })}
                />
            </Field>

            {node.type === "START" && (
                <TriggerEditor trigger={triggerOf(node)} editable={editable} onChange={onChange} />
            )}

            {node.type === "CONDITION" && (
                <ConditionEditor condition={node.condition || {}} editable={editable} onChange={onChange} />
            )}

            {node.type === "DELAY" && (
                <Field label="Pause for" hint="Between 1 and 120 seconds. The pause applies to the next message.">
                    <Input
                        type="number"
                        min={1}
                        max={120}
                        value={node.seconds ?? 3}
                        disabled={!editable}
                        onChange={(event) => onChange({ seconds: Number(event.target.value) || 1 })}
                    />
                </Field>
            )}

            {meta.actions.length > 0 && (
                <div className="pt-1 border-t border-ink-100">
                    <ActionBuilder
                        actions={node.actions || []}
                        onChange={(actions) => onChange({ actions })}
                        allowed={meta.actions}
                        options={options}
                    />
                </div>
            )}

            {node.type === "QUESTION" && <QuestionOptions node={node} editable={editable} onChange={onChange} />}

            {node.type !== "START" && (
                <div className="flex gap-2 pt-2 border-t border-ink-100">
                    <Button variant="secondary" size="sm" icon={Copy} onClick={onDuplicate} disabled={!editable}>
                        Duplicate
                    </Button>
                    <Button variant="danger" size="sm" icon={Trash2} onClick={onDelete} disabled={!editable}>
                        Delete
                    </Button>
                </div>
            )}
        </div>
    );

    return (
        <aside className="w-full sm:w-80 shrink-0 bg-surface border-l border-ink-200 overflow-y-auto fixed sm:static inset-y-0 right-0 z-30 sm:z-auto shadow-xl sm:shadow-none">
            <div className={`flex items-center gap-2 px-3 py-2 border-b ${TONES[meta.tone]}`}>
                <Icon size={14} />
                <span className="text-xs font-bold uppercase tracking-wide">{meta.label}</span>
                <IconButton icon={X} onClick={onClose} className="ml-auto" aria-label="Close settings" />
            </div>
            {body}
        </aside>
    );
}

/**
 * What each of a question's options leads to.
 *
 * Read-only on purpose: an option's destination is set by dragging its handle
 * on the canvas, and offering a dropdown here as well would be two ways to
 * express one thing that can disagree. What this panel adds is the *reverse*
 * view — which options exist and which are still unwired — because that is the
 * question somebody has while looking at the settings rather than the arrows.
 */
function QuestionOptions({ node, editable, onChange }) {
    const ask = (node.actions || []).find((action) => action.type === "ASK_QUESTION");
    const buttons = optionsOf(node);
    const routed = new Set((node.routes || []).map((route) => route.value));

    return (
        <div className="pt-2 border-t border-ink-100 space-y-2">
            <p className="text-[10px] font-bold uppercase tracking-wider text-ink-400 m-0">Where the options go</p>

            {buttons.length === 0 ? (
                <p className="text-xs text-ink-500 m-0">
                    Add reply buttons above to branch on what the customer picks. Without them the answer is
                    saved and the flow carries on down the default path.
                </p>
            ) : (
                <ul className="space-y-1 list-none p-0 m-0">
                    {buttons.map((button) => (
                        <li key={button.id} className="flex items-center gap-2 text-xs">
                            <span className="w-2 h-2 rounded-full bg-brand-500 shrink-0" />
                            <span className="font-medium text-ink-800 truncate">{button.title}</span>
                            <span
                                className={`ml-auto shrink-0 text-[10px] font-semibold ${
                                    routed.has(button.id) ? "text-emerald-600" : node.next ? "text-ink-400" : "text-red-500"
                                }`}
                            >
                                {routed.has(button.id) ? "connected" : node.next ? "default path" : "nowhere"}
                            </span>
                        </li>
                    ))}
                </ul>
            )}

            {ask && (
                <Field
                    label="Save the answer as"
                    hint="Use it later as {{ the name you type }}."
                >
                    <Input
                        value={ask.save_to || ""}
                        placeholder="e.g. topic"
                        disabled={!editable}
                        onChange={(event) =>
                            onChange({
                                actions: (node.actions || []).map((action) =>
                                    action.type === "ASK_QUESTION"
                                        ? { ...action, save_to: event.target.value }
                                        : action,
                                ),
                            })
                        }
                    />
                </Field>
            )}
        </div>
    );
}

/**
 * What starts the flow.
 *
 * The pattern field is cleared when the type changes, deliberately: keeping
 * "price, cost" behind a switch to "any message" means the words are still in
 * the saved JSON, invisible, ready to reappear. The server drops them for the
 * same reason.
 */
function TriggerEditor({ trigger, editable, onChange }) {
    const meta = triggerMeta(trigger.type);
    const set = (patch) => onChange({ trigger: { ...trigger, ...patch } });

    return (
        <div className="space-y-2">
            <Field
                label="Start this flow when"
                hint="Messages that do not match are left to your rules and the AI."
            >
                <Select
                    value={trigger.type}
                    disabled={!editable}
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
                <>
                    <Field label={meta.patternLabel} hint={meta.patternHint}>
                        <Input
                            value={trigger.pattern}
                            placeholder={meta.placeholder}
                            disabled={!editable}
                            onChange={(event) => set({ pattern: event.target.value })}
                        />
                    </Field>

                    {trigger.type !== "BUTTON" && (
                        <CheckboxField
                            label="Match case exactly"
                            description="Off means 'Refund' and 'refund' both start the flow."
                            checked={trigger.case_sensitive}
                            disabled={!editable}
                            onChange={(event) => set({ case_sensitive: event.target.checked })}
                        />
                    )}
                </>
            )}

            <p className="text-[11px] text-ink-500 m-0">
                Starts {describeTrigger(trigger)}. A conversation already part-way through this flow
                carries on regardless.
            </p>
        </div>
    );
}

function ConditionEditor({ condition, editable, onChange }) {
    const set = (patch) => onChange({ condition: { ...condition, ...patch } });
    const needsValue = !["exists", "not_exists"].includes(condition.operator);

    return (
        <div className="space-y-2">
            <Field label="Test">
                <Select
                    value={condition.field || "last_message"}
                    disabled={!editable}
                    onChange={(event) => set({ field: event.target.value })}
                >
                    {CONDITION_FIELDS.map((entry) => (
                        <option key={entry.id} value={entry.id}>
                            {entry.label}
                        </option>
                    ))}
                </Select>
            </Field>

            {condition.field === "variable" && (
                <Field label="Which value">
                    <Input
                        value={condition.name || ""}
                        placeholder="e.g. budget"
                        disabled={!editable}
                        onChange={(event) => set({ name: event.target.value })}
                    />
                </Field>
            )}

            <Field label="Comparison">
                <Select
                    value={condition.operator || "contains"}
                    disabled={!editable}
                    onChange={(event) => set({ operator: event.target.value })}
                >
                    {OPERATORS.map((entry) => (
                        <option key={entry.id} value={entry.id}>
                            {entry.label}
                        </option>
                    ))}
                </Select>
            </Field>

            {needsValue && (
                <Field label="Value">
                    <Input
                        value={condition.value ?? ""}
                        disabled={!editable}
                        onChange={(event) => set({ value: event.target.value })}
                    />
                </Field>
            )}

            <p className="text-[10px] text-ink-400 m-0">
                The <span className="font-semibold text-emerald-600">Yes</span> handle runs when this is true, the{" "}
                <span className="font-semibold text-red-500">No</span> handle when it is not.
            </p>
        </div>
    );
}

// ---------------------------------------------------------------------------
// The publish report
// ---------------------------------------------------------------------------

function ReportDialog({ report, onClose, onSelect }) {
    return (
        <Modal
            title={report.ok ? "Ready to publish" : "This flow cannot be published yet"}
            description={
                report.ok
                    ? "Everything that must be set is set."
                    : "Fix the problems below, then publish. Warnings do not block you."
            }
            onClose={onClose}
            footer={<Button onClick={onClose}>Close</Button>}
        >
            {report.problems.length === 0 && report.warnings.length === 0 && (
                <p className="text-sm text-ink-600 m-0">Nothing to fix.</p>
            )}

            {report.problems.length > 0 && (
                <>
                    <p className="text-xs font-bold uppercase tracking-wider text-red-600 mb-2 mt-0">
                        Problems ({report.problems.length})
                    </p>
                    <ul className="space-y-1.5 list-none p-0 m-0 mb-4">
                        {report.problems.map((item, index) => (
                            <Finding key={index} item={item} tone="problem" onSelect={onSelect} />
                        ))}
                    </ul>
                </>
            )}

            {report.warnings.length > 0 && (
                <>
                    <p className="text-xs font-bold uppercase tracking-wider text-amber-600 mb-2 mt-0">
                        Worth checking ({report.warnings.length})
                    </p>
                    <ul className="space-y-1.5 list-none p-0 m-0">
                        {report.warnings.map((item, index) => (
                            <Finding key={index} item={item} tone="warning" onSelect={onSelect} />
                        ))}
                    </ul>
                </>
            )}
        </Modal>
    );
}

function Finding({ item, tone, onSelect }) {
    const clickable = Boolean(item.node_id);
    return (
        <li
            className={`flex gap-2 text-sm rounded-lg px-2.5 py-2 border ${
                tone === "problem" ? "bg-red-50 border-red-200" : "bg-amber-50 border-amber-200"
            } ${clickable ? "cursor-pointer hover:brightness-98" : ""}`}
            onClick={() => clickable && onSelect(item.node_id)}
        >
            <AlertTriangle size={14} className={`shrink-0 mt-0.5 ${tone === "problem" ? "text-red-500" : "text-amber-500"}`} />
            <span className="min-w-0">
                <span className="block text-ink-800">{item.message}</span>
                {item.node_label && (
                    <span className="block text-[11px] text-ink-500 mt-0.5">
                        {item.node_label} — click to open
                    </span>
                )}
            </span>
        </li>
    );
}

export default function ChatbotCanvas(props) {
    // The provider must sit outside the component using `useReactFlow`, which
    // is why this wrapper exists rather than the hook being called inside a
    // provider rendered by the same component.
    return (
        <ReactFlowProvider>
            <Canvas {...props} />
        </ReactFlowProvider>
    );
}
