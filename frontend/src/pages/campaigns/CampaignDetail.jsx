import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
    CheckCheck,
    Download,
    Eye,
    MessageSquare,
    MousePointerClick,
    Pause,
    Play,
    RotateCcw,
    Send,
    Smartphone,
    TriangleAlert,
    Users,
    X,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import useSelection from "../../hooks/useSelection";
import { downloadFile } from "../../utils/download";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import SendPreview from "../../components/campaigns/SendPreview";
import Card, { CardHeader } from "../../components/ui/Card";
import DataTable from "../../components/ui/DataTable";
import BulkActionBar from "../../components/ui/BulkActionBar";
import StatCard from "../../components/ui/StatCard";
import PageHeader from "../../components/ui/PageHeader";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import { StatusBadge } from "../../components/ui/Badge";
import { StatCardSkeleton } from "../../components/ui/Skeleton";
import { useToast } from "../../components/ui/Toast";

/**
 * CSV of the ticked recipients, built from the rows already on screen.
 *
 * Messages are a read-only audit trail -- there is deliberately no API that
 * changes or removes them -- so exporting the selection is the one bulk
 * operation that genuinely applies here. The whole-campaign CSV from the
 * reports endpoint is untouched and still the way to get every recipient.
 */
function recipientsToCsv(rows) {
    const header = ["phone", "contact", "status", "device", "error", "delay_seconds", "queued_at", "sent_at"];
    const escape = (value) => {
        const text = value === null || value === undefined ? "" : String(value);
        return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const lines = rows.map((message) =>
        [
            message.to_phone,
            message.contact_name,
            message.status,
            message.whatsapp_account_name,
            message.error_message,
            message.assigned_delay_seconds,
            message.queued_at,
            message.sent_at,
        ]
            .map(escape)
            .join(",")
    );
    return [header.join(","), ...lines].join("\n");
}

// Lifecycle actions offered per status, mirroring the backend's own state
// machine (apps/campaigns/views.py). `confirm` gates the destructive ones.
const ACTIONS = {
    DRAFT: [{ action: "launch", label: "Launch Campaign", icon: Send }],
    SCHEDULED: [{ action: "launch", label: "Launch Now", icon: Send }],
    QUEUED: [{ action: "pause", label: "Pause", icon: Pause, variant: "secondary" }],
    RUNNING: [
        { action: "pause", label: "Pause", icon: Pause, variant: "secondary" },
        { action: "cancel", label: "Cancel", icon: X, variant: "danger", confirm: true },
    ],
    PAUSED: [
        { action: "resume", label: "Resume", icon: Play },
        { action: "cancel", label: "Cancel", icon: X, variant: "danger", confirm: true },
    ],
    COMPLETED: [{ action: "retry", label: "Retry Failed", icon: RotateCcw, variant: "secondary" }],
    FAILED: [{ action: "retry", label: "Retry Failed", icon: RotateCcw, variant: "secondary" }],
};

function ProgressBar({ sent, failed, total }) {
    if (!total) return null;
    const sentPct = Math.min((sent / total) * 100, 100);
    const failedPct = Math.min((failed / total) * 100, 100 - sentPct);

    return (
        <div>
            <div className="flex justify-between items-baseline mb-2">
                <span className="text-sm font-semibold text-ink-800">Progress</span>
                <span className="text-sm text-ink-500 tabular-nums">
                    {sent + failed} of {total} processed
                </span>
            </div>
            <div className="flex h-2 rounded-full bg-ink-200 overflow-hidden">
                <div className="bg-brand-500 transition-all" style={{ width: `${sentPct}%` }} />
                <div className="bg-red-500 transition-all" style={{ width: `${failedPct}%` }} />
            </div>
        </div>
    );
}

export default function CampaignDetail() {
    const { id } = useParams();
    const navigate = useNavigate();
    const toast = useToast();
    const [campaign, setCampaign] = useState(null);
    const [report, setReport] = useState(null);
    const [recipients, setRecipients] = useState([]);
    const [recipientPage, setRecipientPage] = useState(1);
    const recipientSelection = useSelection(recipients);
    const [recipientCount, setRecipientCount] = useState(0);
    // The full pre-send picture -- eligible, skipped by reason, credits,
    // blockers -- rather than the bare recipient count this used to show.
    // Every number in it is the server's; see components/campaigns/SendPreview.
    const [preview, setPreview] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);
    const [error, setError] = useState("");
    const [actionError, setActionError] = useState("");
    const [busy, setBusy] = useState(false);
    const [downloading, setDownloading] = useState(false);
    const [pendingAction, setPendingAction] = useState(null);
    // Per-URL click totals. Null until the campaign has actually sent, and
    // left null when link tracking was off for this campaign.
    const [links, setLinks] = useState(null);
    const [retargeting, setRetargeting] = useState(false);

    const downloadReport = async () => {
        setDownloading(true);
        setActionError("");
        try {
            await downloadFile(`/reports/campaigns/${id}/export/`, `campaign-${id}-report.csv`);
        } catch (err) {
            setActionError(apiErrorMessage(err, "Unable to download this report."));
        } finally {
            setDownloading(false);
        }
    };

    const load = async () => {
        try {
            const campaignRes = await api.get(`/campaigns/campaigns/${id}/`);
            setCampaign(campaignRes.data);
            if (campaignRes.data.status === "DRAFT" || campaignRes.data.status === "SCHEDULED") {
                setPreviewLoading(true);
                try {
                    const previewRes = await api.get(`/campaigns/campaigns/${id}/preview/`);
                    setPreview(previewRes.data);
                } finally {
                    setPreviewLoading(false);
                }
            } else {
                const [reportRes, recipientsRes] = await Promise.all([
                    api.get(`/reports/campaigns/${id}/`),
                    api.get("/messaging/messages/", {
                        params: { campaign: id, direction: "OUTBOUND", page: recipientPage, ordering: "id" },
                    }),
                ]);
                setReport(reportRes.data);
                setRecipients(recipientsRes.data.results || []);
                setRecipientCount(recipientsRes.data.count || 0);

                // Its own request, and its own failure: a campaign report is
                // still worth reading if the click breakdown cannot be had.
                api.get("/tracking/links/summary/", { params: { campaign: id } })
                    .then((response) => setLinks(response.data.links || []))
                    .catch(() => setLinks(null));
            }
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load this campaign."));
        }
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [id, recipientPage]);

    const runAction = async (action) => {
        setBusy(true);
        setActionError("");
        try {
            await api.post(`/campaigns/campaigns/${id}/${action}/`);
            toast.success(`Campaign ${action === "retry" ? "retrying failed messages" : `${action}ed`}.`);
            await load();
        } catch (err) {
            setActionError(apiErrorMessage(err, `Unable to ${action} this campaign.`));
        } finally {
            setPendingAction(null);
            setBusy(false);
        }
    };

    /**
     * Start a campaign aimed at the people who clicked.
     *
     * The ids are resolved here rather than in the wizard so that the button
     * can say how many there are before anyone commits to writing a message
     * for them.
     */
    const retargetClickers = async () => {
        setRetargeting(true);
        setActionError("");
        try {
            const response = await api.get("/tracking/links/clickers/", { params: { campaign: id } });
            if (!response.data.count) {
                setActionError("Nobody has clicked a link in this campaign yet.");
                return;
            }
            navigate("/campaigns/new", {
                state: {
                    contactIds: response.data.contact_ids,
                    audienceLabel: `Everyone who clicked a link in “${campaign.name}”.`,
                },
            });
        } catch (err) {
            setActionError(apiErrorMessage(err, "Unable to work out who clicked."));
        } finally {
            setRetargeting(false);
        }
    };

    if (error) return <Alert>{error}</Alert>;

    if (!campaign) {
        return (
            <div>
                <PageHeader title="Campaign" backTo="/campaigns" backLabel="Back to campaigns" />
                <StatCardSkeleton count={5} />
            </div>
        );
    }

    const availableActions = ACTIONS[campaign.status] || [];
    const stats = report || {
        total_recipients: campaign.total_recipients,
        sent: campaign.sent_count,
        delivered: campaign.delivered_count,
        read: campaign.read_count,
        failed: campaign.failed_count,
        skipped: campaign.skipped_count,
        replies: 0,
    };

    const exportSelectedRecipients = () => {
        const csv = recipientsToCsv(recipientSelection.selectedRows);
        const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = `campaign-${id}-recipients-selected.csv`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        toast.success(
            `${recipientSelection.selectedCount} recipient${
                recipientSelection.selectedCount === 1 ? "" : "s"
            } exported.`
        );
        recipientSelection.clear();
    };

    const recipientColumns = [
        {
            key: "recipient",
            header: "Recipient",
            render: (message) => (
                <div className="min-w-0">
                    <p className="font-semibold text-ink-900 truncate m-0">{message.contact_name || message.to_phone}</p>
                    {message.contact_name && (
                        <p className="text-xs text-ink-500 truncate m-0 tabular-nums">{message.to_phone}</p>
                    )}
                </div>
            ),
        },
        {
            key: "account",
            header: "WhatsApp Account",
            hideBelow: "sm",
            render: (message) => (
                <div className="min-w-0">
                    <p className="text-ink-700 truncate m-0">{message.whatsapp_account_name || "—"}</p>
                    {message.whatsapp_account_phone && (
                        <p className="text-xs text-ink-500 truncate m-0 tabular-nums">
                            {message.whatsapp_account_phone}
                        </p>
                    )}
                </div>
            ),
        },
        {
            key: "delay",
            header: "Delay",
            align: "right",
            hideBelow: "md",
            render: (message) => (
                <span className="tabular-nums text-ink-600">
                    {message.assigned_delay_seconds === null || message.assigned_delay_seconds === undefined
                        ? "—"
                        : `${Number(message.assigned_delay_seconds).toFixed(2)}s`}
                </span>
            ),
        },
        {
            key: "status",
            header: "Status",
            align: "right",
            render: (message) => (
                <div className="flex flex-col items-end gap-1">
                    <StatusBadge status={message.status} />
                    {message.error_message && (
                        <span className="text-xs text-ink-500 max-w-[16rem] truncate" title={message.error_message}>
                            {message.error_message}
                        </span>
                    )}
                </div>
            ),
        },
    ];

    const recipientPages = Math.ceil(recipientCount / 20) || 1;

    return (
        <div>
            <PageHeader
                backTo="/campaigns"
                backLabel="Back to campaigns"
                title={
                    <span className="inline-flex flex-wrap items-center gap-3">
                        {campaign.name}
                        <StatusBadge status={campaign.status} />
                    </span>
                }
                actions={
                    <>
                        {availableActions.map(({ action, label, icon, variant, confirm }) => (
                            <Button
                                key={action}
                                icon={icon}
                                variant={variant}
                                onClick={() => (confirm ? setPendingAction({ action, label }) : runAction(action))}
                                disabled={busy}
                            >
                                {label}
                            </Button>
                        ))}
                        {report && (
                            <Button variant="secondary" icon={Download} onClick={downloadReport} loading={downloading}>
                                Export CSV
                            </Button>
                        )}
                    </>
                }
            />

            <Alert>{actionError}</Alert>

            {(preview || previewLoading) && (
                <SendPreview preview={preview} loading={previewLoading} />
            )}

            <div className="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6">
                <StatCard label="Recipients" value={stats.total_recipients} icon={Users} tone="violet" />
                <StatCard label="Sent" value={stats.sent} icon={Send} tone="brand" />
                <StatCard
                    label="Delivered"
                    value={stats.delivered}
                    hint={report ? `${report.delivery_rate}%` : ""}
                    icon={CheckCheck}
                    tone="brand"
                />
                <StatCard label="Read" value={stats.read} hint={report ? `${report.read_rate}%` : ""} icon={Eye} tone="blue" />
                <StatCard label="Failed" value={stats.failed} icon={TriangleAlert} tone="red" />
                <StatCard
                    label="Clicked"
                    value={report ? report.contacts_clicked : 0}
                    hint={report ? `${report.click_rate}% of sent` : ""}
                    icon={MousePointerClick}
                    tone="violet"
                />
            </div>

            {stats.skipped > 0 && (
                <Alert tone="warning" title="Some recipients were skipped">
                    {stats.skipped} recipient{stats.skipped === 1 ? " was" : "s were"} not attempted because no
                    connected WhatsApp device was available for them. Reconnect a device and use Retry Failed to send
                    them.
                </Alert>
            )}

            {stats.total_recipients > 0 && (
                <Card className="mb-6">
                    <ProgressBar sent={stats.sent} failed={stats.failed} total={stats.total_recipients} />
                </Card>
            )}

            {report?.accounts?.length > 1 && (
                <Card className="mb-6">
                    <CardHeader
                        title="Sending devices"
                        description="How this campaign's recipients were distributed across your WhatsApp accounts."
                        className="mb-4"
                    />
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                        {report.accounts.map((account) => (
                            <div key={account.account_id} className="rounded-xl border border-ink-200 p-3.5">
                                <div className="flex items-center gap-2.5 min-w-0">
                                    <span className="shrink-0 w-8 h-8 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
                                        <Smartphone size={15} aria-hidden="true" />
                                    </span>
                                    <div className="min-w-0">
                                        <p className="text-sm font-semibold text-ink-900 truncate m-0">{account.name}</p>
                                        <p className="text-xs text-ink-500 truncate m-0 tabular-nums">
                                            {account.phone_number || "—"}
                                        </p>
                                    </div>
                                </div>
                                <div className="mt-3 pt-3 border-t border-ink-100 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-600 tabular-nums">
                                    <span>
                                        <strong className="text-ink-900">{account.total}</strong> assigned
                                    </span>
                                    <span>
                                        <strong className="text-ink-900">{account.sent}</strong> sent
                                    </span>
                                    {account.failed > 0 && <span className="text-red-600">{account.failed} failed</span>}
                                    {account.skipped > 0 && (
                                        <span className="text-amber-700">{account.skipped} skipped</span>
                                    )}
                                    {account.avg_delay_seconds !== null && (
                                        <span>avg {Number(account.avg_delay_seconds).toFixed(2)}s delay</span>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>
                </Card>
            )}

            {links?.length > 0 && (
                <Card className="mb-6">
                    <CardHeader
                        title="Link clicks"
                        description="Grouped by the link you wrote, not by the per-recipient short code."
                        action={
                            <Button
                                size="sm"
                                variant="secondary"
                                icon={MousePointerClick}
                                loading={retargeting}
                                onClick={retargetClickers}
                            >
                                Message everyone who clicked
                            </Button>
                        }
                        className="mb-4"
                    />
                    <ul className="list-none p-0 m-0 space-y-2.5">
                        {links.map((link) => (
                            <li key={link.url} className="rounded-xl border border-ink-200 p-3.5">
                                <a
                                    href={link.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-sm font-semibold text-brand-700 hover:underline break-all"
                                >
                                    {link.url}
                                </a>
                                <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-600 tabular-nums">
                                    <span>
                                        <strong className="text-ink-900">{link.contacts_clicked}</strong> of{" "}
                                        {link.recipients} clicked
                                    </span>
                                    <span>
                                        <strong className="text-ink-900">{link.click_rate}%</strong> click rate
                                    </span>
                                    <span>{link.clicks} total taps</span>
                                </div>
                                <div className="mt-2 h-1.5 rounded-full bg-ink-100 overflow-hidden">
                                    <div
                                        className="h-full bg-violet-500 rounded-full"
                                        style={{ width: `${Math.min(link.click_rate, 100)}%` }}
                                    />
                                </div>
                            </li>
                        ))}
                    </ul>
                </Card>
            )}

            {links?.length === 0 && campaign.track_links && stats.sent > 0 && (
                <Card className="mb-6">
                    <CardHeader title="Link clicks" description="No links were found in this campaign's message." />
                </Card>
            )}

            {report && recipients.length > 0 && (
                <div className="mb-6">
                    <CardHeader
                        title="Recipients"
                        description="The device each message was sent from and the random delay generated for it."
                        className="mb-3"
                    />
                    <BulkActionBar
                        count={recipientSelection.selectedCount}
                        label="recipient"
                        onClear={recipientSelection.clear}
                    >
                        <Button
                            size="sm"
                            variant="secondary"
                            icon={Download}
                            onClick={exportSelectedRecipients}
                        >
                            Export selected
                        </Button>
                    </BulkActionBar>

                    <DataTable
                        columns={recipientColumns}
                        rows={recipients}
                        selection={recipientSelection}
                        footer={
                            recipientPages > 1 && (
                                <div className="flex items-center justify-between gap-3">
                                    <span className="text-xs text-ink-500 tabular-nums">
                                        Page {recipientPage} of {recipientPages} · {recipientCount} recipients
                                    </span>
                                    <div className="flex gap-2">
                                        <Button
                                            size="sm"
                                            variant="secondary"
                                            disabled={recipientPage <= 1}
                                            onClick={() => setRecipientPage((page) => page - 1)}
                                        >
                                            Previous
                                        </Button>
                                        <Button
                                            size="sm"
                                            variant="secondary"
                                            disabled={recipientPage >= recipientPages}
                                            onClick={() => setRecipientPage((page) => page + 1)}
                                        >
                                            Next
                                        </Button>
                                    </div>
                                </div>
                            )
                        }
                    />
                </div>
            )}

            {report && (
                <Card>
                    <CardHeader title="Replies" description="Contacts who replied after receiving this campaign." className="mb-4" />
                    <div className="flex items-center gap-3">
                        <span className="w-10 h-10 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center shrink-0">
                            <MessageSquare size={19} aria-hidden="true" />
                        </span>
                        <p className="text-sm text-ink-600 m-0">
                            <strong className="text-ink-900">{report.replies}</strong> contact
                            {report.replies === 1 ? "" : "s"} replied ({report.reply_rate}% reply rate).
                        </p>
                    </div>
                </Card>
            )}

            {pendingAction && (
                <ConfirmDialog
                    title={`${pendingAction.label} campaign`}
                    message={`${pendingAction.label} “${campaign.name}”? Messages that have already been sent cannot be recalled.`}
                    confirmLabel={pendingAction.label}
                    onConfirm={() => runAction(pendingAction.action)}
                    onClose={() => setPendingAction(null)}
                />
            )}
        </div>
    );
}
