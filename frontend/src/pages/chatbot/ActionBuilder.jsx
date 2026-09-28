import { Plus, Trash2 } from "lucide-react";
import Button from "../../components/ui/Button";
import ButtonBuilder from "../../components/ui/ButtonBuilder";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";

/**
 * The editor for "what should the bot do", shared by the rule builder and the
 * flow builder.
 *
 * One component because the backend has one action vocabulary: a message
 * written here behaves identically whether it hangs off a rule or a node, and
 * two editors would be two places for the shapes to drift from what
 * `apps/chatbot/actions.py` actually validates.
 *
 * `allowed` narrows the picker for a flow node, which may only carry the
 * action types its own node type permits.
 */

export const ACTION_TYPES = [
    { id: "SEND_MESSAGE", label: "Send a message", group: "Say something" },
    { id: "SEND_MEDIA", label: "Send a file", group: "Say something" },
    { id: "ASK_QUESTION", label: "Ask a question", group: "Say something" },
    { id: "AI_REPLY", label: "Let the AI answer", group: "Say something" },
    { id: "KNOWLEDGE_ANSWER", label: "Answer from the knowledge base", group: "Say something" },
    { id: "SET_VARIABLE", label: "Remember something", group: "Remember" },
    { id: "ADD_TAG", label: "Add a tag", group: "Remember" },
    { id: "REMOVE_TAG", label: "Remove a tag", group: "Remember" },
    { id: "ASSIGN_AGENT", label: "Assign to an agent", group: "Hand over" },
    { id: "HANDOFF", label: "Hand to a person", group: "Hand over" },
    { id: "TRANSFER_CHATBOT", label: "Transfer to another chatbot", group: "Hand over" },
    { id: "TRIGGER_CAMPAIGN", label: "Add to a campaign", group: "Do something" },
    { id: "TRIGGER_AUTOMATION", label: "Start a workflow", group: "Do something" },
    { id: "CALL_TOOL", label: "Run an internal tool", group: "Do something" },
    { id: "CALL_API", label: "Call your own API", group: "Do something" },
    { id: "END_CONVERSATION", label: "End the conversation", group: "Do something" },
];

const GROUPS = ["Say something", "Remember", "Hand over", "Do something"];

/** Actions after which nothing else runs — the API refuses a step following one. */
export const TERMINAL_ACTIONS = new Set(["HANDOFF", "TRANSFER_CHATBOT", "END_CONVERSATION"]);

const DEFAULTS = {
    SEND_MESSAGE: { body: "", buttons: [] },
    SEND_MEDIA: { media_url: "", media_type: "DOCUMENT", filename: "", caption: "" },
    ASK_QUESTION: { body: "", buttons: [], save_to: "" },
    AI_REPLY: { knowledge_base_id: null, prompt: "" },
    KNOWLEDGE_ANSWER: { knowledge_base_id: null, prompt: "" },
    SET_VARIABLE: { name: "", value: "", scope: "CONVERSATION" },
    ADD_TAG: { tag: "" },
    REMOVE_TAG: { tag: "" },
    ASSIGN_AGENT: { agent_id: null },
    HANDOFF: { message: "", reason: "RULE_ACTION" },
    TRANSFER_CHATBOT: { chatbot_id: null },
    TRIGGER_CAMPAIGN: { campaign_id: null },
    TRIGGER_AUTOMATION: { workflow_id: null },
    CALL_TOOL: { tool: "search_knowledge", arguments: {} },
    CALL_API: { url: "", method: "GET", headers: {}, payload: {}, prefix: "api" },
    END_CONVERSATION: { message: "", resolution: "BOT" },
};

export function newAction(type = "SEND_MESSAGE") {
    return { type, ...structuredClone(DEFAULTS[type] || {}) };
}

