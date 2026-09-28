import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, Handshake, Plus, Settings2, StickyNote, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { formatDate, formatMoney, timeAgo } from "../../components/crm/CrmBits";

/**
 * The sales board: one column per stage, one card per deal.
 *
 * Deals move two ways, and both matter. Dragging a card onto a column is what
 * anyone who has used a board expects, so it is the obvious path on a laptop.
 * The dropdown on every card stays, because HTML5 drag does not exist on touch
 * and is awkward with a keyboard or a screen reader -- this board is used on a
 * phone as often as a desktop. Both call the same move, so neither can drift.
 *
 * Native drag events, not a library: a board this size does not need the
 * animation a drag-and-drop dependency buys, and the dropdown already covers
 * the cases native drag cannot reach.
 *
 * A card at rest shows the deal and what it is worth, and nothing else. Owner,
 * dates, notes and the stage dropdown live behind "Details", because scanning
 * a column for the deal you want is the thing this screen is for -- five lines
 * per card turns fifteen deals into a scroll, and a board you have to scroll
 * stops being a board.
 */

function DealCard({ deal, stages, onMove, onEdit, onDelete, onNote, onDragStart, onDragEnd, isDragging }) {
    const [open, setOpen] = useState(false);
    // A native <select> inside a draggable element does not open reliably --
    // the browser starts dragging the card instead. Suspending draggable while
    // the pointer is inside the details panel keeps both paths working.
    const [draggable, setDraggable] = useState(true);

    return (
        <div
            draggable={draggable}
            onDragStart={(event) => {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", String(deal.id));
                onDragStart(deal);
            }}
            onDragEnd={onDragEnd}
            className={`bg-surface border rounded-lg cursor-grab active:cursor-grabbing transition-all ${
                isDragging ? "opacity-40 border-ink-200" : "border-ink-200 hover:border-ink-300 hover:shadow-card"
            }`}
        >
            <div className="px-3 py-2.5">
                <div className="flex items-baseline justify-between gap-2">
                    <p className="text-sm font-semibold text-ink-900 m-0 truncate">{deal.name}</p>
                    <span className="text-sm font-semibold text-ink-900 tabular-nums shrink-0">
                        {formatMoney(deal.value, deal.currency)}
                    </span>
                </div>

                <div className="flex items-center gap-2 mt-1.5">
                    <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => setOpen(!open)}
                        className="inline-flex items-center gap-1 text-[11px] font-medium text-ink-500 bg-transparent border-0 p-0 cursor-pointer hover:text-brand-700"
                    >
                        <ChevronDown
                            size={12}
                            className={`transition-transform ${open ? "rotate-180" : ""}`}
                            aria-hidden="true"
                        />
                        Details
                    </button>
                    {/* Collapsed, a note would be invisible; this is the one hint
                        that there is something written down worth opening. */}
                    {deal.notes && !open && (
                        <StickyNote size={12} className="text-brand-600" aria-label="Has a note" />
                    )}
                </div>
            </div>

            {open && (
                <div
                    onPointerEnter={() => setDraggable(false)}
                    onPointerLeave={() => setDraggable(true)}
                    className="border-t border-ink-100 px-3 py-2.5 space-y-3"
                >
                    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 m-0 text-xs">
                        <dt className="text-ink-500">Customer</dt>
                        <dd className="m-0 min-w-0">
                            <Link
                                to={`/contacts/${deal.contact}`}
                                className="block text-brand-700 no-underline hover:underline truncate"
                            >
                                {deal.contact_name}
                            </Link>
                        </dd>
                        {deal.owner_username && (
                            <>
                                <dt className="text-ink-500">Owner</dt>
                                <dd className="m-0 text-ink-700 truncate">{deal.owner_username}</dd>
                            </>
                        )}
                        {deal.expected_close_date && (
                            <>
                                <dt className="text-ink-500">Closes</dt>
                                <dd className="m-0 text-ink-700">{formatDate(deal.expected_close_date)}</dd>
                            </>
                        )}
                        <dt className="text-ink-500">Updated</dt>
                        <dd className="m-0 text-ink-700">{timeAgo(deal.updated_at)}</dd>
                    </dl>

                    {deal.notes && (
                        <button
                            type="button"
                            onClick={() => onNote(deal)}
                            className="block w-full text-left text-xs text-ink-700 bg-ink-50 border border-ink-200 rounded-md px-2 py-1.5 whitespace-pre-line line-clamp-4 cursor-pointer hover:border-ink-300"
                        >
                            {deal.notes}
                        </button>
                    )}

                    <div>
                        <label
                            htmlFor={`deal-${deal.id}-stage`}
                            className="block text-[11px] font-medium text-ink-500 mb-1"
                        >
                            Stage
                        </label>
                        <Select
                            id={`deal-${deal.id}-stage`}
                            value={deal.stage}
                            className="text-xs"
                            onChange={(event) => onMove(deal, event.target.value)}
                        >
                            {stages.map((stage) => (
                                <option key={stage.id} value={stage.id}>
                                    {stage.name}
                                </option>
                            ))}
                        </Select>
                    </div>

                    <div className="flex items-center gap-1.5">
                        <Button size="xs" variant="secondary" icon={StickyNote} onClick={() => onNote(deal)}>
                            {deal.notes ? "Edit note" : "Add note"}
                        </Button>
                        <Button size="xs" variant="secondary" onClick={() => onEdit(deal)}>
                            Edit
                        </Button>
                        <Button
                            size="xs"
                            variant="ghost"
                            className="ml-auto"
                            aria-label={`Delete ${deal.name}`}
                            onClick={() => onDelete(deal)}
                        >
                            <Trash2 size={13} />
                        </Button>
                    </div>
                </div>
            )}
        </div>
    );
}

