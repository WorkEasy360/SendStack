/**
 * Translating between the chatbot's stored flow and the canvas's graph.
 *
 * The backend stores a node's outgoing edges *on the node* — `next`,
 * `true_next`, `false_next`, and a question's `routes` — because that is what
 * the engine walks. React Flow wants edges as their own list. Neither shape is
 * wrong; this module is the one place that knows both, so no component ever
 * builds an edge by hand and there is exactly one definition of what a handle
 * on a node means.
 *
 * The direction that matters is `fromGraph`. Everything a person draws becomes
 * flow JSON here and is then validated by the server, which is the only
 * authority — the canvas never decides what is runnable, it just makes drawing
 * something unrunnable harder.
 */

/**
 * What can start a flow, mirroring `flow.TriggerType` on the server.
 *
 * The labels live here rather than in the canvas because two screens show
 * them — the builder's palette and the list view's node editor — and a trigger
 * described one way in one and another way in the other is the kind of drift
 * that makes people think they are two different settings.
 *
 * `needsPattern` is the only behaviour in this table: it decides whether the
 * editor asks for something to match, and it is the same set the server
 * refuses to save empty.
 */
export const TRIGGERS = [
    {
        id: "ANY_MESSAGE",
        label: "New message received",
        hint: "Any message the customer sends starts this flow.",
    },
    {
        id: "NEW_CONVERSATION",
        label: "A conversation starts",
        hint: "Only the first message of a new conversation.",
    },
    {
        id: "KEYWORD",
        label: "Message matches a keyword",
        hint: "Any of these words, matched as whole words.",
        needsPattern: true,
        patternLabel: "Words that start it",
        patternHint: "Separate them with commas. 'price' will not fire on 'surprise'.",
        placeholder: "price, pricing, how much",
    },
    {
        id: "CONTAINS",
        label: "Message contains",
        hint: "The text appears anywhere in the message.",
        needsPattern: true,
        patternLabel: "Text to look for",
        placeholder: "order status",
    },
    {
        id: "EXACT",
        label: "Message is exactly",
        hint: "The whole message is this and nothing else.",
        needsPattern: true,
        patternLabel: "The exact message",
        placeholder: "menu",
    },
    {
        id: "STARTS_WITH",
        label: "Message starts with",
        hint: "Useful for commands like '/help'.",
        needsPattern: true,
        patternLabel: "Opening text",
        placeholder: "/order",
    },
    {
        id: "REGEX",
        label: "Message matches a pattern",
        hint: "A regular expression, for when the others are not precise enough.",
        needsPattern: true,
        patternLabel: "Pattern",
        patternHint: "Checked when you save — an invalid pattern is refused.",
        placeholder: "^(hi|hello|hey)\\b",
    },
    {
        id: "BUTTON",
        label: "An option was tapped",
        hint: "The customer chose a reply button, by tapping it or by its number.",
        needsPattern: true,
        patternLabel: "Option id",
        patternHint: "The id, not the label — a reworded button keeps working.",
        placeholder: "talk_to_sales",
    },
];

export const DEFAULT_TRIGGER = { type: "ANY_MESSAGE", pattern: "", case_sensitive: false };

/** One entry from `TRIGGERS`, never undefined. */
export const triggerMeta = (type) => TRIGGERS.find((entry) => entry.id === type) || TRIGGERS[0];

/** The trigger on a Start node, defaulted the way the server defaults it. */
export function triggerOf(node) {
    const trigger = node?.trigger;
    if (!trigger?.type) return { ...DEFAULT_TRIGGER };
    return {
        type: trigger.type,
        pattern: trigger.pattern || "",
        case_sensitive: Boolean(trigger.case_sensitive),
    };
}

