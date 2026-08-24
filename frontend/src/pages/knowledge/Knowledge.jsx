import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
    BookOpen,
    CheckCircle2,
    FileText,
    Lightbulb,
    Loader2,
    MessageSquare,
    Plus,
    Search,
    Sparkles,
    Trash2,
    TriangleAlert,
    Upload,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { timeAgo } from "../../components/crm/CrmBits";

/**
 * Upload your business information once, test it, turn on auto-reply.
 *
 * The page is built around that sentence and shows nothing else. There is no
 * mention of embeddings, chunk size, similarity thresholds or retrieval
 * anywhere a normal user can see — "1,248 passages" is as technical as it
 * gets, and even that is only there because "12 documents" alone does not
 * tell you whether a 300-page PDF actually came out readable.
 */

const STATUS_TONE = { READY: "green", PROCESSING: "amber", PENDING: "amber", FAILED: "red" };
const CONFIDENCE = {
    GOOD: { tone: "green", label: "Good match", icon: CheckCircle2 },
    LIMITED: { tone: "amber", label: "Limited information", icon: TriangleAlert },
    NONE: { tone: "gray", label: "No relevant information", icon: TriangleAlert },
};

const RECOMMENDATION_TONE = {
    green: "border-l-brand-500",
    amber: "border-l-amber-400",
    blue: "border-l-blue-400",
    red: "border-l-red-400",
};

