import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Handshake, Plus, Settings2, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { formatDate, formatMoney, timeAgo } from "../../components/crm/CrmBits";

/**
 * The sales board: one column per stage, one card per deal.
 *
 * Deals move with a dropdown on the card rather than by dragging. Dragging
 * looks better in a screenshot, but this board is used on a phone as often as
 * a laptop, drag targets are the first thing to break on touch, and a select
 * is reachable by keyboard and screen reader for free. It also costs no
 * dependency, which matters more here than the animation does.
 */

const KIND_ACCENT = {
    WON: "border-t-brand-500",
    LOST: "border-t-red-400",
    OPEN: "border-t-ink-300",
};

function DealCard({ deal, stages, onMove, onEdit }) {
    return (
        <div className="bg-white border border-ink-200 rounded-card p-3 shadow-card">
            <div className="flex items-start justify-between gap-2">
                <button
                    type="button"
                    onClick={() => onEdit(deal)}
                    className="text-sm font-bold text-ink-900 bg-transparent border-0 p-0 text-left cursor-pointer hover:text-brand-700"
                >
                    {deal.name}
                </button>
                <span className="text-sm font-bold text-ink-900 tabular-nums shrink-0">
                    {formatMoney(deal.value, deal.currency)}
                </span>
            </div>

            <Link
                to={`/contacts/${deal.contact}`}
                className="block text-xs text-brand-700 no-underline hover:underline mt-1 truncate"
            >
                {deal.contact_name}
            </Link>

            <div className="flex items-center gap-1.5 flex-wrap mt-2">
                {deal.owner_username && <Badge tone="gray">{deal.owner_username}</Badge>}
                {deal.expected_close_date && <Badge tone="blue">{formatDate(deal.expected_close_date)}</Badge>}
            </div>

            <Select
                value={deal.stage}
                className="mt-3 text-xs"
                aria-label={`Move ${deal.name} to another stage`}
                onChange={(event) => onMove(deal, event.target.value)}
            >
                {stages.map((stage) => (
                    <option key={stage.id} value={stage.id}>
                        {stage.name}
                    </option>
                ))}
            </Select>

            <p className="text-[11px] text-ink-400 mt-2 mb-0">Updated {timeAgo(deal.updated_at)}</p>
        </div>
    );
}

const EMPTY_DEAL = { name: "", contact: "", value: "", currency: "INR", expected_close_date: "", notes: "" };

