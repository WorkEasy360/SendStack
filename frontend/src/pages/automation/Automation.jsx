import { useCallback, useEffect, useState } from "react";
import {
    Bot,
    Clock,
    Code,
    GitBranch,
    Hand,
    History,
    MessageCircleReply,
    MousePointerClick,
    Pencil,
    Play,
    Plus,
    Power,
    PowerOff,
    Sparkles,
    Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import { toMediaItems, primaryMessageType } from "../../utils/media";
import { replyButtonIds, toButtons } from "../../utils/buttons";
import useSelection from "../../hooks/useSelection";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Badge, { StatusBadge } from "../../components/ui/Badge";
import Modal from "../../components/ui/Modal";
import Tabs from "../../components/ui/Tabs";
import Spinner from "../../components/ui/Spinner";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import BulkActionBar from "../../components/ui/BulkActionBar";
import MediaPicker from "../../components/ui/MediaPicker";
import ButtonBuilder, { ButtonPreview } from "../../components/ui/ButtonBuilder";
import WorkflowBuilder from "../../components/workflow/WorkflowBuilder";
import QuickSetup from "./QuickSetup";
import { validateGraph } from "../../utils/workflowGraph";
import { Checkbox, CheckboxField, Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { FeatureLocked } from "../../components/billing/FeatureLock";
import { useEntitlements } from "../../context/EntitlementsContext";

// Quick setup is deliberately first and default. Somebody arriving here wants
// something to happen, not to learn a node graph; the three tabs after it are
// where you go once you know exactly what you want to change.
const TABS = [
    { id: "quick", label: "Quick setup", icon: Sparkles },
    { id: "rules", label: "Welcome & Auto-Reply", icon: MessageCircleReply },
    { id: "followups", label: "Follow-ups", icon: Clock },
    { id: "workflows", label: "Workflows", icon: GitBranch },
];

const MESSAGE_TYPES = ["TEXT", "IMAGE", "VIDEO", "DOCUMENT"];

const TRIGGER_ICONS = {
    WELCOME: Hand,
    KEYWORD: MessageCircleReply,
    BUTTON: MousePointerClick,
};

const MATCH_TYPES = [
    { value: "EXACT", label: "Exact match" },
    { value: "CONTAINS", label: "Contains" },
    { value: "STARTS_WITH", label: "Starts with" },
    { value: "ENDS_WITH", label: "Ends with" },
];

const EMPTY_CONTENT = {
    template: "",
    message_type: "TEXT",
    message_body: "",
    media_items: [],
    buttons: [],
};

// The starter graph doubles as the documentation for what a node can carry --
// including buttons on a MESSAGE node and the `button_clicked` condition that
// branches on which one was tapped.
const WORKFLOW_TEMPLATE = [
    { id: "trigger", type: "TRIGGER", next: "welcome" },
    {
        id: "welcome",
        type: "MESSAGE",
        body: "Hi [Name], thanks for reaching out! Would you like a callback?",
        buttons: [
            { type: "REPLY", id: "callback_yes", title: "Yes please" },
            { type: "REPLY", id: "callback_no", title: "Not now" },
        ],
        next: "wait",
    },
    { id: "wait", type: "WAIT", minutes: 60, next: "check" },
    {
        id: "check",
        type: "CONDITION",
        condition: { field: "button_clicked", value: "callback_yes" },
        on_true: "book",
        on_false: "nudge",
    },
    { id: "book", type: "MESSAGE", body: "Great - someone will call you today.", next: "end" },
    { id: "nudge", type: "MESSAGE", body: "Just checking in - can I help with anything?", next: "end" },
    { id: "end", type: "END" },
];

/** Strip empty relations so DRF sees `null` rather than `""` for a template. */
function contentPayload(form) {
    return {
        template: form.template ? Number(form.template) : null,
        message_type: form.message_type,
        message_body: form.message_body,
        // The API mirrors media_url/filename/caption from the first attachment,
        // so the list is the only thing this form has to send.
        media_items: form.media_items,
        buttons: form.buttons,
    };
}

function contentFromRule(rule) {
    return {
        template: rule.template ?? "",
        message_type: rule.message_type || "TEXT",
        message_body: rule.message_body || "",
        // toMediaItems keeps a rule saved with the old single media_url
        // opening with that file already attached.
        media_items: toMediaItems(rule),
        buttons: toButtons(rule),
    };
}

/**
 * List + CRUD for one automation endpoint. Every automation resource is a
 * plain ModelViewSet, so create/update/delete/toggle are identical across them.
 */
function useAutomationResource(path) {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const reload = useCallback(async () => {
        try {
            const response = await api.get(path);
            setItems(response.data.results || []);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load this list."));
        } finally {
            setLoading(false);
        }
    }, [path]);

    useEffect(() => {
        reload();
    }, [reload]);

    const save = async (payload, id) => {
        const response = id ? await api.patch(`${path}${id}/`, payload) : await api.post(path, payload);
        await reload();
        return response.data;
    };

    const remove = async (id) => {
        await api.delete(`${path}${id}/`);
        await reload();
    };

    return { items, loading, error, setError, reload, save, remove };
}

/**
 * Select-all + bulk bar for the automation lists, which are card rows rather
 * than tables and so have no header to hang a select-all off.
 */
function BulkListHeader({ selection, items, label, busy, onAction, onDelete }) {
    if (items.length === 0) return null;

    return (
        <>
            <label className="inline-flex items-center gap-2 mb-3 text-sm font-medium text-ink-600 cursor-pointer select-none">
                <Checkbox
                    checked={selection.allSelected}
                    indeterminate={selection.someSelected}
                    onChange={selection.toggleAll}
                    aria-label={selection.allSelected ? `Deselect all ${label}s` : `Select all ${label}s`}
                />
                Select all {items.length} {label}
                {items.length === 1 ? "" : "s"}
            </label>

            <BulkActionBar count={selection.selectedCount} label={label} onClear={selection.clear}>
                <Button size="sm" variant="secondary" icon={Power} disabled={busy} onClick={() => onAction("activate")}>
                    Activate
                </Button>
                <Button size="sm" variant="secondary" icon={PowerOff} disabled={busy} onClick={() => onAction("deactivate")}>
                    Pause
                </Button>
                <Button size="sm" variant="danger" icon={Trash2} disabled={busy} onClick={onDelete}>
                    Delete
                </Button>
            </BulkActionBar>
        </>
    );
}

/** Shared bulk-action state for one automation list. */
function useAutomationBulk(path, items, reload, setError, toast) {
    const selection = useSelection(items);
    const [busy, setBusy] = useState(false);
    const [confirming, setConfirming] = useState(false);

    const run = async (action) => {
        setBusy(true);
        setError("");
        const result = await runBulk(path, { action, ids: selection.selectedIds });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            setError(describeBulkFailures(result, (id) => items.find((item) => item.id === id)?.name));
        }
        selection.clear();
        setConfirming(false);
        setBusy(false);
        await reload();
    };

    return { selection, busy, confirming, setConfirming, run };
}

/** Shared row shell for every automation list. */
function AutomationRow({ icon: Icon, title, subtitle, item, onToggle, onEdit, onDelete, children, selected = false, onToggleSelect }) {
    const [busy, setBusy] = useState(false);

    const toggle = async () => {
        setBusy(true);
        try {
            await onToggle(item, !item.is_active);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            className={`bg-surface border rounded-card shadow-card p-4 ${
                selected ? "border-brand-400 ring-1 ring-brand-500/20" : "border-ink-200"
            }`}
        >
            <div className="flex items-center gap-3">
                {onToggleSelect && (
                    <Checkbox checked={selected} onChange={onToggleSelect} aria-label={`Select ${title}`} />
                )}
                <span className="shrink-0 w-9 h-9 rounded-lg bg-ink-100 text-ink-500 flex items-center justify-center">
                    <Icon size={17} aria-hidden="true" />
                </span>
                <div className="flex-1 min-w-0">
                    <p className="font-semibold text-sm text-ink-900 truncate m-0">{title}</p>
                    <p className="text-xs text-ink-500 truncate m-0">{subtitle}</p>
                </div>
                <Badge tone={item.is_active ? "green" : "gray"}>{item.is_active ? "Active" : "Paused"}</Badge>
                <Button
                    variant="ghost"
                    size="sm"
                    loading={busy}
                    onClick={toggle}
                    className="shrink-0"
                    title={item.is_active ? "Pause this automation" : "Activate this automation"}
                >
                    {item.is_active ? "Pause" : "Activate"}
                </Button>
                {onEdit && (
                    <Button
                        variant="ghost"
                        size="sm"
                        icon={Pencil}
                        className="shrink-0"
                        onClick={onEdit}
                        aria-label={`Edit ${title}`}
                    />
                )}
                {onDelete && (
                    <Button
                        variant="ghost"
                        size="sm"
                        icon={Trash2}
                        className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                        onClick={onDelete}
                        aria-label={`Delete ${title}`}
                    />
                )}
            </div>
            {children}
        </div>
    );
}

/** Template-or-custom message body, shared by rules, follow-ups and campaigns. */
function MessageContentFields({ form, setForm, templates, accounts = [] }) {
    // Which connection this rule sends on decides whether buttons arrive
    // as buttons or as a numbered list, so the builder is told.
    const provider = accounts.find(
        (account) => String(account.id) === String(form.whatsapp_account),
    )?.provider;

    const usingTemplate = Boolean(form.template);
    // A template brings its own buttons, so they are shown rather than edited
    // here -- editing them belongs on the template, where every message using
    // it picks the change up.
    const templateButtons = usingTemplate
        ? toButtons(templates.find((entry) => String(entry.id) === String(form.template)))
        : [];

    return (
        <>
            <Field
                label="Template"
                hint="Pick a saved template, or leave this blank and write the message below."
            >
                <Select
                    value={form.template}
                    onChange={(event) => setForm({ ...form, template: event.target.value })}
                >
                    <option value="">No template — write it here</option>
                    {templates.map((template) => (
                        <option key={template.id} value={template.id}>
                            {template.name}
                        </option>
                    ))}
                </Select>
            </Field>

            {!usingTemplate && (
                <>
                    <Field
                        label="Message type"
                        hint={form.media_items.length > 0 ? "Set by the first attachment below." : undefined}
                    >
                        <Select
                            value={form.message_type}
                            disabled={form.media_items.length > 0}
                            onChange={(event) => setForm({ ...form, message_type: event.target.value })}
                        >
                            {MESSAGE_TYPES.map((type) => (
                                <option key={type} value={type}>
                                    {type.charAt(0) + type.slice(1).toLowerCase()}
                                </option>
                            ))}
                        </Select>
                    </Field>

                    <Field
                        label="Message"
                        hint="Variables like [Name] are replaced with the contact's details when it sends."
                    >
                        <Textarea
                            value={form.message_body}
                            onChange={(event) => setForm({ ...form, message_body: event.target.value })}
                        />
                    </Field>

                    <MediaPicker
                        value={form.media_items}
                        onChange={(media_items) =>
                            setForm({
                                ...form,
                                media_items,
                                // Kept in step with what the API will store.
                                message_type: primaryMessageType(
                                    media_items,
                                    media_items.length ? form.message_type : "TEXT"
                                ),
                            })
                        }
                    />

                    <ButtonBuilder
                        value={form.buttons}
                        onChange={(buttons) => setForm({ ...form, buttons })}
                        provider={provider}
                        hint="Shown under the message. Add a rule triggered by a button id to answer a tap."
                    />
                </>
            )}

            {usingTemplate && templateButtons.length > 0 && (
                <div className="mb-4">
                    <span className="block text-sm font-semibold text-ink-700 mb-1.5">Buttons</span>
                    <ButtonPreview buttons={templateButtons} className="items-start" />
                    <span className="block text-xs text-ink-500 mt-1.5">
                        From this template — edit them on the template itself.
                    </span>
                </div>
            )}
        </>
    );
}

function AccountField({ value, onChange, accounts }) {
    return (
        <Field label="WhatsApp Device" required hint={accounts.length === 0 ? "Connect a device first." : undefined}>
            <Select value={value} onChange={onChange} required disabled={accounts.length === 0}>
                <option value="">Select...</option>
                {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                        {account.name}
                        {account.phone_number ? ` (${account.phone_number})` : ""}
                    </option>
                ))}
            </Select>
        </Field>
    );
}