/** One line naming what starts the flow, phrased to follow "Starts ". */
export function describeTrigger(trigger) {
    const meta = triggerMeta(trigger?.type);
    const pattern = (trigger?.pattern || "").trim();

    switch (trigger?.type) {
        case "NEW_CONVERSATION":
            return "on the first message of a conversation";
        case "KEYWORD":
            return pattern ? `when they mention ${pattern}` : "on a keyword you have not chosen yet";
        case "CONTAINS":
            return pattern ? `when the message contains "${pattern}"` : "on text you have not chosen yet";
        case "EXACT":
            return pattern ? `when the message is exactly "${pattern}"` : "on text you have not chosen yet";
        case "STARTS_WITH":
            return pattern ? `when the message starts with "${pattern}"` : "on text you have not chosen yet";
        case "REGEX":
            return pattern ? `when the message matches ${pattern}` : "on a pattern you have not written yet";
        case "BUTTON":
            return pattern ? `when the "${pattern}" option is tapped` : "on an option you have not named yet";
        default:
            return meta.id === "ANY_MESSAGE" ? "on any message" : "on any message";
    }
}

/** The handle id a question's per-option branch hangs off. */
export const routeHandle = (value) => `route:${value}`;

/** The option id a route handle refers to, or null if it is not a route. */
export const routeValue = (handle) =>
    typeof handle === "string" && handle.startsWith("route:") ? handle.slice(6) : null;

/** The reply buttons a question node offers, in order. */
export function optionsOf(node) {
    if (!node || node.type !== "QUESTION") return [];
    const ask = (node.actions || []).find((action) => action.type === "ASK_QUESTION");
    return (ask?.buttons || []).filter((button) => (button.type || "REPLY") === "REPLY" && button.id);
}

/**
 * Every output a node can have, as `{handle, label, tone}`.
 *
 * This is the single definition the node component draws from, the edge
 * builder reads, and the validator counts — so a question with three buttons
 * grows three handles everywhere at once, and never in only two of the three.
 */
export function outputsOf(node) {
    if (!node) return [];

    switch (node.type) {
        case "END":
            return [];
        case "CONDITION":
            return [
                { handle: "true", label: "Yes", tone: "yes" },
                { handle: "false", label: "No", tone: "no" },
            ];
        case "QUESTION": {
            const options = optionsOf(node).map((button) => ({
                handle: routeHandle(button.id),
                label: button.title || button.id,
                tone: "option",
            }));
            // The default is last and always present: it is where a typed
            // answer goes, and a question with buttons but no default is a dead
            // end for anybody who does not tap one.
            return [...options, { handle: "next", label: options.length ? "Anything else" : "Next", tone: "default" }];
        }
        default:
            return [{ handle: "next", label: "Next", tone: "default" }];
    }
}

const NODE_WIDTH = 240;

/** Flow JSON → the nodes and edges React Flow renders. */
export function toGraph(flowNodes) {
    const nodes = (flowNodes || []).map((node) => ({
        id: node.id,
        type: "chatbot",
        position: { x: Number(node.position?.x) || 0, y: Number(node.position?.y) || 0 },
        data: { raw: node, nodeType: node.type },
    }));

    const known = new Set(nodes.map((node) => node.id));
    const edges = [];

    (flowNodes || []).forEach((node) => {
        const add = (handle, target, label, tone) => {
            // A dangling edge is dropped rather than drawn: the server would
            // refuse it anyway, and half an arrow pointing at nothing is worse
            // than no arrow while somebody is mid-edit.
            if (!target || !known.has(target)) return;
            edges.push(buildEdge(node.id, handle, target, label, tone));
        };

        if (node.type === "CONDITION") {
            add("true", node.true_next, "Yes", "yes");
            add("false", node.false_next, "No", "no");
        } else if (node.type === "QUESTION") {
            (node.routes || []).forEach((route) => {
                const option = optionsOf(node).find((button) => button.id === route.value);
                add(routeHandle(route.value), route.next, option?.title || route.value, "option");
            });
            add("next", node.next, optionsOf(node).length ? "Anything else" : "", "default");
        } else if (node.type !== "END") {
            add("next", node.next, "", "default");
        }
    });

    return { nodes, edges };
}