export default function ActionBuilder({ actions = [], onChange, allowed = null, options = {}, max = 12 }) {
    const choices = allowed ? ACTION_TYPES.filter((entry) => allowed.includes(entry.id)) : ACTION_TYPES;

    const update = (index, patch) =>
        onChange(actions.map((action, position) => (position === index ? { ...action, ...patch } : action)));

    const replace = (index, type) =>
        onChange(actions.map((action, position) => (position === index ? newAction(type) : action)));

    const remove = (index) => onChange(actions.filter((_, position) => position !== index));

    // A terminal action ends the turn, so the API refuses anything after one.
    // Blocking "Add" here says so before the save fails rather than after.
    const hasTerminal = actions.some((action) => TERMINAL_ACTIONS.has(action.type));

    return (
        <div>
            <div className="space-y-3">
                {actions.map((action, index) => (
                    <div key={index} className="border border-ink-200 rounded-xl p-4 bg-ink-50/40">
                        <div className="flex items-start gap-2 mb-3">
                            <span className="mt-2.5 w-6 h-6 shrink-0 rounded-md bg-ink-900 text-ink-50 text-xs font-bold flex items-center justify-center tabular-nums">
                                {index + 1}
                            </span>
                            <div className="flex-1 min-w-0">
                                <Select
                                    value={action.type}
                                    onChange={(event) => replace(index, event.target.value)}
                                    aria-label={`Step ${index + 1} action`}
                                >
                                    {GROUPS.map((group) => {
                                        const inGroup = choices.filter((entry) => entry.group === group);
                                        if (!inGroup.length) return null;
                                        return (
                                            <optgroup key={group} label={group}>
                                                {inGroup.map((entry) => (
                                                    <option key={entry.id} value={entry.id}>
                                                        {entry.label}
                                                    </option>
                                                ))}
                                            </optgroup>
                                        );
                                    })}
                                </Select>
                            </div>
                            <button
                                type="button"
                                onClick={() => remove(index)}
                                aria-label={`Remove step ${index + 1}`}
                                className="mt-1 w-9 h-9 shrink-0 inline-flex items-center justify-center rounded-lg text-ink-400 hover:bg-red-50 hover:text-red-600 bg-transparent border-none cursor-pointer transition-colors"
                            >
                                <Trash2 size={16} />
                            </button>
                        </div>

                        <div className="pl-8">
                            <ActionFields action={action} onChange={(patch) => update(index, patch)} options={options} />
                        </div>
                    </div>
                ))}
            </div>

            {actions.length === 0 && (
                <p className="text-sm text-ink-500 border border-dashed border-ink-300 rounded-xl px-4 py-6 text-center m-0">
                    No steps yet. Add one to say what should happen.
                </p>
            )}

            <div className="mt-3">
                <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    icon={Plus}
                    disabled={actions.length >= max || hasTerminal}
                    onClick={() => onChange([...actions, newAction(choices[0]?.id || "SEND_MESSAGE")])}
                >
                    Add a step
                </Button>
                {hasTerminal && (
                    <span className="ml-3 text-xs text-ink-500">
                        The last step ends the conversation, so nothing can follow it.
                    </span>
                )}
                {actions.length >= max && (
                    <span className="ml-3 text-xs text-ink-500">That's the most steps one rule can have.</span>
                )}
            </div>
        </div>
    );
}