export default function Pipeline() {
    const toast = useToast();
    const [pipelines, setPipelines] = useState([]);
    const [pipelineId, setPipelineId] = useState(null);
    const [board, setBoard] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const [dealForm, setDealForm] = useState(null);
    const [contactQuery, setContactQuery] = useState("");
    const [contactResults, setContactResults] = useState([]);
    const [stagesOpen, setStagesOpen] = useState(false);
    const [stageForm, setStageForm] = useState(null);
    const [pendingStageDelete, setPendingStageDelete] = useState(null);
    const [pendingDealDelete, setPendingDealDelete] = useState(null);

    useEffect(() => {
        api.get("/crm/pipelines/")
            .then((response) => {
                const results = response.data.results || [];
                setPipelines(results);
                setPipelineId(results[0]?.id || null);
                if (results.length === 0) setLoading(false);
            })
            .catch((err) => {
                setError(apiErrorMessage(err, "Unable to load your pipeline."));
                setLoading(false);
            });
    }, []);

    const loadBoard = useCallback(async () => {
        if (!pipelineId) return;
        setLoading(true);
        try {
            const response = await api.get(`/crm/pipelines/${pipelineId}/board/`);
            setBoard(response.data);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load your pipeline."));
        } finally {
            setLoading(false);
        }
    }, [pipelineId]);

    useEffect(() => {
        loadBoard();
    }, [loadBoard]);

    // The contact picker on the new-deal form. Searched server-side rather
    // than loading every contact into a dropdown, which stops working at
    // exactly the point a customer's list gets interesting.
    useEffect(() => {
        if (!dealForm || dealForm.id) return undefined;
        const handle = setTimeout(() => {
            api.get("/contacts/contacts/", { params: { search: contactQuery, page_size: 10 } })
                .then((response) => setContactResults(response.data.results || []))
                .catch(() => setContactResults([]));
        }, 250);
        return () => clearTimeout(handle);
    }, [contactQuery, dealForm]);

    const stages = board?.pipeline?.stages || [];

    const moveDeal = async (deal, stageId) => {
        if (String(deal.stage) === String(stageId)) return;
        try {
            await api.post(`/crm/deals/${deal.id}/move/`, { stage: Number(stageId) });
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not move that deal."));
        }
    };

    const saveDeal = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            const payload = {
                name: dealForm.name,
                value: dealForm.value || 0,
                currency: dealForm.currency || "INR",
                expected_close_date: dealForm.expected_close_date || null,
                notes: dealForm.notes || "",
            };
            if (dealForm.id) {
                await api.patch(`/crm/deals/${dealForm.id}/`, payload);
            } else {
                await api.post("/crm/deals/", { ...payload, contact: Number(dealForm.contact), pipeline: pipelineId });
            }
            setDealForm(null);
            toast.success(dealForm.id ? "Deal updated." : "Deal created.");
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that deal."));
        } finally {
            setBusy(false);
        }
    };

    const deleteDeal = async () => {
        try {
            await api.delete(`/crm/deals/${pendingDealDelete.id}/`);
            setPendingDealDelete(null);
            setDealForm(null);
            toast.success("Deal deleted.");
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not delete that deal."));
        }
    };

    const saveStage = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            const payload = {
                pipeline: pipelineId,
                name: stageForm.name,
                kind: stageForm.kind,
                order: Number(stageForm.order) || 0,
                probability: Number(stageForm.probability) || 0,
            };
            if (stageForm.id) {
                await api.patch(`/crm/stages/${stageForm.id}/`, payload);
            } else {
                await api.post("/crm/stages/", payload);
            }
            setStageForm(null);
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that stage."));
        } finally {
            setBusy(false);
        }
    };

    const deleteStage = async () => {
        try {
            await api.delete(`/crm/stages/${pendingStageDelete.id}/`);
            setPendingStageDelete(null);
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not delete that stage."));
            setPendingStageDelete(null);
        }
    };

    const columns = board?.columns || [];
    const openTotal = columns
        .filter((column) => column.stage.kind === "OPEN")
        .reduce((sum, column) => sum + Number(column.value || 0), 0);
    const openCount = columns
        .filter((column) => column.stage.kind === "OPEN")
        .reduce((sum, column) => sum + column.count, 0);
    const currency = columns.flatMap((column) => column.deals)[0]?.currency || "INR";
    const anyDeals = columns.some((column) => column.count > 0);

    return (
        <div>
            <PageHeader
                title="Pipeline"
                description={
                    loading
                        ? "Every sale you are working on."
                        : `${openCount} open deal${openCount === 1 ? "" : "s"} worth ${formatMoney(openTotal, currency)}`
                }
                actions={
                    <>
                        {pipelines.length > 1 && (
                            <Select
                                value={pipelineId || ""}
                                className="w-44"
                                aria-label="Choose a pipeline"
                                onChange={(event) => setPipelineId(Number(event.target.value))}
                            >
                                {pipelines.map((pipeline) => (
                                    <option key={pipeline.id} value={pipeline.id}>
                                        {pipeline.name}
                                    </option>
                                ))}
                            </Select>
                        )}
                        <Button variant="secondary" icon={Settings2} onClick={() => setStagesOpen(true)}>
                            Stages
                        </Button>
                        <Button
                            icon={Plus}
                            onClick={() => {
                                setContactQuery("");
                                setDealForm({ ...EMPTY_DEAL });
                            }}
                        >
                            New deal
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            {loading ? (
                <div className="py-20 flex justify-center">
                    <Spinner />
                </div>
            ) : !anyDeals ? (
                <EmptyState
                    icon={Handshake}
                    title="No deals yet"
                    description="A deal is one sale you are working on. Open one from a conversation the moment it turns into a real opportunity, and it stops being something you have to remember."
                    action={
                        <Button
                            icon={Plus}
                            onClick={() => {
                                setContactQuery("");
                                setDealForm({ ...EMPTY_DEAL });
                            }}
                        >
                            Create the first deal
                        </Button>
                    }
                />
            ) : (
                <div className="scroll-x flex gap-4 pb-4 items-start">
                    {columns.map((column) => (
                        <div
                            key={column.stage.id}
                            className={`w-72 shrink-0 rounded-card bg-ink-50 border border-ink-200 border-t-4 ${
                                KIND_ACCENT[column.stage.kind] || KIND_ACCENT.OPEN
                            }`}
                        >
                            <div className="px-3 py-3 border-b border-ink-200">
                                <div className="flex items-center justify-between gap-2">
                                    <p className="text-sm font-bold text-ink-900 m-0 truncate">{column.stage.name}</p>
                                    <span className="text-xs font-semibold text-ink-500 tabular-nums shrink-0">
                                        {column.count}
                                    </span>
                                </div>
                                <p className="text-xs text-ink-500 mt-0.5 mb-0 tabular-nums">
                                    {formatMoney(column.value, currency)}
                                </p>
                            </div>
                            <div className="p-2.5 space-y-2.5 max-h-[calc(100vh-20rem)] overflow-y-auto">
                                {column.deals.length === 0 ? (
                                    <p className="text-xs text-ink-400 text-center py-6 m-0">Nothing here</p>
                                ) : (
                                    column.deals.map((deal) => (
                                        <DealCard
                                            key={deal.id}
                                            deal={deal}
                                            stages={stages}
                                            onMove={moveDeal}
                                            onEdit={(item) =>
                                                setDealForm({
                                                    id: item.id,
                                                    name: item.name,
                                                    contact: item.contact,
                                                    contact_name: item.contact_name,
                                                    value: item.value,
                                                    currency: item.currency,
                                                    expected_close_date: item.expected_close_date || "",
                                                    notes: item.notes || "",
                                                })
                                            }
                                        />
                                    ))
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {dealForm && (
                <Modal
                    title={dealForm.id ? "Edit deal" : "New deal"}
                    onClose={() => setDealForm(null)}
                    footer={
                        <>
                            {dealForm.id && (
                                <Button
                                    variant="danger"
                                    icon={Trash2}
                                    className="mr-auto"
                                    onClick={() => setPendingDealDelete(dealForm)}
                                >
                                    Delete
                                </Button>
                            )}
                            <Button variant="secondary" onClick={() => setDealForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="pipeline-deal-form" loading={busy}>
                                {dealForm.id ? "Save deal" : "Create deal"}
                            </Button>
                        </>
                    }
                >
                    <form id="pipeline-deal-form" onSubmit={saveDeal} className="space-y-4">
                        {dealForm.id ? (
                            <Field label="Customer">
                                <p className="text-sm text-ink-700 m-0">{dealForm.contact_name}</p>
                            </Field>
                        ) : (
                            <Field label="Which customer?" required hint="Search by name, phone or email.">
                                <Input
                                    autoFocus
                                    value={contactQuery}
                                    onChange={(event) => setContactQuery(event.target.value)}
                                    placeholder="Start typing a name or number..."
                                />
                                <Select
                                    value={dealForm.contact}
                                    required
                                    className="mt-2"
                                    aria-label="Choose a contact"
                                    onChange={(event) => setDealForm({ ...dealForm, contact: event.target.value })}
                                >
                                    <option value="">Choose a contact...</option>
                                    {contactResults.map((item) => (
                                        <option key={item.id} value={item.id}>
                                            {item.name || "No name"} — {item.phone}
                                        </option>
                                    ))}
                                </Select>
                            </Field>
                        )}
                        <Field label="What is the sale?" required>
                            <Input
                                required
                                value={dealForm.name}
                                onChange={(event) => setDealForm({ ...dealForm, name: event.target.value })}
                                placeholder="500 units, monthly"
                            />
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="Value">
                                <Input
                                    type="number"
                                    min="0"
                                    step="0.01"
                                    value={dealForm.value}
                                    onChange={(event) => setDealForm({ ...dealForm, value: event.target.value })}
                                />
                            </Field>
                            <Field label="Currency">
                                <Input
                                    maxLength={3}
                                    value={dealForm.currency}
                                    onChange={(event) =>
                                        setDealForm({ ...dealForm, currency: event.target.value.toUpperCase() })
                                    }
                                />
                            </Field>
                        </div>
                        <Field label="Expected to close">
                            <Input
                                type="date"
                                value={dealForm.expected_close_date || ""}
                                onChange={(event) =>
                                    setDealForm({ ...dealForm, expected_close_date: event.target.value })
                                }
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {stagesOpen && (
                <Modal
                    title="Pipeline stages"
                    description="Rename, reorder or add stages so the board matches how you actually sell."
                    onClose={() => setStagesOpen(false)}
                    width="lg"
                    footer={
                        <>
                            <Button
                                variant="secondary"
                                icon={Plus}
                                className="mr-auto"
                                onClick={() =>
                                    setStageForm({
                                        name: "", kind: "OPEN", order: stages.length, probability: 50,
                                    })
                                }
                            >
                                Add stage
                            </Button>
                            <Button variant="secondary" onClick={() => setStagesOpen(false)}>
                                Done
                            </Button>
                        </>
                    }
                >
                    <ul className="list-none p-0 m-0 space-y-2">
                        {stages.map((stage) => (
                            <li
                                key={stage.id}
                                className="border border-ink-200 rounded-card p-3 flex items-center gap-3"
                            >
                                <span
                                    className="w-2.5 h-2.5 rounded-full shrink-0"
                                    style={{ backgroundColor: stage.color }}
                                    aria-hidden="true"
                                />
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm font-semibold text-ink-900 m-0 truncate">{stage.name}</p>
                                    <p className="text-xs text-ink-500 mt-0.5 mb-0">
                                        {stage.kind === "OPEN" ? `${stage.probability}% chance` : stage.kind === "WON" ? "Counts as won" : "Counts as lost"}
                                    </p>
                                </div>
                                <Button
                                    size="xs"
                                    variant="ghost"
                                    onClick={() =>
                                        setStageForm({
                                            id: stage.id,
                                            name: stage.name,
                                            kind: stage.kind,
                                            order: stage.order,
                                            probability: stage.probability,
                                        })
                                    }
                                >
                                    Edit
                                </Button>
                                <Button size="xs" variant="ghost" onClick={() => setPendingStageDelete(stage)}>
                                    <Trash2 size={14} />
                                </Button>
                            </li>
                        ))}
                    </ul>
                </Modal>
            )}

            {stageForm && (
                <Modal
                    title={stageForm.id ? "Edit stage" : "Add stage"}
                    onClose={() => setStageForm(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setStageForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="stage-form" loading={busy}>
                                Save stage
                            </Button>
                        </>
                    }
                >
                    <form id="stage-form" onSubmit={saveStage} className="space-y-4">
                        <Field label="Name" required>
                            <Input
                                autoFocus
                                required
                                maxLength={60}
                                value={stageForm.name}
                                onChange={(event) => setStageForm({ ...stageForm, name: event.target.value })}
                            />
                        </Field>
                        <Field
                            label="What does reaching this stage mean?"
                            hint="Reports count won and lost by this, not by the stage's name — so you can call them whatever you like."
                        >
                            <Select
                                value={stageForm.kind}
                                onChange={(event) => setStageForm({ ...stageForm, kind: event.target.value })}
                            >
                                <option value="OPEN">Still in progress</option>
                                <option value="WON">The sale is won</option>
                                <option value="LOST">The sale is lost</option>
                            </Select>
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="Position">
                                <Input
                                    type="number"
                                    min="0"
                                    value={stageForm.order}
                                    onChange={(event) => setStageForm({ ...stageForm, order: event.target.value })}
                                />
                            </Field>
                            <Field label="Chance of winning (%)">
                                <Input
                                    type="number"
                                    min="0"
                                    max="100"
                                    value={stageForm.probability}
                                    onChange={(event) =>
                                        setStageForm({ ...stageForm, probability: event.target.value })
                                    }
                                />
                            </Field>
                        </div>
                    </form>
                </Modal>
            )}

            {pendingStageDelete && (
                <ConfirmDialog
                    title="Delete this stage?"
                    message={`"${pendingStageDelete.name}" will be removed from the board. Stages that still hold deals cannot be deleted — move those deals first.`}
                    confirmLabel="Delete stage"
                    onConfirm={deleteStage}
                    onClose={() => setPendingStageDelete(null)}
                />
            )}

            {pendingDealDelete && (
                <ConfirmDialog
                    title="Delete this deal?"
                    message={`"${pendingDealDelete.name}" will be removed. The contact, their messages and their history are not affected.`}
                    confirmLabel="Delete deal"
                    onConfirm={deleteDeal}
                    onClose={() => setPendingDealDelete(null)}
                />
            )}
        </div>
    );
}
