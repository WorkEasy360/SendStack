/**
 * Converting between the stored workflow node list and a React Flow graph.
 *
 * The stored shape is the source of truth and the canvas is a view of it, so
 * the one rule everything here obeys is that a node must survive a round trip
 * unchanged. A MESSAGE node carrying media items, a template id, or a field
 * added to the backend after this file was written is edited on the canvas by
 * its label alone and written back with everything else intact — the
 * alternative is a drag-and-drop editor that silently deletes half a
 * workflow the first time somebody opens it.
 */

export const NODE_TYPES = ["TRIGGER", "MESSAGE", "AI_REPLY", "WAIT", "CONDITION", "TAG", "ASSIGN_AGENT", "END"];

/** Which outgoing edges each node type has, and what each one is called. */
export const OUTPUTS = {
    TRIGGER: [{ field: "next", handle: "next", label: "" }],
    MESSAGE: [{ field: "next", handle: "next", label: "" }],
    AI_REPLY: [{ field: "next", handle: "next", label: "" }],
    WAIT: [{ field: "next", handle: "next", label: "" }],
    TAG: [{ field: "next", handle: "next", label: "" }],
    ASSIGN_AGENT: [{ field: "next", handle: "next", label: "" }],
    CONDITION: [
        { field: "on_true", handle: "true", label: "Yes" },
        { field: "on_false", handle: "false", label: "No" },
    ],
    END: [],
};

const HANDLE_TO_FIELD = { next: "next", true: "on_true", false: "on_false" };

/** Kept in step with services.MAX_WAIT_MINUTES on the backend. */
export const MAX_WAIT_MINUTES = 60 * 24 * 30;

const COLUMN_WIDTH = 260;
const ROW_HEIGHT = 150;

export function newNodeId(type, existing) {
    const base = type.toLowerCase();
    let index = 1;
    const taken = new Set(existing);
    while (taken.has(`${base}_${index}`)) index += 1;
    return `${base}_${index}`;
}

/**
 * Place nodes that have never been positioned.
 *
 * Breadth-first from the trigger, one column per step: it is not a pretty
 * layout, but it is a *readable* one, and every node ends up somewhere the
 * user can find and drag. Nodes nothing points at are parked in a final
 * column rather than stacked at the origin on top of each other.
 */
export function autoLayout(nodes) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const depth = new Map();
    const trigger = nodes.find((node) => node.type === "TRIGGER");

    const queue = trigger ? [[trigger.id, 0]] : [];
    while (queue.length) {
        const [id, level] = queue.shift();
        if (depth.has(id)) continue;
        depth.set(id, level);
        const node = byId.get(id);
        if (!node) continue;
        for (const output of OUTPUTS[node.type] || []) {
            const target = node[output.field];
            if (target && !depth.has(target)) queue.push([target, level + 1]);
        }
    }

    const maxDepth = depth.size ? Math.max(...depth.values()) : 0;
    const rowsPerColumn = new Map();

    return nodes.map((node) => {
        const column = depth.has(node.id) ? depth.get(node.id) : maxDepth + 1;
        const row = rowsPerColumn.get(column) || 0;
        rowsPerColumn.set(column, row + 1);
        return { ...node, position: { x: 40 + column * COLUMN_WIDTH, y: 40 + row * ROW_HEIGHT } };
    });
}

