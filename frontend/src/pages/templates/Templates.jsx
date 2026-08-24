import { useEffect, useState } from "react";
import { Eye, FileText, Pencil, Plus, Power, PowerOff, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import { toMediaItems, primaryMessageType } from "../../utils/media";
import { toButtons } from "../../utils/buttons";
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
import MediaPicker from "../../components/ui/MediaPicker";
import ButtonBuilder, { ButtonPreview } from "../../components/ui/ButtonBuilder";
import { Checkbox, Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

const EMPTY_FORM = { name: "", message_type: "TEXT", body: "", media_items: [], buttons: [] };

const TYPE_TONE = { TEXT: "gray", IMAGE: "blue", VIDEO: "violet", DOCUMENT: "amber" };

export default function Templates() {
    const toast = useToast();
    const [templates, setTemplates] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [showModal, setShowModal] = useState(false);
    const [editing, setEditing] = useState(null);
    const [form, setForm] = useState(EMPTY_FORM);
    const [formError, setFormError] = useState("");
    const [saving, setSaving] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(null);

    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);
    const selection = useSelection(templates);

    const [preview, setPreview] = useState(null);
    const [previewSample, setPreviewSample] = useState("{}");
    const [previewResult, setPreviewResult] = useState(null);
    const [previewError, setPreviewError] = useState("");
    const [previewing, setPreviewing] = useState(false);

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
    }, []);

    const openCreate = () => {
        setEditing(null);
        setForm(EMPTY_FORM);
        setFormError("");
        setShowModal(true);
    };

    const openEdit = (template) => {
        setEditing(template);
        setForm({
            name: template.name,
            message_type: template.message_type,
            body: template.body,
            // Hydrated through toMediaItems so a template saved with the old
            // single media_url opens with that file already attached.
            media_items: toMediaItems(template),
            buttons: toButtons(template),
        });
        setFormError("");
        setShowModal(true);
    };

    const save = async (event) => {
        event.preventDefault();
        setFormError("");
        setSaving(true);
        try {
            if (editing) {
                await api.patch(`/templates/templates/${editing.id}/`, form);
            } else {
                await api.post("/templates/templates/", form);
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

    const setMedia = (media_items) => {
        // The API derives message_type from the first attachment, so the form
        // shows the same thing rather than a type the save would overwrite.
        setForm((previous) => ({
            ...previous,
            media_items,
            message_type: primaryMessageType(media_items, media_items.length ? previous.message_type : "TEXT"),
        }));
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
                            className={`bg-white border rounded-card shadow-card p-5 flex flex-col transition-colors ${
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
                                <Badge tone={TYPE_TONE[template.message_type] || "gray"}>{template.message_type}</Badge>
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
                                            [{variable}]
                                        </span>
                                    ))}
                                </div>
                            )}

                            <div className="flex gap-2 pt-3 border-t border-ink-100">
                                <Button variant="secondary" size="sm" icon={Eye} onClick={() => openPreview(template)}>
                                    Preview
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
                    title={editing ? "Edit Template" : "New Template"}
                    description="Use [Name], [OrderID] and similar placeholders for personalization."
                    onClose={() => setShowModal(false)}
                >
                    <form onSubmit={save}>
                        <Alert>{formError}</Alert>
                        <Field label="Name" required>
                            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus required />
                        </Field>
                        <Field
                            label="Message Type"
                            hint={
                                form.media_items.length > 0
                                    ? "Set by the first attachment below."
                                    : undefined
                            }
                        >
                            <Select
                                value={form.message_type}
                                disabled={form.media_items.length > 0}
                                onChange={(e) => setForm({ ...form, message_type: e.target.value })}
                            >
                                <option value="TEXT">Text</option>
                                <option value="IMAGE">Image</option>
                                <option value="VIDEO">Video</option>
                                <option value="DOCUMENT">Document</option>
                            </Select>
                        </Field>
                        <Field label="Message" hint="Use [Name], [OrderID], etc. for personalization variables." required>
                            <Textarea value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} required />
                        </Field>
                        <MediaPicker value={form.media_items} onChange={setMedia} />
                        <ButtonBuilder
                            value={form.buttons}
                            onChange={(buttons) => setForm({ ...form, buttons })}
                        />
                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={() => setShowModal(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" loading={saving}>
                                {editing ? "Save Changes" : "Create Template"}
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
                        <div className="rounded-xl bg-ink-100 p-4">
                            <p className="text-xs font-semibold uppercase tracking-wide text-ink-400 m-0 mb-2">
                                WhatsApp preview
                            </p>
                            <div className="max-w-[85%] ml-auto">
                                <div className="bg-brand-600 text-white rounded-2xl rounded-br-sm px-3.5 py-2.5 text-sm whitespace-pre-wrap">
                                    {previewResult.body}
                                </div>
                                <ButtonPreview buttons={preview.buttons} className="mt-1" />
                            </div>
                        </div>
                    )}
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