export default function Automation() {
    const [tab, setTab] = useState("quick");
    const { hasFeature } = useEntitlements();
    const [accounts, setAccounts] = useState([]);
    const [templates, setTemplates] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    useEffect(() => {
        Promise.all([api.get("/whatsapp/accounts/"), api.get("/templates/templates/")])
            .then(([accountsRes, templatesRes]) => {
                setAccounts(accountsRes.data.results || []);
                setTemplates((templatesRes.data.results || []).filter((template) => template.is_active));
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load devices and templates.")))
            .finally(() => setLoading(false));
    }, []);

    if (loading) return <Spinner />;

    const shared = { accounts, templates };

    return (
        <div>
            <PageHeader
                title="Automation"
                description="Answer instantly, follow up automatically, and never lose a lead to silence."
            />

            <Alert>{error}</Alert>

            {accounts.length === 0 && (
                <Alert tone="warning" title="No WhatsApp device connected">
                    Automation runs against a connected device. <Link to="/whatsapp">Connect one</Link> before creating
                    rules — nothing sends until a device is connected.
                </Alert>
            )}

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "quick" && (
                <QuickSetup
                    {...shared}
                    onCreated={(kind) => setTab(kind === "FOLLOW_UP" ? "followups" : "rules")}
                />
            )}
            {tab === "rules" && <RulesTab {...shared} />}
            {tab === "followups" && <FollowUpsTab {...shared} />}
            {/* The workflow builder is the only part of this page behind a
                plan switch. Welcome messages, keyword replies and follow-ups
                are on every plan and keep working exactly as they did. */}
            {tab === "workflows" &&
                (hasFeature("workflows") ? (
                    <WorkflowsTab {...shared} />
                ) : (
                    <FeatureLocked
                        title="Workflows"
                        description="Build multi-step sequences: send a message, wait, branch on what the contact does, tag them, or hand the chat to an agent."
                    />
                ))}
        </div>
    );
}

function RulesTab({ accounts, templates }) {
    const toast = useToast();
    const { items: rules, loading, error, setError, reload, save, remove } = useAutomationResource("/automation/rules/");
    const bulk = useAutomationBulk("/automation/rules/bulk/", rules, reload, setError, toast);

    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);

    const openCreate = () =>
        setEditing({
            name: "",
            whatsapp_account: "",
            trigger_type: "WELCOME",
            keyword: "",
            keyword_match_type: "CONTAINS",
            button_id: "",
            priority: 0,
            cooldown_seconds: 0,
            is_active: true,
            ...EMPTY_CONTENT,
        });

    const openEdit = (rule) =>
        setEditing({
            id: rule.id,
            name: rule.name,
            whatsapp_account: rule.whatsapp_account,
            trigger_type: rule.trigger_type,
            keyword: rule.keyword || "",
            keyword_match_type: rule.keyword_match_type || "CONTAINS",
            button_id: rule.button_id || "",
            priority: rule.priority ?? 0,
            cooldown_seconds: rule.cooldown_seconds ?? 0,
            is_active: rule.is_active,
            ...contentFromRule(rule),
        });

    const toggleActive = async (rule, isActive) => {
        try {
            await save({ is_active: isActive }, rule.id);
            toast.success(isActive ? "Rule activated." : "Rule paused.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this rule."));
        }
    };

    const submit = async (form) => {
        await save(
            {
                name: form.name,
                whatsapp_account: Number(form.whatsapp_account),
                trigger_type: form.trigger_type,
                keyword: form.trigger_type === "KEYWORD" ? form.keyword : "",
                keyword_match_type: form.keyword_match_type,
                button_id: form.trigger_type === "BUTTON" ? form.button_id : "",
                priority: Number(form.priority) || 0,
                cooldown_seconds: Math.max(0, Number(form.cooldown_seconds) || 0),
                is_active: form.is_active,
                ...contentPayload(form),
            },
            form.id
        );
        toast.success(form.id ? "Rule updated." : "Automation rule created.");
        setEditing(null);
    };

    const confirmDelete = async (rule) => {
        try {
            await remove(rule.id);
            toast.success("Rule deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this rule."));
        } finally {
            setPendingDelete(null);
        }
    };

    const describe = (rule) => {
        if (rule.trigger_type === "WELCOME") return "Welcome message — sent the first time a contact writes in";
        if (rule.trigger_type === "BUTTON") return `Sent when a contact taps the button “${rule.button_id}”`;
        const match = MATCH_TYPES.find((entry) => entry.value === rule.keyword_match_type)?.label.toLowerCase();
        return `Auto-reply when a message ${match} “${rule.keyword}” · priority ${rule.priority}`;
    };

    if (loading) return <Spinner />;

    return (
        <div>
            <div className="flex justify-between items-center gap-3 mb-4">
                <p className="text-sm text-ink-500 m-0">
                    Reply automatically when someone messages you for the first time, when their message matches a
                    keyword, or when they tap one of your buttons. The highest-priority matching keyword rule wins,
                    and a tapped button is answered by its own rule instead of a keyword one.
                </p>
                <Button icon={Plus} onClick={openCreate} className="shrink-0">
                    New Rule
                </Button>
            </div>

            <Alert>{error}</Alert>

            <BulkListHeader
                selection={bulk.selection}
                items={rules}
                label="rule"
                busy={bulk.busy}
                onAction={bulk.run}
                onDelete={() => bulk.setConfirming(true)}
            />

            <div className="space-y-3">
                {rules.map((rule) => (
                    <AutomationRow
                        key={rule.id}
                        item={rule}
                        icon={TRIGGER_ICONS[rule.trigger_type] || MessageCircleReply}
                        title={rule.name}
                        subtitle={describe(rule)}
                        selected={bulk.selection.isSelected(rule.id)}
                        onToggleSelect={() => bulk.selection.toggle(rule.id)}
                        onToggle={toggleActive}
                        onEdit={() => openEdit(rule)}
                        onDelete={() => setPendingDelete(rule)}
                    />
                ))}
                {rules.length === 0 && (
                    <EmptyState
                        icon={MessageCircleReply}
                        title="No automation rules yet"
                        description="Add a welcome message or a keyword auto-reply so contacts get an instant response."
                        action={
                            <Button icon={Plus} onClick={openCreate}>
                                New Rule
                            </Button>
                        }
                    />
                )}
            </div>

            {editing && (
                <RuleModal
                    initial={editing}
                    accounts={accounts}
                    templates={templates}
                    onSubmit={submit}
                    onClose={() => setEditing(null)}
                />
            )}

            {bulk.confirming && (
                <ConfirmDialog
                    title={`Delete ${bulk.selection.selectedCount} rule${bulk.selection.selectedCount === 1 ? "" : "s"}`}
                    message={`Delete the ${bulk.selection.selectedCount} selected rule${
                        bulk.selection.selectedCount === 1 ? "" : "s"
                    }? Contacts will no longer get these automatic replies.`}
                    confirmLabel={`Delete ${bulk.selection.selectedCount}`}
                    onConfirm={() => bulk.run("delete")}
                    onClose={() => bulk.setConfirming(false)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete rule"
                    message={`Delete “${pendingDelete.name}”? Contacts will no longer get this automatic reply.`}
                    confirmLabel="Delete rule"
                    onConfirm={() => confirmDelete(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

function RuleModal({ initial, accounts, templates, onSubmit, onClose }) {
    const [form, setForm] = useState(initial);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    // Every reply button defined on a template, so a BUTTON rule can be
    // pointed at one by picking rather than by remembering its id.
    const knownButtonIds = [...new Set(templates.flatMap((template) => replyButtonIds(template.buttons)))];

    const submit = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            await onSubmit(form);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save this rule."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title={form.id ? "Edit Automation Rule" : "New Automation Rule"} onClose={onClose} width="lg">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Name" required>
                    <Input
                        value={form.name}
                        onChange={(event) => setForm({ ...form, name: event.target.value })}
                        autoFocus
                        required
                    />
                </Field>

                <AccountField
                    value={form.whatsapp_account}
                    onChange={(event) => setForm({ ...form, whatsapp_account: event.target.value })}
                    accounts={accounts}
                />

                <Field label="Trigger">
                    <Select
                        value={form.trigger_type}
                        onChange={(event) => setForm({ ...form, trigger_type: event.target.value })}
                    >
                        <option value="WELCOME">Welcome message (first contact)</option>
                        <option value="KEYWORD">Keyword match (auto-reply)</option>
                        <option value="BUTTON">Button tapped</option>
                    </Select>
                </Field>

                {form.trigger_type === "BUTTON" && (
                    <Field
                        label="Button id"
                        required
                        hint="The id of the reply button this rule answers — pick one already used on a template, or type your own."
                    >
                        <Input
                            value={form.button_id}
                            list="automation-button-ids"
                            className="font-mono"
                            placeholder="rsvp_yes"
                            onChange={(event) => setForm({ ...form, button_id: event.target.value })}
                            required
                        />
                        <datalist id="automation-button-ids">
                            {knownButtonIds.map((id) => (
                                <option key={id} value={id} />
                            ))}
                        </datalist>
                    </Field>
                )}

                {form.trigger_type === "KEYWORD" && (
                    <>
                        <Field label="Keyword" required>
                            <Input
                                value={form.keyword}
                                onChange={(event) => setForm({ ...form, keyword: event.target.value })}
                                required
                            />
                        </Field>
                        <Field label="Match type">
                            <Select
                                value={form.keyword_match_type}
                                onChange={(event) => setForm({ ...form, keyword_match_type: event.target.value })}
                            >
                                {MATCH_TYPES.map((entry) => (
                                    <option key={entry.value} value={entry.value}>
                                        {entry.label}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                        <Field label="Priority" hint="Higher numbers are checked first when several keywords match.">
                            <Input
                                type="number"
                                value={form.priority}
                                onChange={(event) => setForm({ ...form, priority: event.target.value })}
                            />
                        </Field>
                    </>
                )}

                <Field
                    label="Don't repeat within (seconds)"
                    hint="0 answers every time. Set this so a contact who sends the same thing twice doesn't get two identical replies."
                >
                    <Input
                        type="number"
                        min="0"
                        value={form.cooldown_seconds}
                        onChange={(event) => setForm({ ...form, cooldown_seconds: event.target.value })}
                    />
                </Field>

                <MessageContentFields form={form} setForm={setForm} templates={templates} accounts={accounts} />

                <div className="mb-5">
                    <Toggle
                        checked={form.is_active}
                        onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                        label="Active"
                        description="Paused rules stay saved but never fire."
                    />
                </div>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Create rule"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

function FollowUpsTab({ accounts, templates }) {
    const toast = useToast();
    const { items: rules, loading, error, setError, reload, save, remove } = useAutomationResource("/automation/follow-ups/");
    const bulk = useAutomationBulk("/automation/follow-ups/bulk/", rules, reload, setError, toast);

    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);

    const openCreate = () =>
        setEditing({
            name: "", whatsapp_account: "", delay_minutes: 1440, trigger_templates: [], is_active: true,
            ...EMPTY_CONTENT,
        });

    const openEdit = (rule) =>
        setEditing({
            id: rule.id,
            name: rule.name,
            whatsapp_account: rule.whatsapp_account,
            delay_minutes: rule.delay_minutes,
            trigger_templates: rule.trigger_templates || [],
            is_active: rule.is_active,
            ...contentFromRule(rule),
        });

    const toggleActive = async (rule, isActive) => {
        try {
            await save({ is_active: isActive }, rule.id);
            toast.success(isActive ? "Follow-up activated." : "Follow-up paused.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this follow-up."));
        }
    };

    const submit = async (form) => {
        await save(
            {
                name: form.name,
                whatsapp_account: Number(form.whatsapp_account),
                delay_minutes: Number(form.delay_minutes),
                trigger_templates: (form.trigger_templates || []).map(Number),
                is_active: form.is_active,
                ...contentPayload(form),
            },
            form.id
        );
        toast.success(form.id ? "Follow-up updated." : "Follow-up created.");
        setEditing(null);
    };

    const confirmDelete = async (rule) => {
        try {
            await remove(rule.id);
            toast.success("Follow-up deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this follow-up."));
        } finally {
            setPendingDelete(null);
        }
    };

    const describeDelay = (minutes) => {
        if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes / 1440 === 1 ? "" : "s"}`;
        if (minutes % 60 === 0) return `${minutes / 60} hour${minutes / 60 === 1 ? "" : "s"}`;
        return `${minutes} minutes`;
    };

    const describeScope = (rule) => {
        const ids = rule.trigger_templates || [];
        if (ids.length === 0) return "your message";
        if (ids.length === 1) {
            const match = templates.find((t) => t.id === ids[0]);
            return match ? `your “${match.name}” message` : "your message";
        }
        return `one of ${ids.length} selected templates`;
    };

    if (loading) return <Spinner />;

    return (
        <div>
            <div className="flex justify-between items-center gap-3 mb-4">
                <p className="text-sm text-ink-500 m-0">
                    Nudge contacts who never replied. The timer starts when one of your messages is sent, and the
                    follow-up is skipped if they reply first.
                </p>
                <Button icon={Plus} onClick={openCreate} className="shrink-0">
                    New Follow-up
                </Button>
            </div>

            <Alert>{error}</Alert>

            <BulkListHeader
                selection={bulk.selection}
                items={rules}
                label="follow-up"
                busy={bulk.busy}
                onAction={bulk.run}
                onDelete={() => bulk.setConfirming(true)}
            />

            <div className="space-y-3">
                {rules.map((rule) => (
                    <AutomationRow
                        key={rule.id}
                        item={rule}
                        icon={Clock}
                        title={rule.name}
                        subtitle={`Sent ${describeDelay(rule.delay_minutes)} after ${describeScope(rule)}, if there is no reply`}
                        selected={bulk.selection.isSelected(rule.id)}
                        onToggleSelect={() => bulk.selection.toggle(rule.id)}
                        onToggle={toggleActive}
                        onEdit={() => openEdit(rule)}
                        onDelete={() => setPendingDelete(rule)}
                    />
                ))}
                {rules.length === 0 && (
                    <EmptyState
                        icon={Clock}
                        title="No follow-up rules yet"
                        description="Create a follow-up so contacts who never replied get a second message automatically."
                        action={
                            <Button icon={Plus} onClick={openCreate}>
                                New Follow-up
                            </Button>
                        }
                    />
                )}
            </div>

            {editing && (
                <FollowUpModal
                    initial={editing}
                    accounts={accounts}
                    templates={templates}
                    onSubmit={submit}
                    onClose={() => setEditing(null)}
                />
            )}

            {bulk.confirming && (
                <ConfirmDialog
                    title={`Delete ${bulk.selection.selectedCount} follow-up${bulk.selection.selectedCount === 1 ? "" : "s"}`}
                    message={`Delete the ${bulk.selection.selectedCount} selected follow-up${
                        bulk.selection.selectedCount === 1 ? "" : "s"
                    }? Follow-ups already scheduled for these rules stop firing.`}
                    confirmLabel={`Delete ${bulk.selection.selectedCount}`}
                    onConfirm={() => bulk.run("delete")}
                    onClose={() => bulk.setConfirming(false)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete follow-up"
                    message={`Delete “${pendingDelete.name}”? Follow-ups already scheduled for this rule stop firing.`}
                    confirmLabel="Delete follow-up"
                    onConfirm={() => confirmDelete(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

/**
 * Which outbound templates arm this follow-up. Nothing checked means "every
 * message" -- the same behaviour a follow-up rule has always had, so leaving
 * this alone changes nothing for a tenant who only ever sends one template.
 * Checking specific templates is what lets several campaigns share one
 * WhatsApp number without their follow-ups nudging each other's contacts.
 */
function TemplateScopeField({ value, onChange, templates }) {
    const selected = value || [];
    const toggle = (id) =>
        onChange(selected.includes(id) ? selected.filter((entry) => entry !== id) : [...selected, id]);

    return (
        <Field
            label="Only follow up after these templates"
            hint={
                templates.length === 0
                    ? "You have no saved templates yet, so this follow-up arms after any message you send."
                    : selected.length === 0
                      ? "Nothing selected: this follow-up arms after any message you send, template or not."
                      : "This follow-up arms only when the message it's following up on used one of these."
            }
        >
            {templates.length > 0 && (
                <div className="max-h-40 overflow-y-auto rounded-lg border border-ink-200 divide-y divide-ink-100">
                    {templates.map((template) => (
                        <CheckboxField
                            key={template.id}
                            className="px-3 py-2"
                            checked={selected.includes(template.id)}
                            onChange={() => toggle(template.id)}
                            label={template.name}
                        />
                    ))}
                </div>
            )}
        </Field>
    );
}

function FollowUpModal({ initial, accounts, templates, onSubmit, onClose }) {
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
            setError(apiErrorMessage(err, "Unable to save this follow-up."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title={form.id ? "Edit Follow-up" : "New Follow-up Rule"} onClose={onClose} width="lg">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Name" required>
                    <Input
                        value={form.name}
                        onChange={(event) => setForm({ ...form, name: event.target.value })}
                        autoFocus
                        required
                    />
                </Field>

                <AccountField
                    value={form.whatsapp_account}
                    onChange={(event) => setForm({ ...form, whatsapp_account: event.target.value })}
                    accounts={accounts}
                />

                <Field label="Wait (minutes)" hint="1440 = 1 day" required>
                    <Input
                        type="number"
                        min={1}
                        value={form.delay_minutes}
                        onChange={(event) => setForm({ ...form, delay_minutes: event.target.value })}
                        required
                    />
                </Field>

                <TemplateScopeField
                    value={form.trigger_templates}
                    onChange={(trigger_templates) => setForm({ ...form, trigger_templates })}
                    templates={templates}
                />

                <MessageContentFields form={form} setForm={setForm} templates={templates} accounts={accounts} />

                <div className="mb-5">
                    <Toggle
                        checked={form.is_active}
                        onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                        label="Active"
                        description="Paused follow-ups stay saved but are never scheduled."
                    />
                </div>

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Create follow-up"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

function WorkflowsTab({ accounts, templates }) {
    const toast = useToast();
    const { items: workflows, loading, error, setError, reload, save, remove } = useAutomationResource("/automation/workflows/");
    const bulk = useAutomationBulk("/automation/workflows/bulk/", workflows, reload, setError, toast);

    // Only the TAG and ASSIGN_AGENT inspectors need these, so a failure here
    // leaves those two dropdowns empty rather than blocking the whole tab.
    const [tags, setTags] = useState([]);
    const [agents, setAgents] = useState([]);

    useEffect(() => {
        api.get("/contacts/tags/")
            .then((response) => setTags(response.data.results || []))
            .catch(() => setTags([]));
        api.get("/teams/members/")
            .then((response) => setAgents(response.data.results || []))
            .catch(() => setAgents([]));
    }, []);

    const [editing, setEditing] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [running, setRunning] = useState(null);
    const [runsFor, setRunsFor] = useState(null);

    const openCreate = () => setEditing({ name: "", whatsapp_account: "", nodes: WORKFLOW_TEMPLATE, is_active: true });

    const openEdit = (workflow) =>
        setEditing({
            id: workflow.id,
            name: workflow.name,
            whatsapp_account: workflow.whatsapp_account,
            nodes: workflow.nodes,
            is_active: workflow.is_active,
        });

    const toggleActive = async (workflow, isActive) => {
        try {
            await save({ is_active: isActive }, workflow.id);
            toast.success(isActive ? "Workflow activated." : "Workflow paused.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this workflow."));
        }
    };

    const submit = async (form) => {
        await save(
            {
                name: form.name,
                whatsapp_account: Number(form.whatsapp_account),
                nodes: form.nodes,
                is_active: form.is_active,
            },
            form.id
        );
        toast.success(form.id ? "Workflow updated." : "Workflow created.");
        setEditing(null);
    };

    const confirmDelete = async (workflow) => {
        try {
            await remove(workflow.id);
            toast.success("Workflow deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this workflow."));
        } finally {
            setPendingDelete(null);
        }
    };

    if (loading) return <Spinner />;

    return (
        <div>
            <Alert tone="info" title="Build a workflow on the canvas">
                Drag steps around, and join one to the next by pulling from the dot on a card&rsquo;s right edge. A
                contact enters at the trigger and follows the arrows. The raw JSON is still there behind{" "}
                <strong>Edit as JSON</strong> if you would rather type it.
            </Alert>

            <Alert>{error}</Alert>

            <div className="flex justify-between items-center gap-3 mb-4">
                <p className="text-sm text-ink-500 m-0">
                    A workflow runs per contact: start one manually here, then watch its run progress below.
                </p>
                <Button icon={Plus} onClick={openCreate} className="shrink-0">
                    New Workflow
                </Button>
            </div>

            <BulkListHeader
                selection={bulk.selection}
                items={workflows}
                label="workflow"
                busy={bulk.busy}
                onAction={bulk.run}
                onDelete={() => bulk.setConfirming(true)}
            />

            <div className="space-y-3">
                {workflows.map((workflow) => {
                    const account = accounts.find((entry) => entry.id === workflow.whatsapp_account);
                    return (
                        <AutomationRow
                            key={workflow.id}
                            item={workflow}
                            icon={GitBranch}
                            title={workflow.name}
                            subtitle={`${workflow.nodes.length} node${workflow.nodes.length === 1 ? "" : "s"}${
                                account ? ` · ${account.name}` : ""
                            }`}
                            selected={bulk.selection.isSelected(workflow.id)}
                            onToggleSelect={() => bulk.selection.toggle(workflow.id)}
                            onToggle={toggleActive}
                            onEdit={() => openEdit(workflow)}
                            onDelete={() => setPendingDelete(workflow)}
                        >
                            <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-ink-100">
                                <Button size="sm" variant="secondary" icon={Play} onClick={() => setRunning(workflow)}>
                                    Run for a contact
                                </Button>
                                <Button
                                    size="sm"
                                    variant="ghost"
                                    icon={History}
                                    onClick={() => setRunsFor(runsFor?.id === workflow.id ? null : workflow)}
                                >
                                    {runsFor?.id === workflow.id ? "Hide runs" : "Recent runs"}
                                </Button>
                            </div>
                            {runsFor?.id === workflow.id && <WorkflowRuns workflow={workflow} />}
                        </AutomationRow>
                    );
                })}
                {workflows.length === 0 && (
                    <EmptyState
                        icon={Bot}
                        title="No workflows yet"
                        description="Build a multi-step conversation flow with messages, buttons, waits and conditions."
                        action={
                            <Button icon={Plus} onClick={openCreate}>
                                New Workflow
                            </Button>
                        }
                    />
                )}
            </div>

            {editing && (
                <WorkflowModal
                    initial={editing}
                    accounts={accounts}
                    templates={templates}
                    tags={tags}
                    agents={agents}
                    onSubmit={submit}
                    onClose={() => setEditing(null)}
                />
            )}

            {running && <RunWorkflowModal workflow={running} onClose={() => setRunning(null)} />}

            {bulk.confirming && (
                <ConfirmDialog
                    title={`Delete ${bulk.selection.selectedCount} workflow${bulk.selection.selectedCount === 1 ? "" : "s"}`}
                    message={`Delete the ${bulk.selection.selectedCount} selected workflow${
                        bulk.selection.selectedCount === 1 ? "" : "s"
                    }? Runs already in progress stop advancing.`}
                    confirmLabel={`Delete ${bulk.selection.selectedCount}`}
                    onConfirm={() => bulk.run("delete")}
                    onClose={() => bulk.setConfirming(false)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete workflow"
                    message={`Delete “${pendingDelete.name}”? Runs already in progress will stop advancing.`}
                    confirmLabel="Delete workflow"
                    onConfirm={() => confirmDelete(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

function WorkflowModal({ initial, accounts, templates, tags, agents, onSubmit, onClose }) {
    const [form, setForm] = useState(initial);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    // The canvas reads its `value` once, on mount, so that dragging a node
    // does not fight the parent's state. Coming back from the JSON editor
    // therefore has to remount it -- hence a key that changes each time.
    const [jsonText, setJsonText] = useState(null);
    const [canvasKey, setCanvasKey] = useState(0);

    const editingJson = jsonText !== null;
    const problems = editingJson ? [] : validateGraph(form.nodes);

    const setNodes = useCallback((nodes) => setForm((current) => ({ ...current, nodes })), []);

    const openJson = () => {
        setJsonText(JSON.stringify(form.nodes, null, 2));
        setError("");
    };

    /** Returns the parsed nodes, or null after reporting the parse failure. */
    const parseJson = () => {
        try {
            const parsed = JSON.parse(jsonText);
            if (!Array.isArray(parsed)) {
                setError("The node graph has to be a list of nodes.");
                return null;
            }
            return parsed;
        } catch {
            setError("The node graph is not valid JSON.");
            return null;
        }
    };

    const closeJson = () => {
        const parsed = parseJson();
        if (parsed === null) return;
        setNodes(parsed);
        setJsonText(null);
        setCanvasKey((key) => key + 1);
        setError("");
    };

    const submit = async (event) => {
        event.preventDefault();

        // Saving straight from the JSON editor is allowed -- it would be
        // irritating to be made to switch back first -- so parse it here too.
        let payload = form;
        if (editingJson) {
            const parsed = parseJson();
            if (parsed === null) return;
            payload = { ...form, nodes: parsed };
        }

        setSaving(true);
        setError("");
        try {
            await onSubmit(payload);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save this workflow."));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal title={form.id ? "Edit Workflow" : "New Workflow"} onClose={onClose} width="max-w-6xl">
            <form onSubmit={submit}>
                <Alert>{error}</Alert>

                <Field label="Name" required>
                    <Input
                        value={form.name}
                        onChange={(event) => setForm({ ...form, name: event.target.value })}
                        autoFocus
                        required
                    />
                </Field>

                <AccountField
                    value={form.whatsapp_account}
                    onChange={(event) => setForm({ ...form, whatsapp_account: event.target.value })}
                    accounts={accounts}
                />

                <div className="flex items-end justify-between gap-3 mb-1.5">
                    <span className="text-sm font-semibold text-ink-800">Steps</span>
                    <Button type="button" size="xs" variant="ghost" icon={Code} onClick={editingJson ? closeJson : openJson}>
                        {editingJson ? "Back to canvas" : "Edit as JSON"}
                    </Button>
                </div>

                {editingJson ? (
                    <Field hint="Exactly one TRIGGER node; every next/on_true/on_false must point at an existing node id. A MESSAGE node can carry buttons, and a CONDITION on button_clicked branches on which one was tapped.">
                        <Textarea
                            value={jsonText}
                            onChange={(event) => setJsonText(event.target.value)}
                            rows={16}
                            className="font-mono text-xs"
                        />
                    </Field>
                ) : (
                    <div className="mb-4">
                        <WorkflowBuilder
                            key={canvasKey}
                            value={form.nodes}
                            onChange={setNodes}
                            tags={tags}
                            agents={agents}
                            templates={templates}
                        />
                    </div>
                )}

                <div className="mb-5">
                    <Toggle
                        checked={form.is_active}
                        onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                        label="Active"
                        description="A paused workflow cannot be started for a contact."
                    />
                </div>

                <div className="flex gap-2 justify-end items-center">
                    {problems.length > 0 && (
                        <span className="text-xs text-amber-700 mr-auto">
                            {problems.length} thing{problems.length === 1 ? "" : "s"} to fix before this will run.
                        </span>
                    )}
                    <Button type="button" variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button type="submit" loading={saving}>
                        {form.id ? "Save changes" : "Create workflow"}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}

/** Recent runs for one workflow, loaded on demand. */
function WorkflowRuns({ workflow }) {
    const [runs, setRuns] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/automation/workflow-runs/", { params: { workflow: workflow.id, ordering: "-started_at" } })
            .then((response) => setRuns(response.data.results || []))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load runs.")));
    }, [workflow.id]);

    if (error) return <Alert className="mt-3">{error}</Alert>;
    if (runs === null) return <Spinner size="sm" />;
    if (runs.length === 0) {
        return <p className="text-xs text-ink-500 mt-3 mb-0">This workflow hasn&rsquo;t run yet.</p>;
    }

    return (
        <ul className="list-none p-0 m-0 mt-3 space-y-1.5">
            {runs.map((run) => (
                <li key={run.id} className="flex items-center gap-2 text-xs text-ink-600">
                    <StatusBadge status={run.status} />
                    <span>Contact #{run.contact}</span>
                    <span className="text-ink-400">
                        {run.current_node_id ? `at “${run.current_node_id}”` : ""} ·{" "}
                        {new Date(run.started_at).toLocaleString()}
                    </span>
                </li>
            ))}
        </ul>
    );
}

/** Starts a workflow for one contact, found by name or number. */
function RunWorkflowModal({ workflow, onClose }) {
    const toast = useToast();
    const [search, setSearch] = useState("");
    const [contacts, setContacts] = useState([]);
    const [contactId, setContactId] = useState("");
    const [searching, setSearching] = useState(false);
    const [starting, setStarting] = useState(false);
    const [error, setError] = useState("");

    const find = async (event) => {
        event.preventDefault();
        setSearching(true);
        setError("");
        try {
            const response = await api.get("/contacts/contacts/", { params: { search, ordering: "name" } });
            setContacts(response.data.results || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to search contacts."));
        } finally {
            setSearching(false);
        }
    };

    const start = async () => {
        setStarting(true);
        setError("");
        try {
            await api.post(`/automation/workflows/${workflow.id}/run/`, { contact_id: Number(contactId) });
            toast.success("Workflow started.");
            onClose();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to start this workflow."));
        } finally {
            setStarting(false);
        }
    };

    return (
        <Modal title={`Run “${workflow.name}”`} description="Pick the contact this run is for." onClose={onClose}>
            <Alert>{error}</Alert>

            <form onSubmit={find} className="flex gap-2 items-end mb-4">
                <Field label="Find a contact" className="mb-0 flex-1">
                    <Input
                        value={search}
                        onChange={(event) => setSearch(event.target.value)}
                        placeholder="Name or number"
                        autoFocus
                    />
                </Field>
                <Button type="submit" variant="secondary" loading={searching}>
                    Search
                </Button>
            </form>

            <Field label="Contact" required>
                <Select value={contactId} onChange={(event) => setContactId(event.target.value)} required>
                    <option value="">
                        {contacts.length === 0 ? "Search for a contact first..." : "Select a contact..."}
                    </option>
                    {contacts.map((contact) => (
                        <option key={contact.id} value={contact.id}>
                            {contact.name || "Unnamed"} — {contact.phone}
                        </option>
                    ))}
                </Select>
            </Field>

            {!workflow.is_active && (
                <Alert tone="warning">This workflow is paused — activate it before starting a run.</Alert>
            )}

            <div className="flex gap-2 justify-end">
                <Button type="button" variant="secondary" onClick={onClose}>
                    Cancel
                </Button>
                <Button
                    icon={Play}
                    onClick={start}
                    loading={starting}
                    disabled={!contactId || !workflow.is_active}
                >
                    Start run
                </Button>
            </div>
        </Modal>
    );
}
