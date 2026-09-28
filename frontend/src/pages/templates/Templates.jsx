import { useEffect, useState } from "react";
import { Braces, Eye, FileText, Pencil, Plus, Power, PowerOff, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import { contentKind } from "../../utils/interactive";
import useSelection from "../../hooks/useSelection";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Modal from "../../components/ui/Modal";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import { CardGridSkeleton } from "../../components/ui/Skeleton";
import BulkActionBar from "../../components/ui/BulkActionBar";
import WhatsAppPreview from "../../components/ui/WhatsAppPreview";
import TemplateApiPanel from "../../components/ui/TemplateApiPanel";
import TemplateEditor, { EMPTY_TEMPLATE, formToPayload, templateToForm } from "./TemplateEditor";
import { Checkbox, Field, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

const TYPE_TONE = { TEXT: "gray", IMAGE: "blue", VIDEO: "violet", DOCUMENT: "amber" };

/** How each content kind is labelled on a template card. */
const KIND_TONE = { TEXT: "gray", BUTTON: "blue", LIST: "violet", POLL: "green" };
const KIND_LABEL = { TEXT: "Text", BUTTON: "Buttons", LIST: "Menu", POLL: "Poll" };

export default function Templates() {
    const toast = useToast();
    const [templates, setTemplates] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [showModal, setShowModal] = useState(false);
    const [editing, setEditing] = useState(null);
    const [form, setForm] = useState(EMPTY_TEMPLATE);
    const [formError, setFormError] = useState("");
    const [saving, setSaving] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(null);

    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);
    const selection = useSelection(templates);

    // Which template's API JSON is open. Held separately from `preview`
    // because the two modals answer different questions -- "what will my
    // customer see?" and "what do I POST to send this?" -- and somebody
    // usually wants one or the other, not both at once.
    const [apiFor, setApiFor] = useState(null);

    const [preview, setPreview] = useState(null);
    const [previewSample, setPreviewSample] = useState("{}");
    const [previewResult, setPreviewResult] = useState(null);
    const [previewError, setPreviewError] = useState("");
    const [previewing, setPreviewing] = useState(false);

    // The composer needs both to do its job: which variables this workspace's
    // own contacts actually carry, and what each connected number can deliver.
    // Fetched once here rather than per keystroke in the editor.
    const [variables, setVariables] = useState({ builtin: [], custom: [] });
    const [accounts, setAccounts] = useState([]);

    const load = async () => {
        setLoading(true);
        try {
            const response = await api.get("/templates/templates/");
            setTemplates(response.data.results || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load templates."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
        // Neither is essential to editing a template, so a failure on either
        // leaves the composer working with a fixed variable list and no
        // per-number preview rather than blocking the page.
        api.get("/templates/templates/variables/")
            .then((response) => setVariables(response.data))
            .catch(() => {});
        api.get("/whatsapp/accounts/capabilities/")
            .then((response) => setAccounts(response.data.accounts || []))
            .catch(() => {});
    }, []);

    const openCreate = () => {
        setEditing(null);
        setForm(EMPTY_TEMPLATE);
        setFormError("");
        setShowModal(true);
    };

    const openEdit = (template) => {
        setEditing(template);
        setForm(templateToForm(template));
        setFormError("");
        setShowModal(true);
    };

    const save = async (event) => {
        event.preventDefault();
        setFormError("");
        setSaving(true);
        try {
            // Every content kind is sent, including the empty ones: switching
            // a template from a menu to a poll has to *clear* the menu, and
            // omitting the field would leave the old one in place and have the
            // save refused for carrying two.
            const payload = formToPayload(form);
            if (editing) {
                await api.patch(`/templates/templates/${editing.id}/`, payload);
            } else {
                await api.post("/templates/templates/", payload);
            }
            setShowModal(false);
            toast.success(editing ? "Template updated." : "Template created.");
            await load();
        } catch (err) {
            setFormError(apiErrorMessage(err, "Unable to save this template."));
        } finally {
            setSaving(false);
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const ids = selection.selectedIds;
        const result = await runBulk("/templates/templates/bulk/", { action, ids });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0) {
            setError(
                describeBulkFailures(result, (id) => templates.find((template) => template.id === id)?.name)
            );
        }
        selection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        await load();
    };

    const remove = async (template) => {
        try {
            await api.delete(`/templates/templates/${template.id}/`);
            toast.success(`Template “${template.name}” deleted.`);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this template."));
        } finally {
            setPendingDelete(null);
            await load();
        }
    };

    const openPreview = (template) => {
        setPreview(template);
        setPreviewSample("{}");
        setPreviewResult(null);
        setPreviewError("");
    };

    const runPreview = async () => {
        setPreviewError("");
        let sampleData;
        try {
            sampleData = JSON.parse(previewSample || "{}");
        } catch {
            setPreviewError("Sample data must be valid JSON, e.g. {\"Name\": \"Priya\"}");
            return;
        }
        setPreviewing(true);
        try {
            const response = await api.post(`/templates/templates/${preview.id}/preview/`, { sample_data: sampleData });
            setPreviewResult(response.data);
        } catch (err) {
            setPreviewError(apiErrorMessage(err, "Unable to render preview."));
        } finally {
            setPreviewing(false);
        }
    };

    return (
        <div>
            <PageHeader
                title="Templates"
                description="Reusable, personalized messages for campaigns and automation."
                actions={
                    <Button icon={Plus} onClick={openCreate}>
                        New Template
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            {loading ? (
                <CardGridSkeleton count={4} className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" />
            ) : templates.length === 0 ? (
                <EmptyState
                    icon={FileText}
                    title="No templates yet"
                    description="Create a template to reuse the same message across campaigns and automation rules."
                    action={
                        <Button icon={Plus} onClick={openCreate}>
                            New Template
                        </Button>
                    }
                />
            ) : (
                <>
                    {/* Card grids get their own select-all: there is no table
                        header to hang it off. */}
                    <label className="inline-flex items-center gap-2 mb-3 text-sm font-medium text-ink-600 cursor-pointer select-none">
                        <Checkbox
                            checked={selection.allSelected}
                            indeterminate={selection.someSelected}
                            onChange={selection.toggleAll}
                            aria-label={selection.allSelected ? "Deselect all templates" : "Select all templates"}
                        />
                        Select all {templates.length} template{templates.length === 1 ? "" : "s"}
                    </label>

                    <BulkActionBar count={selection.selectedCount} label="template" onClear={selection.clear}>
                        <Button
                            size="sm"
                            variant="secondary"
                            icon={Power}
                            disabled={bulkBusy}
                            onClick={() => runBulkAction("activate")}
                        >
                            Activate
                        </Button>
                        <Button
                            size="sm"
                            variant="secondary"
                            icon={PowerOff}
                            disabled={bulkBusy}
                            onClick={() => runBulkAction("deactivate")}
                        >
                            Deactivate
                        </Button>
                        <Button
                            size="sm"
                            variant="danger"
                            icon={Trash2}
                            disabled={bulkBusy}
                            onClick={() => setPendingBulk("delete")}
                        >
                            Delete
                        </Button>
                    </BulkActionBar>

                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {templates.map((template) => (
                        <div
                            key={template.id}
                            className={`bg-surface border rounded-card shadow-card p-5 flex flex-col transition-colors ${
                                selection.isSelected(template.id)
                                    ? "border-brand-400 ring-1 ring-brand-500/20"
                                    : "border-ink-200 hover:border-ink-300"
                            }`}
                        >
                            <div className="flex items-start justify-between gap-3 mb-2">
                                <Checkbox
                                    checked={selection.isSelected(template.id)}
                                    onChange={() => selection.toggle(template.id)}
                                    className="mt-0.5"
                                    aria-label={`Select ${template.name}`}
                                />
                                <h3 className="text-sm font-bold text-ink-900 m-0 truncate flex-1">{template.name}</h3>
                                {!template.is_active && <Badge tone="gray">Paused</Badge>}
                                {/* Two different facts: what kind of message
                                    this is, and what it carries. A menu with a
                                    picture is both, and collapsing them into
                                    one badge would hide whichever lost. */}
                                {contentKind(template) !== "TEXT" && (
                                    <Badge tone={KIND_TONE[contentKind(template)]}>
                                        {KIND_LABEL[contentKind(template)]}
                                    </Badge>
                                )}
                                {template.message_type !== "TEXT" && (
                                    <Badge tone={TYPE_TONE[template.message_type] || "gray"}>
                                        {template.message_type}
                                    </Badge>
                                )}
                            </div>

                            <p className="text-sm text-ink-600 line-clamp-3 mb-3 flex-1 whitespace-pre-wrap">
                                {template.body || <em className="text-ink-400">No text body</em>}
                            </p>

                            {template.buttons?.length > 0 && (
                                <div className="flex gap-1 flex-wrap mb-3">
                                    {template.buttons.map((button, index) => (
                                        <span
                                            key={button.id || index}
                                            className="text-xs font-medium bg-brand-50 text-brand-700 ring-1 ring-inset ring-brand-600/20 px-1.5 py-0.5 rounded"
                                        >
                                            {button.title}
                                        </span>
                                    ))}
                                </div>
                            )}

                            {template.detected_variables?.length > 0 && (
                                <div className="flex gap-1 flex-wrap mb-3">
                                    {template.detected_variables.map((variable) => (
                                        <span
                                            key={variable}
                                            className="text-xs font-mono bg-ink-100 text-ink-600 px-1.5 py-0.5 rounded"
                                        >
                                            {`{{${variable}}}`}
                                        </span>
                                    ))}
                                </div>
                            )}

                            <div className="flex gap-2 pt-3 border-t border-ink-100">
                                <Button variant="secondary" size="sm" icon={Eye} onClick={() => openPreview(template)}>
                                    Preview
                                </Button>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={Braces}
                                    onClick={() => setApiFor(template)}
                                    title="The JSON to send this template through the API"
                                >
                                    API JSON
                                </Button>
                                <Button variant="ghost" size="sm" icon={Pencil} onClick={() => openEdit(template)}>
                                    Edit
                                </Button>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={Trash2}
                                    className="ml-auto text-red-600 hover:bg-red-50 hover:text-red-700"
                                    onClick={() => setPendingDelete(template)}
                                    aria-label={`Delete ${template.name}`}
                                />
                            </div>
                        </div>
                    ))}
                </div>
                </>
            )}

            {showModal && (
                <Modal
                    title={editing ? "Edit template" : "New template"}
                    description="Pick a message type, fill in what it needs, and watch it build on the right."
                    width="max-w-5xl"
                    onClose={() => setShowModal(false)}
                >
                    <form onSubmit={save}>
                        <Alert>{formError}</Alert>
                        <TemplateEditor
                            form={form}
                            onChange={setForm}
                            accounts={accounts}
                            variables={variables}
                        />
                        <div className="flex gap-2 justify-end border-t border-ink-100 pt-4">
                            <Button type="button" variant="secondary" onClick={() => setShowModal(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" loading={saving}>
                                {editing ? "Save changes" : "Create template"}
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}

            {preview && (
                <Modal title={`Preview: ${preview.name}`} onClose={() => setPreview(null)}>
                    <Alert>{previewError}</Alert>
                    <Field label="Sample data (JSON)" hint={'e.g. {"Name": "Priya", "OrderID": "A100"}'}>
                        <Textarea
                            value={previewSample}
                            onChange={(e) => setPreviewSample(e.target.value)}
                            className="font-mono text-xs"
                            rows={3}
                        />
                    </Field>
                    <Button variant="secondary" onClick={runPreview} loading={previewing} className="mb-5">
                        Render Preview
                    </Button>
                    {previewResult && (
                        // The server rendered the variables against the sample
                        // data; the same bubble the composer draws renders the
                        // result, so a preview here and a preview while editing
                        // cannot look like two different products.
                        <WhatsAppPreview
                            form={{
                                body: previewResult.body,
                                media_items: previewResult.media_items || [],
                                buttons: previewResult.buttons || [],
                                list_content: previewResult.list_content || {},
                                poll_content: previewResult.poll_content || {},
                                variables: {},
                            }}
                        />
                    )}
                </Modal>
            )}

            {apiFor && (
                <Modal
                    title={`API JSON: ${apiFor.name}`}
                    description="Send this template from your own software. Everything below is generated from the template itself, so it matches what the API accepts."
                    width="max-w-3xl"
                    onClose={() => setApiFor(null)}
                >
                    <TemplateApiPanel templateId={apiFor.id} />
                </Modal>
            )}

            {pendingBulk === "delete" && (
                <ConfirmDialog
                    title={`Delete ${selection.selectedCount} template${selection.selectedCount === 1 ? "" : "s"}`}
                    message={`Delete the ${selection.selectedCount} selected template${
                        selection.selectedCount === 1 ? "" : "s"
                    }? Campaigns already sent are unaffected. This cannot be undone.`}
                    confirmLabel={`Delete ${selection.selectedCount}`}
                    onConfirm={() => runBulkAction("delete")}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete template"
                    message={`Delete “${pendingDelete.name}”? Campaigns already sent are unaffected.`}
                    confirmLabel="Delete template"
                    onConfirm={() => remove(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}