function ActionFields({ action, onChange, options }) {
    const { knowledgeBases = [], agents = [], campaigns = [], workflows = [], chatbots = [], tools = [], assets = [] } = options;

    switch (action.type) {
        case "SEND_MESSAGE":
        case "ASK_QUESTION":
            return (
                <>
                    <Field
                        label="Message"
                        hint="Use {{first_name}}, {{phone}} or any variable this bot has remembered."
                    >
                        <Textarea
                            rows={3}
                            value={action.body || ""}
                            onChange={(event) => onChange({ body: event.target.value })}
                            placeholder="Our basic plan starts at ₹999/month."
                        />
                    </Field>
                    {action.type === "ASK_QUESTION" && (
                        <Field
                            label="Save the answer as"
                            hint="The customer's next message is stored under this name, e.g. shoe_size."
                        >
                            <Input
                                value={action.save_to || ""}
                                onChange={(event) => onChange({ save_to: event.target.value })}
                                placeholder="shoe_size"
                            />
                        </Field>
                    )}
                    <ButtonBuilder
                        value={action.buttons || []}
                        onChange={(buttons) => onChange({ buttons })}
                        provider={options.provider || null}
                    />
                </>
            );

        case "SEND_MEDIA":
            return (
                <>
                    {assets.length > 0 && (
                        <Field label="A saved file" hint="Or give a direct URL below.">
                            <Select
                                value={action.asset_key || ""}
                                onChange={(event) =>
                                    onChange({ asset_key: event.target.value || undefined, media_url: "" })
                                }
                            >
                                <option value="">Use a URL instead</option>
                                {assets.map((asset) => (
                                    <option key={asset.key} value={asset.key}>
                                        {asset.label} ({asset.key})
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                    {!action.asset_key && (
                        <div className="grid sm:grid-cols-2 gap-x-4">
                            <Field label="File URL">
                                <Input
                                    value={action.media_url || ""}
                                    onChange={(event) => onChange({ media_url: event.target.value })}
                                    placeholder="https://…/brochure.pdf"
                                />
                            </Field>
                            <Field label="Type">
                                <Select
                                    value={action.media_type || "DOCUMENT"}
                                    onChange={(event) => onChange({ media_type: event.target.value })}
                                >
                                    <option value="DOCUMENT">Document</option>
                                    <option value="IMAGE">Image</option>
                                    <option value="VIDEO">Video</option>
                                </Select>
                            </Field>
                        </div>
                    )}
                    <Field label="Caption" className="mb-0">
                        <Input
                            value={action.caption || ""}
                            onChange={(event) => onChange({ caption: event.target.value })}
                            placeholder="Here's our brochure."
                        />
                    </Field>
                </>
            );

        case "AI_REPLY":
        case "KNOWLEDGE_ANSWER":
            return (
                <>
                    <Field label="Knowledge base" hint="Leave blank to use this bot's own.">
                        <Select
                            value={action.knowledge_base_id || ""}
                            onChange={(event) =>
                                onChange({ knowledge_base_id: event.target.value ? Number(event.target.value) : null })
                            }
                        >
                            <option value="">This bot's knowledge base</option>
                            {knowledgeBases.map((base) => (
                                <option key={base.id} value={base.id}>
                                    {base.name}
                                </option>
                            ))}
                        </Select>
                    </Field>
                    <Field label="Extra instruction" hint="Optional. Added on top of this bot's own prompt." className="mb-0">
                        <Textarea
                            rows={2}
                            value={action.prompt || ""}
                            onChange={(event) => onChange({ prompt: event.target.value })}
                            placeholder="Keep the answer to one sentence."
                        />
                    </Field>
                </>
            );

        case "SET_VARIABLE":
            return (
                <div className="grid sm:grid-cols-3 gap-x-4">
                    <Field label="Name">
                        <Input
                            value={action.name || ""}
                            onChange={(event) => onChange({ name: event.target.value })}
                            placeholder="city"
                        />
                    </Field>
                    <Field label="Value" hint="Variables work here too.">
                        <Input
                            value={action.value || ""}
                            onChange={(event) => onChange({ value: event.target.value })}
                            placeholder="{{last_message}}"
                        />
                    </Field>
                    <Field label="Remember for">
                        <Select
                            value={action.scope || "CONVERSATION"}
                            onChange={(event) => onChange({ scope: event.target.value })}
                        >
                            <option value="CONVERSATION">This conversation</option>
                            <option value="CUSTOMER">This customer, forever</option>
                        </Select>
                    </Field>
                </div>
            );

        case "ADD_TAG":
        case "REMOVE_TAG":
            return (
                <Field label="Tag" className="mb-0">
                    <Input
                        value={action.tag || ""}
                        onChange={(event) => onChange({ tag: event.target.value })}
                        placeholder="Hot lead"
                    />
                </Field>
            );

        case "ASSIGN_AGENT":
            return (
                <Field label="Agent" hint="Leave blank to use your team's routing policy." className="mb-0">
                    <Select
                        value={action.agent_id || ""}
                        onChange={(event) => onChange({ agent_id: event.target.value ? Number(event.target.value) : null })}
                    >
                        <option value="">Whoever the routing policy picks</option>
                        {agents.map((agent) => (
                            <option key={agent.id} value={agent.id}>
                                {agent.username}
                            </option>
                        ))}
                    </Select>
                </Field>
            );

        case "HANDOFF":
            return (
                <Field
                    label="What to tell the customer"
                    hint="Leave blank to use this bot's handoff message."
                    className="mb-0"
                >
                    <Textarea
                        rows={2}
                        value={action.message || ""}
                        onChange={(event) => onChange({ message: event.target.value })}
                        placeholder="Let me get a colleague to help with that."
                    />
                </Field>
            );

        case "TRANSFER_CHATBOT":
            return (
                <Field label="Transfer to" className="mb-0">
                    <Select
                        value={action.chatbot_id || ""}
                        onChange={(event) => onChange({ chatbot_id: Number(event.target.value) || null })}
                    >
                        <option value="">Choose a chatbot…</option>
                        {chatbots.map((bot) => (
                            <option key={bot.id} value={bot.id}>
                                {bot.name}
                            </option>
                        ))}
                    </Select>
                </Field>
            );

        case "TRIGGER_CAMPAIGN":
            return (
                <Field
                    label="Campaign"
                    hint="Adds this customer to the campaign's audience. It does not launch it."
                    className="mb-0"
                >
                    <Select
                        value={action.campaign_id || ""}
                        onChange={(event) => onChange({ campaign_id: Number(event.target.value) || null })}
                    >
                        <option value="">Choose a campaign…</option>
                        {campaigns.map((campaign) => (
                            <option key={campaign.id} value={campaign.id}>
                                {campaign.name}
                            </option>
                        ))}
                    </Select>
                </Field>
            );

        case "TRIGGER_AUTOMATION":
            return (
                <Field label="Workflow" className="mb-0">
                    <Select
                        value={action.workflow_id || ""}
                        onChange={(event) => onChange({ workflow_id: Number(event.target.value) || null })}
                    >
                        <option value="">Choose a workflow…</option>
                        {workflows.map((workflow) => (
                            <option key={workflow.id} value={workflow.id}>
                                {workflow.name}
                            </option>
                        ))}
                    </Select>
                </Field>
            );

        case "CALL_TOOL":
            return (
                <Field
                    label="Tool"
                    hint="Only tools you have switched on under AI Agent will actually run."
                    className="mb-0"
                >
                    <Select value={action.tool || ""} onChange={(event) => onChange({ tool: event.target.value })}>
                        {(tools.length ? tools : [{ name: "search_knowledge" }, { name: "lookup_customer" }]).map(
                            (tool) => (
                                <option key={tool.name} value={tool.name}>
                                    {tool.name}
                                </option>
                            ),
                        )}
                    </Select>
                </Field>
            );

        case "CALL_API":
            return (
                <>
                    <div className="grid sm:grid-cols-4 gap-x-4">
                        <Field label="Method">
                            <Select
                                value={action.method || "GET"}
                                onChange={(event) => onChange({ method: event.target.value })}
                            >
                                <option value="GET">GET</option>
                                <option value="POST">POST</option>
                            </Select>
                        </Field>
                        <Field label="URL" className="mb-4 sm:col-span-3">
                            <Input
                                value={action.url || ""}
                                onChange={(event) => onChange({ url: event.target.value })}
                                placeholder="https://api.yourshop.com/orders/{{order_id}}"
                            />
                        </Field>
                    </div>
                    <Field
                        label="Store the answer under"
                        hint="A JSON field called 'status' becomes {{api_status}}."
                        className="mb-0"
                    >
                        <Input
                            value={action.prefix || "api"}
                            onChange={(event) => onChange({ prefix: event.target.value })}
                            placeholder="api"
                        />
                    </Field>
                    <p className="text-xs text-ink-500 mt-3 mb-0">
                        Must be https, and must not point inside your own network. The response becomes variables you
                        can use in a later message — it is never treated as an instruction to the AI.
                    </p>
                </>
            );

        case "END_CONVERSATION":
            return (
                <Field label="A final message" hint="Optional." className="mb-0">
                    <Input
                        value={action.message || ""}
                        onChange={(event) => onChange({ message: event.target.value })}
                        placeholder="Thanks for chatting — have a great day!"
                    />
                </Field>
            );

        default:
            return null;
    }
}