function formatBytes(bytes) {
    if (!bytes) return "—";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// Test panel — the step between uploading and switching anything on
// ---------------------------------------------------------------------------

function TestPanel({ baseId, baseName }) {
    const [question, setQuestion] = useState("");
    const [result, setResult] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    const ask = async (event) => {
        event.preventDefault();
        if (!question.trim()) return;
        setBusy(true);
        setError("");
        try {
            const response = await api.post("/ai/ask/", { question: question.trim(), knowledge_base: baseId });
            setResult(response.data);
        } catch (err) {
            setError(apiErrorMessage(err, "Could not test that question."));
        } finally {
            setBusy(false);
        }
    };

    const confidence = result ? CONFIDENCE[result.confidence] || CONFIDENCE.NONE : null;
    const ConfidenceIcon = confidence?.icon;

    return (
        <Card>
            <CardHeader
                title="Ask your knowledge base"
                description={`Try a question a customer would ask. Nothing is sent to anybody — this is exactly what ${baseName} would answer.`}
                className="mb-4"
            />
            <Alert>{error}</Alert>

            <form onSubmit={ask} className="flex flex-col sm:flex-row gap-2">
                <Input
                    value={question}
                    onChange={(event) => setQuestion(event.target.value)}
                    placeholder="What is your refund policy?"
                    className="flex-1"
                />
                <Button type="submit" icon={Search} loading={busy} disabled={!question.trim()}>
                    Ask
                </Button>
            </form>

            {result && (
                <div className="mt-5 space-y-4">
                    <div className="flex items-center gap-2">
                        {ConfidenceIcon && <ConfidenceIcon size={16} className="text-ink-400" aria-hidden="true" />}
                        <Badge tone={confidence.tone}>{confidence.label}</Badge>
                    </div>

                    {result.answered ? (
                        <div className="rounded-card border border-ink-200 bg-ink-50/60 p-4">
                            <p className="text-[11px] font-bold uppercase tracking-wider text-ink-400 m-0 mb-2">
                                Answer
                            </p>
                            <p className="text-sm text-ink-800 m-0 whitespace-pre-wrap">{result.answer}</p>
                        </div>
                    ) : (
                        <Alert tone="warning" title="This would not be answered">
                            {result.detail ||
                                "Nothing in this knowledge base is close enough. The customer would get your fallback instead."}
                        </Alert>
                    )}

                    {result.sources?.length > 0 && (
                        <div>
                            <p className="text-[11px] font-bold uppercase tracking-wider text-ink-400 m-0 mb-2">
                                Where it came from
                            </p>
                            <div className="flex flex-wrap gap-1.5">
                                {result.sources.map((source) => (
                                    <Badge key={`${source.document_id}-${source.citation}`} tone="blue">
                                        {source.citation}
                                    </Badge>
                                ))}
                            </div>
                        </div>
                    )}

                    {result.excerpts?.length > 0 && (
                        <details className="text-sm">
                            <summary className="cursor-pointer text-ink-500 hover:text-ink-800 font-medium">
                                Show the exact text it read
                            </summary>
                            <ul className="list-none p-0 mt-3 mb-0 space-y-2">
                                {result.excerpts.map((excerpt, index) => (
                                    <li key={index} className="rounded-card border border-ink-200 p-3">
                                        <p className="text-xs font-semibold text-ink-500 m-0 mb-1">
                                            {excerpt.citation}
                                        </p>
                                        <p className="text-sm text-ink-700 m-0 whitespace-pre-wrap">
                                            {excerpt.content}
                                        </p>
                                    </li>
                                ))}
                            </ul>
                        </details>
                    )}
                </div>
            )}
        </Card>
    );
}

// ---------------------------------------------------------------------------
// One knowledge base
// ---------------------------------------------------------------------------

function BaseDetail({ base, onChanged }) {
    const toast = useToast();
    const [documents, setDocuments] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [uploading, setUploading] = useState(false);
    const [showText, setShowText] = useState(false);
    const [textForm, setTextForm] = useState({ title: "", content: "" });
    const [saving, setSaving] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(null);
    const fileInput = useRef(null);
    const pollRef = useRef(null);

    const load = useCallback(async () => {
        try {
            const response = await api.get("/ai/knowledge/", {
                params: { knowledge_base: base.id, page_size: 100 },
            });
            setDocuments(response.data.results || []);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load these documents."));
        } finally {
            setLoading(false);
        }
    }, [base.id]);

    useEffect(() => {
        load();
    }, [load]);

    // A document being read is the one thing on this page that changes on its
    // own, so the list refreshes while anything is still processing — and
    // stops the moment nothing is, rather than polling forever.
    useEffect(() => {
        const working = documents.some((document) => ["PENDING", "PROCESSING"].includes(document.status));
        if (!working) {
            clearInterval(pollRef.current);
            return undefined;
        }
        pollRef.current = setInterval(load, 3000);
        return () => clearInterval(pollRef.current);
    }, [documents, load]);

    const upload = async (files) => {
        if (!files?.length) return;
        setUploading(true);
        setError("");
        let failures = 0;
        for (const file of Array.from(files)) {
            const data = new FormData();
            data.append("file", file);
            data.append("knowledge_base", base.id);
            try {
                await api.post("/ai/knowledge/upload/", data, {
                    headers: { "Content-Type": "multipart/form-data" },
                });
            } catch (err) {
                failures += 1;
                setError(apiErrorMessage(err, `${file.name} could not be uploaded.`));
            }
        }
        if (failures < files.length) {
            toast.success(
                files.length - failures === 1
                    ? "Uploaded. We're reading it now."
                    : `${files.length - failures} files uploaded. We're reading them now.`,
            );
        }
        setUploading(false);
        if (fileInput.current) fileInput.current.value = "";
        await load();
        onChanged?.();
    };

    const saveText = async (event) => {
        event.preventDefault();
        setSaving(true);
        try {
            if (textForm.id) {
                await api.patch(`/ai/knowledge/${textForm.id}/`, {
                    title: textForm.title,
                    content: textForm.content,
                });
            } else {
                await api.post("/ai/knowledge/", {
                    knowledge_base: base.id,
                    title: textForm.title,
                    content: textForm.content,
                });
            }
            setShowText(false);
            setTextForm({ title: "", content: "" });
            toast.success("Saved.");
            await load();
            onChanged?.();
        } catch (err) {
            setError(apiErrorMessage(err, "Could not save that."));
        } finally {
            setSaving(false);
        }
    };

    const reindex = async (document) => {
        try {
            await api.post(`/ai/knowledge/${document.id}/reindex/`);
            toast.success("Reading it again.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not re-read that document."));
        }
    };

    const remove = async () => {
        try {
            await api.delete(`/ai/knowledge/${pendingDelete.id}/`);
            toast.success("Deleted. It will not be used to answer anybody again.");
            setPendingDelete(null);
            await load();
            onChanged?.();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not delete that document."));
            setPendingDelete(null);
        }
    };

    const toggleActive = async (document) => {
        try {
            await api.patch(`/ai/knowledge/${document.id}/`, { is_active: !document.is_active });
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not change that."));
        }
    };

    const columns = [
        {
            key: "title",
            header: "Document",
            render: (document) => (
                <div className="min-w-0">
                    <p className="font-semibold text-ink-900 truncate m-0">{document.title}</p>
                    <p className="text-xs text-ink-500 truncate m-0">
                        {document.original_filename || "Typed in"}
                        {document.size_bytes ? ` · ${formatBytes(document.size_bytes)}` : ""}
                        {document.page_count ? ` · ${document.page_count} pages` : ""}
                    </p>
                </div>
            ),
        },
        {
            key: "status",
            header: "Status",
            render: (document) => (
                <div className="min-w-0">
                    <Badge tone={STATUS_TONE[document.status] || "gray"}>
                        {document.status === "READY" && !document.is_active ? "Turned off" : document.status_display}
                    </Badge>
                    {document.error && (
                        <p className="text-xs text-red-600 mt-1 mb-0 max-w-xs">{document.error}</p>
                    )}
                </div>
            ),
        },
        {
            key: "chunk_count",
            header: "Passages",
            align: "right",
            hideBelow: "sm",
            render: (document) => <span className="tabular-nums text-ink-600">{document.chunk_count}</span>,
        },
        {
            key: "indexed_at",
            header: "Updated",
            hideBelow: "md",
            render: (document) => (
                <span className="text-ink-500">{timeAgo(document.indexed_at || document.updated_at)}</span>
            ),
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (document) => (
                <div className="flex justify-end gap-3 whitespace-nowrap">
                    {document.source_type === "TEXT" && (
                        <RowAction
                            tone="brand"
                            onClick={() =>
                                setTextForm({ id: document.id, title: document.title, content: document.content }) ||
                                setShowText(true)
                            }
                        >
                            Edit
                        </RowAction>
                    )}
                    {document.source_type === "FILE" && (
                        <RowAction onClick={() => reindex(document)}>Re-read</RowAction>
                    )}
                    <RowAction onClick={() => toggleActive(document)}>
                        {document.is_active ? "Turn off" : "Turn on"}
                    </RowAction>
                    <RowAction tone="danger" onClick={() => setPendingDelete(document)}>
                        Delete
                    </RowAction>
                </div>
            ),
        },
    ];

    const ready = documents.filter((document) => document.status === "READY" && document.is_active);
    const passages = ready.reduce((sum, document) => sum + (document.chunk_count || 0), 0);
    const working = documents.filter((document) => ["PENDING", "PROCESSING"].includes(document.status)).length;

    return (
        <div>
            <PageHeader
                title={base.name}
                description={
                    loading
                        ? "Everything the assistant may answer from."
                        : `${ready.length} document${ready.length === 1 ? "" : "s"} ready · ${passages.toLocaleString()} passages searchable`
                }
                backTo="/knowledge"
                backLabel="All knowledge bases"
                actions={
                    <>
                        <Button variant="secondary" icon={Plus} onClick={() => { setTextForm({ title: "", content: "" }); setShowText(true); }}>
                            Type something in
                        </Button>
                        <Button icon={Upload} loading={uploading} onClick={() => fileInput.current?.click()}>
                            Upload documents
                        </Button>
                    </>
                }
            />

            {/* The picker itself stays hidden: the button above is the affordance. */}
            <input
                ref={fileInput}
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md,.csv,.xlsx"
                className="hidden"
                onChange={(event) => upload(event.target.files)}
            />

            <Alert>{error}</Alert>

            {working > 0 && (
                <Alert tone="info" title={`Reading ${working} document${working === 1 ? "" : "s"}`}>
                    Large files take a moment. This list updates itself — you can carry on.
                </Alert>
            )}

            <div className="grid grid-cols-1 xl:grid-cols-[1fr_400px] gap-6 items-start">
                <div>
                    {loading ? (
                        <div className="py-16 flex justify-center">
                            <Spinner />
                        </div>
                    ) : (
                        <DataTable
                            columns={columns}
                            rows={documents}
                            empty={
                                <EmptyState
                                    icon={FileText}
                                    title="Nothing in here yet"
                                    description="Upload your price list, FAQ, policies or product catalogue. PDF, Word, text, CSV and Excel all work — we read them and pull out the text."
                                    action={
                                        <Button icon={Upload} onClick={() => fileInput.current?.click()}>
                                            Upload your first document
                                        </Button>
                                    }
                                />
                            }
                        />
                    )}
                </div>

                <TestPanel baseId={base.id} baseName={base.name} />
            </div>

            {showText && (
                <Modal
                    title={textForm.id ? "Edit" : "Type something in"}
                    description="For a fact that isn't in any document — opening hours, a delivery charge, a policy."
                    onClose={() => setShowText(false)}
                    width="lg"
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setShowText(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="knowledge-text-form" loading={saving}>
                                Save
                            </Button>
                        </>
                    }
                >
                    <form id="knowledge-text-form" onSubmit={saveText} className="space-y-4">
                        <Field label="What is this about?" required>
                            <Input
                                autoFocus
                                required
                                value={textForm.title}
                                onChange={(event) => setTextForm({ ...textForm, title: event.target.value })}
                                placeholder="Delivery charges"
                            />
                        </Field>
                        <Field
                            label="What should the assistant know?"
                            required
                            hint="Write it in paragraphs. One idea per paragraph works best."
                        >
                            <Textarea
                                rows={10}
                                required
                                value={textForm.content}
                                onChange={(event) => setTextForm({ ...textForm, content: event.target.value })}
                                placeholder={"Delivery inside the city takes two working days and costs 60 rupees.\n\nDelivery outside the city takes five days and costs 150 rupees."}
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete this document?"
                    message={`"${pendingDelete.title}" will be removed and nothing in it will ever be used to answer a customer again. This cannot be undone.`}
                    confirmLabel="Delete"
                    onConfirm={remove}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// The list of bases
// ---------------------------------------------------------------------------

export default function Knowledge() {
    const toast = useToast();
    const [bases, setBases] = useState([]);
    const [recommendations, setRecommendations] = useState([]);
    const [agent, setAgent] = useState(null);
    const [open, setOpen] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [showCreate, setShowCreate] = useState(false);
    const [form, setForm] = useState({ name: "", description: "" });
    const [saving, setSaving] = useState(false);
    const [pendingDelete, setPendingDelete] = useState(null);

    const load = useCallback(async () => {
        try {
            const [basesRes, recommendationsRes, agentRes] = await Promise.all([
                api.get("/ai/knowledge-bases/"),
                api.get("/ai/knowledge-recommendations/").catch(() => ({ data: { results: [] } })),
                api.get("/ai/agent/").catch(() => ({ data: null })),
            ]);
            setBases(basesRes.data.results || []);
            setRecommendations(recommendationsRes.data.results || []);
            setAgent(agentRes.data);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load your knowledge bases."));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const create = async (event) => {
        event.preventDefault();
        setSaving(true);
        try {
            const response = await api.post("/ai/knowledge-bases/", form);
            setShowCreate(false);
            setForm({ name: "", description: "" });
            await load();
            setOpen(response.data);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not create that knowledge base."));
        } finally {
            setSaving(false);
        }
    };

    const remove = async () => {
        try {
            await api.delete(`/ai/knowledge-bases/${pendingDelete.id}/`);
            toast.success("Deleted.");
            setPendingDelete(null);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not delete that."));
            setPendingDelete(null);
        }
    };

    if (open) {
        const current = bases.find((base) => base.id === open.id) || open;
        return <BaseDetail base={current} onChanged={load} />;
    }

    if (loading) {
        return (
            <div className="py-20 flex justify-center">
                <Spinner />
            </div>
        );
    }

    const totalReady = bases.reduce((sum, base) => sum + (base.document_count || 0), 0);
    const autoReplyOn = agent?.is_enabled;

    return (
        <div>
            <PageHeader
                title="Knowledge base"
                description="Upload your business information once, and let the assistant answer customers from it."
                actions={
                    <>
                        <Link to="/ai" className="no-underline">
                            <Button variant="secondary" icon={Sparkles}>
                                Auto-reply settings
                            </Button>
                        </Link>
                        <Button icon={Plus} onClick={() => setShowCreate(true)}>
                            New knowledge base
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            {totalReady > 0 && !autoReplyOn && (
                <Alert tone="warning" title="Auto-reply is off">
                    Your documents are indexed, but nothing is answering customers from them yet.{" "}
                    <Link to="/automation">Turn on knowledge auto-reply</Link> when you are happy with the answers
                    below.
                </Alert>
            )}

            {recommendations.length > 0 && (
                <Card className="mb-6">
                    <CardHeader
                        title="What we noticed"
                        description="Read from your own documents. Nothing here has been switched on."
                        className="mb-4"
                    />
                    <ul className="list-none p-0 m-0 space-y-3">
                        {recommendations.map((item) => (
                            <li
                                key={item.key}
                                className={`border border-ink-200 border-l-4 rounded-card p-4 flex flex-col sm:flex-row sm:items-center gap-3 ${
                                    RECOMMENDATION_TONE[item.tone] || "border-l-ink-300"
                                }`}
                            >
                                <span className="w-8 h-8 rounded-lg bg-amber-50 text-amber-600 flex items-center justify-center shrink-0">
                                    <Lightbulb size={16} aria-hidden="true" />
                                </span>
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm font-bold text-ink-900 m-0">{item.title}</p>
                                    <p className="text-sm text-ink-600 mt-0.5 mb-0">{item.detail}</p>
                                </div>
                                {item.action === "enable" && (
                                    <Link to="/automation" className="no-underline shrink-0">
                                        <Button size="sm" icon={Sparkles}>
                                            {item.action_label}
                                        </Button>
                                    </Link>
                                )}
                            </li>
                        ))}
                    </ul>
                </Card>
            )}

            {bases.length === 0 ? (
                <EmptyState
                    icon={BookOpen}
                    title="No knowledge bases yet"
                    description="A knowledge base is a folder of your business information — price list, FAQ, policies. Customers can then ask about it on WhatsApp and get an answer straight away."
                    action={
                        <Button icon={Plus} onClick={() => setShowCreate(true)}>
                            Create one
                        </Button>
                    }
                />
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {bases.map((base) => (
                        <Card key={base.id} className="flex flex-col">
                            <div className="flex items-start gap-3">
                                <span className="w-10 h-10 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center shrink-0">
                                    <BookOpen size={18} aria-hidden="true" />
                                </span>
                                <div className="min-w-0 flex-1">
                                    <p className="text-base font-bold text-ink-900 m-0 truncate">{base.name}</p>
                                    {base.description && (
                                        <p className="text-sm text-ink-500 mt-0.5 mb-0 line-clamp-2">
                                            {base.description}
                                        </p>
                                    )}
                                </div>
                                {agent?.knowledge_base === base.id && autoReplyOn && (
                                    <Badge tone="green">Live</Badge>
                                )}
                            </div>

                            <div className="flex flex-wrap items-center gap-1.5 mt-4">
                                <Badge tone="gray">
                                    {base.document_count} document{base.document_count === 1 ? "" : "s"}
                                </Badge>
                                <Badge tone="gray">{(base.chunk_total || 0).toLocaleString()} passages</Badge>
                                {base.processing_count > 0 && (
                                    <Badge tone="amber">
                                        <Loader2 size={11} className="inline animate-spin mr-1" aria-hidden="true" />
                                        Reading {base.processing_count}
                                    </Badge>
                                )}
                                {base.failed_count > 0 && <Badge tone="red">{base.failed_count} failed</Badge>}
                            </div>

                            <div className="flex gap-2 mt-4 pt-4 border-t border-ink-100">
                                <Button size="sm" onClick={() => setOpen(base)} className="flex-1">
                                    Open
                                </Button>
                                <Link to="/automation" className="no-underline">
                                    <Button size="sm" variant="secondary" icon={MessageSquare}>
                                        Auto-reply
                                    </Button>
                                </Link>
                                <Button size="sm" variant="ghost" onClick={() => setPendingDelete(base)}>
                                    <Trash2 size={14} />
                                </Button>
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            {showCreate && (
                <Modal
                    title="New knowledge base"
                    description="A separate folder of information — useful when a promotion's prices must not be quoted after it ends."
                    onClose={() => setShowCreate(false)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setShowCreate(false)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="kb-form" loading={saving}>
                                Create
                            </Button>
                        </>
                    }
                >
                    <form id="kb-form" onSubmit={create} className="space-y-4">
                        <Field label="Name" required>
                            <Input
                                autoFocus
                                required
                                maxLength={120}
                                value={form.name}
                                onChange={(event) => setForm({ ...form, name: event.target.value })}
                                placeholder="ABC Electronics"
                            />
                        </Field>
                        <Field label="What is it for?" hint="Optional — a note for your team.">
                            <Textarea
                                rows={3}
                                value={form.description}
                                onChange={(event) => setForm({ ...form, description: event.target.value })}
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete this knowledge base?"
                    message={`"${pendingDelete.name}" and all ${pendingDelete.document_count} of its documents will be removed. Nothing in them will ever be used to answer a customer again. This cannot be undone.`}
                    confirmLabel="Delete"
                    onConfirm={remove}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}