export function buildEdge(source, sourceHandle, target, label, tone) {
    return {
        id: `${source}::${sourceHandle}->${target}`,
        source,
        target,
        sourceHandle,
        targetHandle: "in",
        type: "smoothstep",
        label: label || undefined,
        markerEnd: { type: "arrowclosed" },
        data: { tone },
        style: tone === "no" ? { stroke: "#f87171" } : undefined,
    };
}

/**
 * The canvas → flow JSON.
 *
 * Two clean-ups happen here rather than being left for the server to reject:
 * a route whose option no longer exists is dropped (the author deleted the
 * button, and the branch cannot survive it), and an edge from a handle the
 * node no longer has is ignored for the same reason.
 */
export function fromGraph(graphNodes, graphEdges) {
    const positions = Object.fromEntries(
        graphNodes.map((node) => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]),
    );

    return graphNodes.map((graphNode) => {
        const node = { ...graphNode.data.raw, position: positions[graphNode.id] };
        const outgoing = graphEdges.filter((edge) => edge.source === graphNode.id);
        const targetFor = (handle) => outgoing.find((edge) => edge.sourceHandle === handle)?.target || "";

        node.next = node.type === "END" ? "" : targetFor("next");

        if (node.type === "CONDITION") {
            node.true_next = targetFor("true");
            node.false_next = targetFor("false");
        } else {
            delete node.true_next;
            delete node.false_next;
        }

        if (node.type === "QUESTION") {
            node.routes = optionsOf(node)
                .map((button) => ({ value: button.id, next: targetFor(routeHandle(button.id)) }))
                .filter((route) => route.next);
        } else {
            delete node.routes;
        }

        return node;
    });
}

/** A node id nothing else is using. */
export function newNodeId(nodes, type) {
    const stem = String(type || "node").toLowerCase();
    let counter = 1;
    while (nodes.some((node) => node.id === `${stem}_${counter}`)) counter += 1;
    return `${stem}_${counter}`;
}

/**
 * Lay the graph out left to right, in the order a conversation reaches it.
 *
 * A layered walk from Start rather than a general graph-drawing algorithm: a
 * conversation *is* layered — turn one, then turn two — so depth from Start is
 * the x axis people already have in their heads, and the result reads like the
 * transcript it describes. Anything unreachable is parked in a row underneath
 * rather than hidden, because an orphan you cannot see is one you cannot fix.
 */
export function autoLayout(flowNodes) {
    const byId = Object.fromEntries(flowNodes.map((node) => [node.id, node]));
    const start = flowNodes.find((node) => node.type === "START");
    const depth = new Map();

    if (start) {
        const queue = [[start.id, 0]];
        while (queue.length) {
            const [id, level] = queue.shift();
            if (!byId[id] || depth.has(id)) continue;
            depth.set(id, level);
            edgeTargets(byId[id]).forEach((target) => queue.push([target, level + 1]));
        }
    }

    const orphanRow = Math.max(0, ...[...depth.values()].map(() => 0));
    const rows = new Map();
    const laidOut = flowNodes.map((node) => {
        const level = depth.has(node.id) ? depth.get(node.id) : -1;
        const row = rows.get(level) || 0;
        rows.set(level, row + 1);
        return {
            ...node,
            position:
                level === -1
                    ? { x: row * (NODE_WIDTH + 80), y: (maxDepthRows(depth) + orphanRow + 2) * 180 }
                    : { x: level * (NODE_WIDTH + 100), y: row * 180 },
        };
    });

    return laidOut;
}

function maxDepthRows(depth) {
    const counts = {};
    depth.forEach((level) => {
        counts[level] = (counts[level] || 0) + 1;
    });
    return Math.max(0, ...Object.values(counts));
}