/**
 * The board with one deal moved between columns, counts and totals included.
 *
 * Pure, and only ever a stand-in: the reload that follows the request is the
 * real answer. It exists so a dropped card lands where it was dropped instead
 * of snapping back for the length of a round trip.
 */
function boardWithMove(board, deal, stageId) {
    if (!board) return board;
    const moved = { ...deal, stage: stageId };
    return {
        ...board,
        columns: board.columns.map((column) => {
            const leaving = column.stage.id === deal.stage;
            const arriving = column.stage.id === stageId;
            if (!leaving && !arriving) return column;
            return {
                ...column,
                deals: leaving
                    ? column.deals.filter((item) => item.id !== deal.id)
                    : [moved, ...column.deals],
                count: column.count + (leaving ? -1 : 1),
                value: Number(column.value || 0) + Number(deal.value || 0) * (leaving ? -1 : 1),
            };
        }),
    };
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
    const [draggedDeal, setDraggedDeal] = useState(null);
    const [dropStageId, setDropStageId] = useState(null);
    const [noteForm, setNoteForm] = useState(null);

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
        const before = board;
        setBoard(boardWithMove(board, deal, Number(stageId)));
        try {
            await api.post(`/crm/deals/${deal.id}/move/`, { stage: Number(stageId) });
            await loadBoard();
        } catch (err) {
            setBoard(before);
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

    // A note of its own, without opening the whole deal form. Writing one down
    // is the thing most likely to happen mid-call, and a five-field modal is
    // enough friction that it gets skipped -- which is how a board ends up
    // holding numbers nobody can explain a week later.
    const saveNote = async (event) => {
        event.preventDefault();
        setBusy(true);
        try {
            await api.patch(`/crm/deals/${noteForm.id}/`, { notes: noteForm.notes });
            setNoteForm(null);
            toast.success("Note saved.");
            await loadBoard();
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not save that note."));
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
                // A grid rather than a row of fixed-width columns, so every stage
                // is on screen at once instead of behind a sideways scroll. The
                // 11rem floor is what keeps that honest: a pipeline with enough
                // stages to squeeze its columns unreadable scrolls again rather
                // than shrinking them to slivers.
                <div className="scroll-x pb-4">
                    <div
                        className="grid gap-3 items-start"
                        style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(11rem, 1fr))` }}
                    >
                        {columns.map((column) => {
                            const isDropTarget =
                                dropStageId === column.stage.id &&
                                draggedDeal &&
                                draggedDeal.stage !== column.stage.id;
                            return (
                                <div
                                    key={column.stage.id}
                                    onDragOver={(event) => {
                                        // Only preventDefault marks a valid drop target; without
                                        // it the browser refuses the drop and animates the card back.
                                        if (!draggedDeal) return;
                                        event.preventDefault();
                                        event.dataTransfer.dropEffect = "move";
                                        if (dropStageId !== column.stage.id) setDropStageId(column.stage.id);
                                    }}
                                    onDragLeave={(event) => {
                                        // Moving between the column's own children fires a leave
                                        // too; only a pointer that has left the column counts.
                                        if (!event.currentTarget.contains(event.relatedTarget)) {
                                            setDropStageId(null);
                                        }
                                    }}
                                    onDrop={(event) => {
                                        event.preventDefault();
                                        const deal = draggedDeal;
                                        setDropStageId(null);
                                        setDraggedDeal(null);
                                        if (deal) moveDeal(deal, column.stage.id);
                                    }}
                                    // The stage's own colour is already stored and already
                                    // editable, so using it here means the board reads the
                                    // same as the Stages dialog rather than a second map.
                                    style={{ borderTopColor: column.stage.color, borderTopWidth: "3px" }}
                                    className={`min-w-0 rounded-lg border transition-colors ${
                                        isDropTarget
                                            ? "bg-brand-50 border-brand-300 ring-2 ring-brand-500/30"
                                            : "bg-ink-50/60 border-ink-200"
                                    }`}
                                >
                                    <div className="px-3 py-2.5 border-b border-ink-200">
                                        <div className="flex items-center gap-2">
                                            <p className="flex-1 min-w-0 text-xs font-semibold uppercase tracking-wide text-ink-600 m-0 truncate">
                                                {column.stage.name}
                                            </p>
                                            <span className="text-[11px] font-semibold text-ink-600 tabular-nums bg-surface border border-ink-200 rounded-full px-1.5 shrink-0">
                                                {column.count}
                                            </span>
                                        </div>
                                        <p className="text-sm font-semibold text-ink-900 mt-1 mb-0 tabular-nums">
                                            {formatMoney(column.value, currency)}
                                        </p>
                                    </div>
                                    <div className="p-2 space-y-2 max-h-[calc(100vh-19rem)] overflow-y-auto">
                                        {column.deals.length === 0 ? (
                                            <p className="text-xs text-ink-400 text-center py-8 m-0">
                                                {draggedDeal ? "Drop here" : "No deals"}
                                            </p>
                                        ) : (
                                            column.deals.map((deal) => (
                                                <DealCard
                                                    key={deal.id}
                                                    deal={deal}
                                                    stages={stages}
                                                    onMove={moveDeal}
                                                onDelete={setPendingDealDelete}
                                                onNote={(item) =>
                                                    setNoteForm({
                                                        id: item.id,
                                                        name: item.name,
                                                        notes: item.notes || "",
                                                    })
                                                }
                                                    isDragging={draggedDeal?.id === deal.id}
                                                    onDragStart={setDraggedDeal}
                                                    onDragEnd={() => {
                                                        setDraggedDeal(null);
                                                        setDropStageId(null);
                                                    }}
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
                            );
                        })}
                    </div>
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
                        <Field label="Notes" hint="What you agreed, what they asked for, what to do next.">
                            <Textarea
                                rows={3}
                                value={dealForm.notes || ""}
                                onChange={(event) => setDealForm({ ...dealForm, notes: event.target.value })}
                                placeholder="Wants delivery before Diwali. Asked for a 5% discount on 500 units."
                            />
                        </Field>
                    </form>
                </Modal>
            )}

            {noteForm && (
                <Modal
                    title="Note"
                    description={noteForm.name}
                    onClose={() => setNoteForm(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setNoteForm(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="deal-note-form" loading={busy}>
                                Save note
                            </Button>
                        </>
                    }
                >
                    <form id="deal-note-form" onSubmit={saveNote}>
                        <Field label="What should you remember about this deal?">
                            <Textarea
                                autoFocus
                                rows={6}
                                value={noteForm.notes}
                                onChange={(event) => setNoteForm({ ...noteForm, notes: event.target.value })}
                                placeholder="Wants delivery before Diwali. Asked for a 5% discount on 500 units."
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
