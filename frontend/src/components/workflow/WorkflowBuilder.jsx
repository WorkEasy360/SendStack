import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Background,
    Controls,
    Handle,
    MiniMap,
    Position,
    ReactFlow,
    ReactFlowProvider,
    addEdge,
    applyEdgeChanges,
    applyNodeChanges,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
    Bot,
    Clock,
    Flag,
    GitBranch,
    MessageSquare,
    Play,
    Tag as TagIcon,
    Trash2,
    UserCheck,
} from "lucide-react";
import Button from "../ui/Button";
import { useDarkMode } from "../../utils/theme";
import { Field, Input, Select, Textarea } from "../ui/Field";
import {
    OUTPUTS,
    describeNode,
    fromGraph,
    newNodeId,
    toGraph,
    validateGraph,
} from "../../utils/workflowGraph";

const NODE_META = {
    TRIGGER: { label: "Trigger", icon: Play, tone: "bg-brand-50 border-brand-300 text-brand-700" },
    MESSAGE: { label: "Message", icon: MessageSquare, tone: "bg-blue-50 border-blue-300 text-blue-700" },
    AI_REPLY: { label: "AI reply", icon: Bot, tone: "bg-violet-50 border-violet-300 text-violet-700" },
    WAIT: { label: "Wait", icon: Clock, tone: "bg-amber-50 border-amber-300 text-amber-700" },
    CONDITION: { label: "Condition", icon: GitBranch, tone: "bg-ink-100 border-ink-300 text-ink-700" },
    TAG: { label: "Add tag", icon: TagIcon, tone: "bg-emerald-50 border-emerald-300 text-emerald-700" },
    ASSIGN_AGENT: { label: "Assign agent", icon: UserCheck, tone: "bg-cyan-50 border-cyan-300 text-cyan-700" },
    END: { label: "End", icon: Flag, tone: "bg-ink-100 border-ink-300 text-ink-600" },
};

// The order they appear in the palette: roughly the order somebody building a
// flow reaches for them.
const PALETTE = ["MESSAGE", "AI_REPLY", "WAIT", "CONDITION", "TAG", "ASSIGN_AGENT", "END"];

function WorkflowNode({ id, data, selected }) {
    const node = data.raw;
    const meta = NODE_META[data.nodeType] || NODE_META.MESSAGE;
    const Icon = meta.icon;
    const outputs = OUTPUTS[data.nodeType] || [];

    return (
        <div
            className={`w-[210px] rounded-xl border-2 bg-surface shadow-card transition-shadow ${
                selected ? "border-brand-500 shadow-lg" : "border-ink-200"
            }`}
        >
            {data.nodeType !== "TRIGGER" && (
                <Handle type="target" id="in" position={Position.Left} className="!w-2.5 !h-2.5 !bg-ink-400" />
            )}

            <div className={`flex items-center gap-2 px-3 py-2 rounded-t-[10px] border-b ${meta.tone}`}>
                <Icon size={14} aria-hidden="true" />
                <span className="text-xs font-bold uppercase tracking-wide">{meta.label}</span>
            </div>

            <div className="px-3 py-2.5">
                <p className="text-xs text-ink-600 m-0 line-clamp-3 break-words">{describeNode(node)}</p>
                <p className="text-[10px] text-ink-400 mt-1.5 mb-0 font-mono">{id}</p>
            </div>

            {outputs.map((output, index) => (
                <Handle
                    key={output.handle}
                    type="source"
                    id={output.handle}
                    position={Position.Right}
                    // Two outputs are spread down the right edge so "Yes" and
                    // "No" are distinguishable without reading the labels.
                    style={outputs.length > 1 ? { top: `${35 + index * 30}%` } : undefined}
                    className={`!w-2.5 !h-2.5 ${output.handle === "false" ? "!bg-red-400" : "!bg-brand-500"}`}
                />
            ))}

            {outputs.length > 1 && (
                <div className="absolute -right-1 top-0 h-full flex flex-col justify-center pointer-events-none">
                    <span className="text-[9px] font-bold text-brand-600 translate-x-6 -translate-y-2">Yes</span>
                    <span className="text-[9px] font-bold text-red-500 translate-x-6 translate-y-3">No</span>
                </div>
            )}
        </div>
    );
}

const nodeTypes = { workflow: WorkflowNode };

/**
 * Drag-and-drop editor for a workflow's node graph.
 *
 * `value` is the stored node list and `onChange` is called with a new one on
 * every edit, so the surrounding form keeps owning the data. Positions are
 * written into the nodes themselves, which the backend validator ignores --
 * that is what makes a layout survive being saved and reopened.
 */