/** Everywhere one node can hand control to, mirroring `flow._edges` on the server. */
export function edgeTargets(node) {
    const targets = [node.next];
    if (node.type === "CONDITION") targets.push(node.true_next, node.false_next);
    if (node.type === "QUESTION") (node.routes || []).forEach((route) => targets.push(route.next));
    return targets.filter(Boolean);
}

/** The one-line summary a node shows on the canvas. */
export function describeNode(node) {
    const actions = node.actions || [];
    const first = actions[0];

    switch (node.type) {
        case "START":
            return `Starts ${describeTrigger(triggerOf(node))}.`;
        case "CONDITION": {
            const condition = node.condition || {};
            return `If ${condition.field || "…"} ${condition.operator || "is"} ${condition.value || "…"}`;
        }
        case "DELAY":
            return `Wait ${node.seconds || 0}s`;
        case "QUESTION": {
            const options = optionsOf(node);
            const body = first?.body || "Ask something";
            return options.length ? `${body} (${options.length} options)` : body;
        }
        default:
            break;
    }

    if (!first) return "Nothing configured yet.";
    if (first.body) return first.body;
    if (first.message) return first.message;
    if (first.tag) return `Tag: ${first.tag}`;
    if (first.name) return `Set ${first.name}`;
    if (first.url) return `${first.method || "GET"} ${first.url}`;
    if (first.tool) return `Run ${first.tool}`;
    return first.type.replace(/_/g, " ").toLowerCase();
}

/**
 * Problems the canvas can see for itself, without asking the server.
 *
 * Deliberately a subset of what `apps/chatbot/validation.py` reports, and
 * never a replacement for it: this exists to mark a node red while somebody is
 * still drawing, and the server's answer is what the Publish button obeys.
 */
export function localProblems(flowNodes) {
    const problems = [];
    const starts = flowNodes.filter((node) => node.type === "START");

    if (starts.length === 0) problems.push({ node_id: "", message: "This flow has no Start node." });
    if (starts.length > 1) problems.push({ node_id: "", message: "A flow may only have one Start node." });

    const reachable = new Set();
    if (starts[0]) {
        const byId = Object.fromEntries(flowNodes.map((node) => [node.id, node]));
        const queue = [starts[0].id];
        while (queue.length) {
            const id = queue.shift();
            if (!byId[id] || reachable.has(id)) continue;
            reachable.add(id);
            edgeTargets(byId[id]).forEach((target) => queue.push(target));
        }
    }

    if (starts[0]) {
        const trigger = triggerOf(starts[0]);
        if (triggerMeta(trigger.type).needsPattern && !trigger.pattern.trim()) {
            problems.push({
                node_id: starts[0].id,
                message: "This flow's trigger has nothing to match, so the flow would never start.",
            });
        }
    }

    flowNodes.forEach((node) => {
        if (starts[0] && !reachable.has(node.id)) {
            problems.push({ node_id: node.id, message: "Nothing leads to this node." });
        }

        // The action editor starts a new message/question with an empty body
        // while it is being filled in.  Flag that local, fixable state before
        // autosave sends it to the API; otherwise the server (correctly)
        // rejects it and the user only sees a generic save failure while
        // typing.
        (node.actions || []).forEach((action, index) => {
            if (["SEND_MESSAGE", "ASK_QUESTION"].includes(action.type) && !String(action.body || "").trim()) {
                problems.push({
                    node_id: node.id,
                    message: `Step ${index + 1}: add message text before saving this node.`,
                });
            }
        });

        if (node.type === "QUESTION") {
            const routed = new Set((node.routes || []).map((route) => route.value));
            optionsOf(node).forEach((button) => {
                if (!routed.has(button.id) && !node.next) {
                    problems.push({
                        node_id: node.id,
                        message: `The '${button.title || button.id}' option leads nowhere.`,
                    });
                }
            });
        }
    });

    return problems;
}