/** Stored node list -> React Flow nodes and edges. */
export function toGraph(nodeList) {
    const needsLayout = nodeList.some((node) => !node.position || typeof node.position.x !== "number");
    const positioned = needsLayout ? autoLayout(nodeList) : nodeList;

    const nodes = positioned.map((node) => ({
        id: node.id,
        type: "workflow",
        position: node.position,
        // `raw` is the whole stored node. Everything the canvas does not
        // render still lives here and is written straight back out.
        data: { raw: node, nodeType: node.type },
    }));

    const known = new Set(nodeList.map((node) => node.id));
    const edges = [];
    for (const node of positioned) {
        for (const output of OUTPUTS[node.type] || []) {
            const target = node[output.field];
            // A dangling reference is dropped from the drawing rather than
            // drawn to nowhere; the node keeps it until the user saves, and
            // the backend validator is the thing that refuses it.
            if (!target || !known.has(target)) continue;
            edges.push({
                id: `${node.id}:${output.handle}`,
                source: node.id,
                sourceHandle: output.handle,
                target,
                targetHandle: "in",
                label: output.label || undefined,
                type: "smoothstep",
                markerEnd: { type: "arrowclosed" },
            });
        }
    }
    return { nodes, edges };
}

/** React Flow nodes and edges -> the stored node list. */
export function fromGraph(nodes, edges) {
    const outgoing = new Map();
    for (const edge of edges) {
        const field = HANDLE_TO_FIELD[edge.sourceHandle] || "next";
        outgoing.set(`${edge.source}:${field}`, edge.target);
    }

    return nodes.map((node) => {
        const stored = { ...node.data.raw, id: node.id, type: node.data.nodeType, position: node.position };
        for (const output of OUTPUTS[stored.type] || []) {
            const target = outgoing.get(`${node.id}:${output.field}`);
            if (target) stored[output.field] = target;
            else delete stored[output.field];
        }
        // Edge fields that belong to a different node type are removed, so
        // changing a CONDITION into a MESSAGE does not leave on_true behind
        // for the validator to reject.
        for (const field of ["next", "on_true", "on_false"]) {
            const owns = (OUTPUTS[stored.type] || []).some((output) => output.field === field);
            if (!owns) delete stored[field];
        }
        return stored;
    });
}

/**
 * The same checks the backend makes, run as the user types.
 *
 * Duplicated deliberately: the server stays the authority (this is a browser,
 * and nothing it says can be trusted), but being told "a workflow needs
 * exactly one trigger" while looking at the canvas is worth far more than
 * being told it after pressing save.
 */
export function validateGraph(nodeList) {
    const problems = [];
    if (!nodeList.length) return ["A workflow needs at least one node."];

    const triggers = nodeList.filter((node) => node.type === "TRIGGER");
    if (triggers.length === 0) problems.push("A workflow needs a Trigger node to start from.");
    if (triggers.length > 1) problems.push("A workflow can only have one Trigger node.");

    const ids = new Set(nodeList.map((node) => node.id));
    for (const node of nodeList) {
        for (const field of ["next", "on_true", "on_false"]) {
            if (node[field] && !ids.has(node[field])) {
                problems.push(`“${node.id}” points at a node that no longer exists.`);
            }
        }
        if (node.type === "WAIT" && typeof node.minutes !== "number") {
            problems.push(`“${node.id}” needs a wait time in minutes.`);
        }
        if (node.type === "WAIT" && typeof node.minutes === "number" && node.minutes <= 0) {
            problems.push(`“${node.id}” needs a wait time of at least one minute.`);
        }
        if (node.type === "WAIT" && node.minutes > MAX_WAIT_MINUTES) {
            problems.push(`“${node.id}” cannot wait longer than 30 days.`);
        }
        if (node.type === "CONDITION" && !node.condition?.field) {
            problems.push(`“${node.id}” needs something to check.`);
        }
        if (node.type === "CONDITION" && node.condition?.field === "has_tag" && !node.condition?.value) {
            problems.push(`“${node.id}” needs a tag to check for.`);
        }
        if (node.type === "CONDITION" && !node.on_true && !node.on_false) {
            problems.push(`“${node.id}” needs at least one of its Yes / No outputs connected.`);
        }
        if (node.type === "MESSAGE" && !node.body?.trim() && !node.template_id && !node.media_url) {
            problems.push(`“${node.id}” has no message to send.`);
        }
        if (node.type === "TAG" && !node.tag_id) {
            problems.push(`“${node.id}” needs a tag to add.`);
        }
        if (node.type === "ASSIGN_AGENT" && !node.agent_id) {
            problems.push(`“${node.id}” needs an agent to assign.`);
        }
        if (node.type === "TRIGGER") {
            const trigger = node.trigger || {};
            if (trigger.event === "KEYWORD" && !trigger.keyword?.trim()) {
                problems.push("The trigger needs a keyword to watch for.");
            }
            if (trigger.event === "BUTTON" && !trigger.button_id?.trim()) {
                problems.push("The trigger needs the id of the button it answers.");
            }
        }
    }

    problems.push(...findTightLoops(nodeList));
    return [...new Set(problems)];
}

