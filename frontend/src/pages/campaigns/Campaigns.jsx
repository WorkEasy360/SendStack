import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Ban, Download, Pause, Play, Plus, Send, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import useSelection from "../../hooks/useSelection";
import { downloadFile } from "../../utils/download";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import SearchInput from "../../components/ui/SearchInput";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import BulkActionBar from "../../components/ui/BulkActionBar";
import { useToast } from "../../components/ui/Toast";
import { StatusBadge } from "../../components/ui/Badge";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { Select } from "../../components/ui/Field";

const REPORTABLE_STATUSES = new Set(["RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]);
// Mirrors CampaignViewSet.perform_destroy -- anything mid-flight (SCHEDULED,
// QUEUED, RUNNING, PAUSED) has to be cancelled first, so offering Delete on
// those would only produce a rejection.
const DELETABLE_STATUSES = new Set(["DRAFT", "COMPLETED", "FAILED", "CANCELLED"]);
// The lifecycle endpoints each accept only certain statuses; the bulk bar
// hides an action entirely when nothing selected could take it, rather than
// offering a button whose only outcome is a rejection.
const CANCELLABLE_STATUSES = new Set(["SCHEDULED", "QUEUED", "RUNNING", "PAUSED"]);
const PAUSABLE_STATUSES = new Set(["RUNNING"]);
const RESUMABLE_STATUSES = new Set(["PAUSED"]);

const STATUS_OPTIONS = [
    "DRAFT",
    "SCHEDULED",
    "QUEUED",
    "RUNNING",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
];

export default function Campaigns() {
    const toast = useToast();
    const [campaigns, setCampaigns] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [downloadingId, setDownloadingId] = useState(null);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [deletingId, setDeletingId] = useState(null);
    const [search, setSearch] = useState("");
    const [statusFilter, setStatusFilter] = useState("");
    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);
    const selection = useSelection(campaigns);

    // `search` and `status` are both supported filters on the campaign
    // list endpoint, so filtering stays server-side and paginates correctly.
    const load = (overrides = {}) => {
        const { searchTerm = search, status = statusFilter } = overrides;
        setLoading(true);
        const params = { ordering: "-created_at" };
        if (searchTerm) params.search = searchTerm;
        if (status) params.status = status;

        api.get("/campaigns/campaigns/", { params })
            .then((res) => {
                setCampaigns(res.data.results || []);
                setError("");
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load campaigns.")))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const deleteCampaign = async (campaign) => {
        setDeletingId(campaign.id);
        setError("");
        try {
            await api.delete(`/campaigns/campaigns/${campaign.id}/`);
            toast.success("Campaign deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this campaign."));
        } finally {
            setPendingDelete(null);
            setDeletingId(null);
            load();
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const result = await runBulk("/campaigns/campaigns/bulk/", { action, ids: selection.selectedIds });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0) {
            setError(describeBulkFailures(result, (id) => campaigns.find((c) => c.id === id)?.name));
        }
        selection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        load();
    };

    // Reports are per-campaign CSVs, so "download the selection" is one file
    // each, fetched in sequence rather than as a burst of parallel requests.
    const downloadSelectedReports = async () => {
        const reportable = selection.selectedRows.filter((campaign) => REPORTABLE_STATUSES.has(campaign.status));
        if (!reportable.length) {
            setError("None of the selected campaigns has started sending yet, so there is no report to download.");
            return;
        }
        setBulkBusy(true);
        setError("");
        let failed = 0;
        for (const campaign of reportable) {
            try {
                await downloadFile(`/reports/campaigns/${campaign.id}/export/`, `campaign-${campaign.id}-report.csv`);
            } catch {
                failed += 1;
            }
        }
        setBulkBusy(false);
        if (failed) setError(`${failed} of ${reportable.length} reports could not be downloaded.`);
        else toast.success(`${reportable.length} report${reportable.length === 1 ? "" : "s"} downloaded.`);
    };

    const downloadReport = async (campaign) => {
        setDownloadingId(campaign.id);
        setError("");
        try {
            await downloadFile(`/reports/campaigns/${campaign.id}/export/`, `campaign-${campaign.id}-report.csv`);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to download this report."));
        } finally {
            setDownloadingId(null);
        }
    };

    const hasFilters = Boolean(search || statusFilter);
    const selectedHas = (statuses) => selection.selectedRows.some((campaign) => statuses.has(campaign.status));

    const columns = [
        {
            key: "name",
            header: "Campaign",
            render: (campaign) => (
                <Link
                    to={`/campaigns/${campaign.id}`}
                    className="font-semibold text-ink-900 hover:text-brand-700 no-underline"
                >
                    {campaign.name}
                </Link>
            ),
        },
        { key: "status", header: "Status", render: (campaign) => <StatusBadge status={campaign.status} /> },
        {
            key: "recipients",
            header: "Recipients",
            align: "right",
            render: (campaign) => <span className="tabular-nums">{campaign.total_recipients}</span>,
        },
        {
            key: "sent",
            header: "Sent",
            align: "right",
            hideBelow: "sm",
            render: (campaign) => <span className="tabular-nums">{campaign.sent_count}</span>,
        },
        {
            key: "delivered",
            header: "Delivered",
            align: "right",
            hideBelow: "md",
            render: (campaign) => <span className="tabular-nums">{campaign.delivered_count}</span>,
        },
        {
            key: "failed",
            header: "Failed",
            align: "right",
            hideBelow: "md",
            render: (campaign) => (
                <span className={`tabular-nums ${campaign.failed_count > 0 ? "text-red-600 font-semibold" : ""}`}>
                    {campaign.failed_count}
                </span>
            ),
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (campaign) => (
                <div className="flex justify-end gap-3 whitespace-nowrap">
                    <Link
                        to={`/campaigns/${campaign.id}`}
                        className="text-sm font-semibold text-brand-700 hover:text-brand-800 no-underline transition-colors"
                    >
                        View
                    </Link>
                    {REPORTABLE_STATUSES.has(campaign.status) && (
                        <RowAction onClick={() => downloadReport(campaign)} disabled={downloadingId === campaign.id}>
                            {downloadingId === campaign.id ? "Downloading..." : "Report"}
                        </RowAction>
                    )}
                    {DELETABLE_STATUSES.has(campaign.status) && (
                        <RowAction
                            tone="danger"
                            onClick={() => setPendingDelete(campaign)}
                            disabled={deletingId === campaign.id}
                        >
                            Delete
                        </RowAction>
                    )}
                </div>
            ),
        },
    ];

    return (
        <div>
            <PageHeader
                title="Campaigns"
                description={
                    loading ? "Bulk WhatsApp sends to your audience." : `${campaigns.length} campaign${campaigns.length === 1 ? "" : "s"} shown`
                }
                actions={
                    <Link to="/campaigns/new" className="no-underline">
                        <Button icon={Plus}>New Campaign</Button>
                    </Link>
                }
            />

            <Alert>{error}</Alert>

            <div className="flex flex-col sm:flex-row gap-2 mb-4">
                <form
                    onSubmit={(event) => {
                        event.preventDefault();
                        load();
                    }}
                    className="flex-1 sm:max-w-sm"
                >
                    <SearchInput
                        value={search}
                        onChange={(event) => setSearch(event.target.value)}
                        onClear={() => {
                            setSearch("");
                            load({ searchTerm: "" });
                        }}
                        placeholder="Search campaigns..."
                    />
                </form>
                <Select
                    value={statusFilter}
                    onChange={(event) => {
                        setStatusFilter(event.target.value);
                        load({ status: event.target.value });
                    }}
                    aria-label="Filter by status"
                    className="sm:w-48"
                >
                    <option value="">All statuses</option>
                    {STATUS_OPTIONS.map((status) => (
                        <option key={status} value={status}>
                            {status.charAt(0) + status.slice(1).toLowerCase()}
                        </option>
                    ))}
                </Select>
            </div>

            <BulkActionBar count={selection.selectedCount} label="campaign" onClear={selection.clear}>
                <Button size="sm" variant="secondary" icon={Download} disabled={bulkBusy} onClick={downloadSelectedReports}>
                    Download reports
                </Button>
                {selectedHas(PAUSABLE_STATUSES) && (
                    <Button size="sm" variant="secondary" icon={Pause} disabled={bulkBusy} onClick={() => runBulkAction("pause")}>
                        Pause
                    </Button>
                )}
                {selectedHas(RESUMABLE_STATUSES) && (
                    <Button size="sm" variant="secondary" icon={Play} disabled={bulkBusy} onClick={() => runBulkAction("resume")}>
                        Resume
                    </Button>
                )}
                {selectedHas(CANCELLABLE_STATUSES) && (
                    <Button size="sm" variant="secondary" icon={Ban} disabled={bulkBusy} onClick={() => setPendingBulk("cancel")}>
                        Cancel
                    </Button>
                )}
                <Button size="sm" variant="danger" icon={Trash2} disabled={bulkBusy} onClick={() => setPendingBulk("delete")}>
                    Delete
                </Button>
            </BulkActionBar>

            {loading ? (
                <TableSkeleton rows={5} columns={6} />
            ) : (
                <DataTable
                    columns={columns}
                    rows={campaigns}
                    selection={selection}
                    empty={
                        hasFilters ? (
                            <EmptyState
                                icon={Send}
                                title="No matching campaigns"
                                description="No campaign matches your current search and filters."
                                action={
                                    <Button
                                        variant="secondary"
                                        onClick={() => {
                                            setSearch("");
                                            setStatusFilter("");
                                            load({ searchTerm: "", status: "" });
                                        }}
                                    >
                                        Clear filters
                                    </Button>
                                }
                            />
                        ) : (
                            <EmptyState
                                icon={Send}
                                title="No campaigns yet"
                                description="Create your first campaign to start sending WhatsApp messages to your contacts."
                                action={
                                    <Link to="/campaigns/new" className="no-underline">
                                        <Button icon={Plus}>New Campaign</Button>
                                    </Link>
                                }
                            />
                        )
                    }
                    footer={
                        campaigns.some((campaign) => REPORTABLE_STATUSES.has(campaign.status)) ? (
                            <p className="text-xs text-ink-500 m-0 inline-flex items-center gap-1.5">
                                <Download size={13} aria-hidden="true" />
                                Per-recipient CSV reports are available for campaigns that have started sending.
                            </p>
                        ) : null
                    }
                />
            )}

            {pendingBulk && (
                <ConfirmDialog
                    title={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount} campaign${selection.selectedCount === 1 ? "" : "s"}`
                            : `Cancel ${selection.selectedCount} campaign${selection.selectedCount === 1 ? "" : "s"}`
                    }
                    message={
                        pendingBulk === "delete"
                            ? `Delete the ${selection.selectedCount} selected campaign${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? Their send history and report data go with them, and this cannot be undone. Anything still sending is refused and left alone.`
                            : `Cancel the ${selection.selectedCount} selected campaign${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? Recipients not yet sent to will not receive the message.`
                    }
                    confirmLabel={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount}`
                            : `Cancel ${selection.selectedCount}`
                    }
                    cancelLabel="Go back"
                    onConfirm={() => runBulkAction(pendingBulk)}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete campaign"
                    // Message.campaign is CASCADE, so the per-recipient send
                    // records go with it -- say so before, not after.
                    message={
                        `Delete "${pendingDelete.name}"? Its ${pendingDelete.sent_count > 0 ? "send history and " : ""}` +
                        "report data are removed with it, and this cannot be undone."
                    }
                    confirmLabel="Delete campaign"
                    onConfirm={() => deleteCampaign(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}