function Canvas({ value, onChange, tags = [], agents = [], templates = [] }) {
    const dark = useDarkMode();
    const initial = useMemo(() => toGraph(value), []); // eslint-disable-line react-hooks/exhaustive-deps
    const [nodes, setNodes] = useState(initial.nodes);
    const [edges, setEdges] = useState(initial.edges);
    const [selectedId, setSelectedId] = useState(null);

    // The graph is read from `value` once, on mount (the useMemo above), and
    // is the source of truth from then on. That -- not any re-entrancy guard
    // -- is what stops our own output coming back in as new input and
    // resetting every node's position on each keystroke. A caller that needs
    // to load a different graph remounts this component.
    useEffect(() => {
        onChange(fromGraph(nodes, edges));
    }, [nodes, edges]); // eslint-disable-line react-hooks/exhaustive-deps

    const onNodesChange = useCallback((changes) => setNodes((current) => applyNodeChanges(changes, current)), []);
    const onEdgesChange = useCallback((changes) => setEdges((current) => applyEdgeChanges(changes, current)), []);

    const onConnect = useCallback((connection) => {
        setEdges((current) => {
            // One edge per output: a node cannot go two places at once, so a
            // new connection from the same handle replaces the old one rather
            // than quietly adding a second the graph cannot express.
            const cleaned = current.filter(
                (edge) => !(edge.source === connection.source && edge.sourceHandle === connection.sourceHandle),
            );
            return addEdge(
                { ...connection, type: "smoothstep", markerEnd: { type: "arrowclosed" } },
                cleaned,
            );
        });
    }, []);

    const addNode = (type) => {
        const id = newNodeId(type, nodes.map((node) => node.id));
        const raw = { id, type };
        if (type === "WAIT") raw.minutes = 60;
        if (type === "CONDITION") raw.condition = { field: "has_replied" };
        if (type === "MESSAGE") raw.body = "";

        setNodes((current) => [
            ...current,
            {
                id,
                type: "workflow",
                // Dropped just right of centre so a new node is always in view
                // rather than somewhere the user has to hunt for.
                position: { x: 120 + current.length * 40, y: 60 + (current.length % 4) * 130 },
                data: { raw, nodeType: type },
            },
        ]);
        setSelectedId(id);
    };

    const updateSelected = (patch) => {
        setNodes((current) =>
            current.map((node) =>
                node.id === selectedId ? { ...node, data: { ...node.data, raw: { ...node.data.raw, ...patch } } } : node,
            ),
        );
    };

    const deleteSelected = () => {
        setNodes((current) => current.filter((node) => node.id !== selectedId));
        setEdges((current) => current.filter((edge) => edge.source !== selectedId && edge.target !== selectedId));
        setSelectedId(null);
    };

    const selected = nodes.find((node) => node.id === selectedId) || null;
    const problems = validateGraph(fromGraph(nodes, edges));

    return (
        <div className="border border-ink-200 rounded-card overflow-hidden bg-surface">
            <div className="flex flex-wrap items-center gap-1.5 px-3 py-2 border-b border-ink-200 bg-ink-50">
                <span className="text-xs font-semibold text-ink-500 mr-1">Add:</span>
                {PALETTE.map((type) => {
                    const meta = NODE_META[type];
                    return (
                        <Button key={type} size="xs" variant="secondary" icon={meta.icon} onClick={() => addNode(type)} type="button">
                            {meta.label}
                        </Button>
                    );
                })}
            </div>

            <div className="flex flex-col lg:flex-row">
                <div className="h-[440px] lg:h-[540px] flex-1 min-w-0">
                    <ReactFlow
                        colorMode={dark ? "dark" : "light"}
                        nodes={nodes}
                        edges={edges}
                        nodeTypes={nodeTypes}
                        onNodesChange={onNodesChange}
                        onEdgesChange={onEdgesChange}
                        onConnect={onConnect}
                        onNodeClick={(_event, node) => setSelectedId(node.id)}
                        onPaneClick={() => setSelectedId(null)}
                        onInit={(instance) => {
                            // A frame after mount the dialog has its final
                            // size, so this is the fit that actually lands.
                            requestAnimationFrame(() => instance.fitView({ padding: 0.18 }));
                        }}
                        fitView
                        fitViewOptions={{ padding: 0.18 }}
                        proOptions={{ hideAttribution: false }}
                        deleteKeyCode={null}
                    >
                        <Background gap={16} size={1} color={dark ? "#26324d" : "#e5e7eb"} />
                        <Controls showInteractive={false} />
                        {/* Only once the graph is big enough to get lost in.
                            On a six-node workflow it is a pale box covering
                            canvas that the fit-view button already handles. */}
                        {nodes.length > 8 && (
                            <MiniMap pannable zoomable className="!hidden lg:!block" />
                        )}
                    </ReactFlow>
                </div>

                <aside className="w-full lg:w-72 shrink-0 border-t lg:border-t-0 lg:border-l border-ink-200 p-4 bg-surface lg:max-h-[540px] overflow-y-auto">
                    {selected ? (
                        <NodeInspector
                            node={selected}
                            tags={tags}
                            agents={agents}
                            templates={templates}
                            onChange={updateSelected}
                            onDelete={deleteSelected}
                        />
                    ) : (
                        <div className="text-sm text-ink-500">
                            <p className="font-semibold text-ink-800 m-0 mb-1">Nothing selected</p>
                            <p className="m-0">
                                Click a node to edit it, or drag from the dot on its right edge to the next node to
                                connect them.
                            </p>
                        </div>
                    )}
                </aside>
            </div>

            {problems.length > 0 && (
                <div className="px-3 py-2 border-t border-ink-200 bg-amber-50">
                    <ul className="m-0 pl-4 text-xs text-amber-800">
                        {problems.slice(0, 4).map((problem) => (
                            <li key={problem}>{problem}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

/** What starts a workflow. Mirrors services.TriggerEvent on the backend. */
const TRIGGER_EVENTS = [
    { value: "MANUAL", label: "Only when I start it", hint: "Runs for a contact you pick, from the API or the contact list." },
    { value: "NEW_CONVERSATION", label: "A contact writes in for the first time", hint: "Their very first message on this number." },
    { value: "KEYWORD", label: "A contact sends a keyword", hint: "Matched against the text of their message." },
    { value: "BUTTON", label: "A contact taps a button", hint: "Matched against the id of the button they tapped." },
    { value: "INBOUND_MESSAGE", label: "Any message from a contact", hint: "A catch-all. Runs only when no rule or other workflow answered first." },
];

function TriggerSettings({ raw, onChange }) {
    const trigger = raw.trigger || {};
    const event = trigger.event || "MANUAL";
    const patch = (changes) => onChange({ trigger: { ...trigger, event, ...changes } });
    const selected = TRIGGER_EVENTS.find((option) => option.value === event);

    return (
        <>
            <Field label="Start this workflow when" hint={selected?.hint}>
                <Select value={event} onChange={(e) => patch({ event: e.target.value })}>
                    {TRIGGER_EVENTS.map((option) => (
                        <option key={option.value} value={option.value}>
                            {option.label}
                        </option>
                    ))}
                </Select>
            </Field>

            {event === "KEYWORD" && (
                <>
                    <Field label="Keyword">
                        <Input
                            value={trigger.keyword || ""}
                            placeholder="demo"
                            onChange={(e) => patch({ keyword: e.target.value })}
                        />
                    </Field>
                    <Field label="Match">
                        <Select
                            value={trigger.match_type || "CONTAINS"}
                            onChange={(e) => patch({ match_type: e.target.value })}
                        >
                            <option value="CONTAINS">Message contains it</option>
                            <option value="EXACT">Message is exactly it</option>
                            <option value="STARTS_WITH">Message starts with it</option>
                            <option value="ENDS_WITH">Message ends with it</option>
                        </Select>
                    </Field>
                </>
            )}

            {event === "BUTTON" && (
                <Field label="Button id" hint="The id on the button that offered this option.">
                    <Input
                        value={trigger.button_id || ""}
                        onChange={(e) => patch({ button_id: e.target.value })}
                    />
                </Field>
            )}

            {event !== "MANUAL" && (
                <Field label="If the contact is already in this workflow">
                    <Select
                        value={trigger.allow_restart ? "restart" : "ignore"}
                        onChange={(e) => patch({ allow_restart: e.target.value === "restart" })}
                    >
                        <option value="ignore">Let their current run finish (recommended)</option>
                        <option value="restart">Start another run alongside it</option>
                    </Select>
                </Field>
            )}

            <p className="text-sm text-ink-500 m-0 mt-3">
                Connect this step to the first thing the workflow should do.
                {event !== "MANUAL" && " A keyword or button rule with the same trigger answers first if you have one."}
            </p>
        </>
    );
}

function NodeInspector({ node, tags, agents, templates, onChange, onDelete }) {
    const raw = node.data.raw;
    const type = node.data.nodeType;
    const meta = NODE_META[type];

    return (
        <div>
            <div className="flex items-center justify-between gap-2 mb-4">
                <span className="inline-flex items-center gap-1.5 text-sm font-bold text-ink-900">
                    <meta.icon size={15} aria-hidden="true" />
                    {meta.label}
                </span>
                {type !== "TRIGGER" && (
                    <Button size="xs" variant="ghost" icon={Trash2} onClick={onDelete} type="button">
                        Delete
                    </Button>
                )}
            </div>

            {type === "MESSAGE" && (
                <>
                    <Field label="Template" hint="Leave blank to write the message here instead.">
                        <Select
                            value={raw.template_id || ""}
                            onChange={(event) =>
                                onChange({ template_id: event.target.value ? Number(event.target.value) : undefined })
                            }
                        >
                            <option value="">No template</option>
                            {templates.map((template) => (
                                <option key={template.id} value={template.id}>
                                    {template.name}
                                </option>
                            ))}
                        </Select>
                    </Field>
                    {!raw.template_id && (
                        <Field label="Message" hint="[Name] and other contact fields are filled in per contact.">
                            <Textarea
                                rows={5}
                                value={raw.body || ""}
                                onChange={(event) => onChange({ body: event.target.value })}
                            />
                        </Field>
                    )}
                </>
            )}

            {type === "AI_REPLY" && (
                <p className="text-sm text-ink-500 m-0">
                    The AI agent answers the contact's last message using your knowledge base, then the workflow
                    carries on. It hands over to a person if it cannot answer.
                </p>
            )}

            {type === "WAIT" && (
                <Field label="Wait for (minutes)" hint="1440 is a day.">
                    <Input
                        type="number"
                        min="1"
                        value={raw.minutes ?? ""}
                        onChange={(event) =>
                            onChange({ minutes: event.target.value === "" ? undefined : Number(event.target.value) })
                        }
                    />
                </Field>
            )}

            {type === "CONDITION" && (
                <>
                    <Field label="Check">
                        <Select
                            value={raw.condition?.field || "has_replied"}
                            onChange={(event) =>
                                onChange({ condition: { ...raw.condition, field: event.target.value } })
                            }
                        >
                            <option value="has_replied">Has the contact replied?</option>
                            <option value="button_clicked">Did they tap a button?</option>
                            <option value="has_tag">Do they have a tag?</option>
                        </Select>
                    </Field>
                    {raw.condition?.field === "button_clicked" && (
                        <Field label="Button id" hint="Leave blank to match any button they tapped.">
                            <Input
                                value={raw.condition?.value || ""}
                                onChange={(event) =>
                                    onChange({ condition: { ...raw.condition, value: event.target.value } })
                                }
                            />
                        </Field>
                    )}
                    {raw.condition?.field === "has_tag" && (
                        <Field label="Tag">
                            <Select
                                value={raw.condition?.value || ""}
                                onChange={(event) =>
                                    onChange({
                                        condition: {
                                            ...raw.condition,
                                            value: event.target.value ? Number(event.target.value) : undefined,
                                        },
                                    })
                                }
                            >
                                <option value="">Choose a tag</option>
                                {tags.map((tag) => (
                                    <option key={tag.id} value={tag.id}>
                                        {tag.name}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                </>
            )}

            {type === "TAG" && (
                <Field label="Tag to add">
                    <Select
                        value={raw.tag_id || ""}
                        onChange={(event) => onChange({ tag_id: event.target.value ? Number(event.target.value) : undefined })}
                    >
                        <option value="">Choose a tag</option>
                        {tags.map((tag) => (
                            <option key={tag.id} value={tag.id}>
                                {tag.name}
                            </option>
                        ))}
                    </Select>
                </Field>
            )}

            {type === "ASSIGN_AGENT" && (
                <Field label="Assign to">
                    <Select
                        value={raw.agent_id || ""}
                        onChange={(event) => onChange({ agent_id: event.target.value ? Number(event.target.value) : undefined })}
                    >
                        <option value="">Choose an agent</option>
                        {agents.map((agent) => (
                            <option key={agent.user} value={agent.user}>
                                {agent.username}
                            </option>
                        ))}
                    </Select>
                </Field>
            )}

            {type === "TRIGGER" && <TriggerSettings raw={raw} onChange={onChange} />}

            {type === "END" && <p className="text-sm text-ink-500 m-0">The run finishes here.</p>}
        </div>
    );
}

export default function WorkflowBuilder(props) {
    return (
        <ReactFlowProvider>
            <Canvas {...props} />
        </ReactFlowProvider>
    );
}