/**
 * Steps that loop with no Wait between them.
 *
 * The engine walks edges until it reaches a Wait or an End, so a loop made
 * only of Messages and Conditions never gets a chance to stop -- it sends the
 * same message over and over. A loop that passes through a Wait is a normal
 * drip sequence and is left alone. The backend refuses these too; catching it
 * here means the person drawing it finds out before they hit Save.
 */
function findTightLoops(nodeList) {
    const byId = new Map(nodeList.map((node) => [node.id, node]));
    const state = new Map(); // id -> "open" | "done"
    const problems = [];

    const walk = (id, path) => {
        const node = byId.get(id);
        if (!node) return;
        if (state.get(id) === "open") {
            const loop = path.slice(path.indexOf(id)).concat(id).join(" → ");
            problems.push(`These steps loop with nothing to pause them: ${loop}. Add a Wait step inside the loop.`);
            return;
        }
        if (state.get(id) === "done") return;

        state.set(id, "open");
        if (node.type !== "WAIT") {
            for (const field of ["next", "on_true", "on_false"]) {
                if (node[field]) walk(node[field], [...path, id]);
            }
        }
        state.set(id, "done");
    };

    for (const node of nodeList) walk(node.id, []);
    return problems;
}

/** A short, human line describing what a node does, for the card on the canvas. */
export function describeNode(node) {
    switch (node.type) {
        case "TRIGGER":
            return describeTrigger(node.trigger);
        case "MESSAGE":
            return node.template_id ? "Sends a template" : (node.body || "").slice(0, 80) || "No message yet";
        case "AI_REPLY":
            return "The AI agent answers";
        case "WAIT":
            return typeof node.minutes === "number" ? `Waits ${formatMinutes(node.minutes)}` : "No wait time set";
        case "CONDITION":
            if (node.condition?.field === "button_clicked") {
                return `Tapped ${node.condition.value ? `“${node.condition.value}”` : "any button"}?`;
            }
            if (node.condition?.field === "has_tag") {
                return node.condition.value ? "Has a given tag?" : "No tag chosen";
            }
            return "Has the contact replied?";
        case "TAG":
            return node.tag_id ? "Adds a tag" : "No tag chosen";
        case "ASSIGN_AGENT":
            return node.agent_id ? "Assigns an agent" : "No agent chosen";
        case "END":
            return "Ends the workflow";
        default:
            return "";
    }
}

/** One line for the Trigger card, so the canvas says how a workflow starts. */
function describeTrigger(trigger) {
    switch (trigger?.event) {
        case "NEW_CONVERSATION":
            return "When a contact first writes in";
        case "KEYWORD":
            return trigger.keyword ? `On “${trigger.keyword}”` : "No keyword set";
        case "BUTTON":
            return trigger.button_id ? `On button “${trigger.button_id}”` : "No button set";
        case "INBOUND_MESSAGE":
            return "On any message from a contact";
        default:
            return "Only when you start it";
    }
}

export function formatMinutes(minutes) {
    if (minutes < 60) return `${minutes} min`;
    if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes === 1440 ? "" : "s"}`;
    if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? "" : "s"}`;
    return `${Math.round((minutes / 60) * 10) / 10} hours`;
}
