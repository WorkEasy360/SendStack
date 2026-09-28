import { useEffect, useState } from "react";
import { LayoutGrid, Plug, Plus, PlugZap, Rows3, Smartphone, Trash2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import useSelection from "../../hooks/useSelection";
import WhatsAppAccountCard from "../../components/WhatsAppAccountCard";
import WhatsAppConnectionWizard from "../../components/WhatsAppConnectionWizard";
import SendingLimitsModal from "../../components/SendingLimitsModal";
import { HealthBadge } from "../../components/SenderHealth";
import { ACCOUNT_TYPE_LABEL, CONNECTION_METHOD_LABEL } from "../../constants/whatsapp";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import BulkActionBar from "../../components/ui/BulkActionBar";
import { Checkbox } from "../../components/ui/Field";
import { StatusBadge } from "../../components/ui/Badge";
import { CardGridSkeleton } from "../../components/ui/Skeleton";
import { useToast } from "../../components/ui/Toast";

const NEEDS_RECONNECT = new Set(["DISCONNECTED", "ERROR", "EXPIRED"]);
const VIEW_KEY = "devices_view";

function WhatsAppAccounts() {
    const toast = useToast();
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(true);
    const [showWizard, setShowWizard] = useState(false);
    const [reconnectAccount, setReconnectAccount] = useState(null);
    const [busyId, setBusyId] = useState(null);
    const [error, setError] = useState("");
    const [pendingDelete, setPendingDelete] = useState(null);
    const [view, setView] = useState(() => localStorage.getItem(VIEW_KEY) || "grid");
    const [pendingBulk, setPendingBulk] = useState(null);
    // Health is fetched separately from the account list: it is derived from
    // live message counts rather than stored on the account, so folding it
    // into every account response would make the cheap list endpoint
    // expensive for every other caller of it.
    const [health, setHealth] = useState({});
    const [limitsAccount, setLimitsAccount] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);
    const selection = useSelection(accounts);

    useEffect(() => {
        localStorage.setItem(VIEW_KEY, view);
    }, [view]);

    const loadHealth = async () => {
        try {
            const response = await api.get("/whatsapp/accounts/health/");
            setHealth(Object.fromEntries((response.data || []).map((row) => [row.account_id, row])));
        } catch {
            // Health is a read-out, not a control: if it fails to load the
            // devices page still has to render and stay usable.
            setHealth({});
        }
    };

    const loadAccounts = async () => {
        try {
            const response = await api.get("/whatsapp/accounts/");
            setAccounts(response.data.results || []);
            await loadHealth();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load WhatsApp accounts."));
        } finally {
            setLoading(false);
        }
    };

    const setSending = async (account, paused) => {
        setBusyId(account.id);
        setError("");
        try {
            await api.post(`/whatsapp/accounts/${account.id}/${paused ? "pause" : "resume"}-sending/`);
            toast.success(paused ? `Sending paused on ${account.name}.` : `Sending resumed on ${account.name}.`);
            await loadHealth();
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to change sending for this number."));
        } finally {
            setBusyId(null);
        }
    };

    useEffect(() => {
        loadAccounts();
    }, []);

    const runAction = async (id, action) => {
        setBusyId(id);
        setError("");
        try {
            await api.post(`/whatsapp/accounts/${id}/${action}/`);
            toast.success("Device disconnected.");
        } catch (err) {
            setError(apiErrorMessage(err, `Unable to ${action} this account.`));
        } finally {
            await loadAccounts();
            setBusyId(null);
        }
    };

    const deleteAccount = async (id) => {
        setBusyId(id);
        setError("");
        try {
            await api.delete(`/whatsapp/accounts/${id}/`);
            toast.success("Device deleted.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this account."));
        } finally {
            setPendingDelete(null);
            await loadAccounts();
            setBusyId(null);
        }
    };

    const runBulkAction = async (action) => {
        setBulkBusy(true);
        setError("");
        const result = await runBulk("/whatsapp/accounts/bulk/", { action, ids: selection.selectedIds });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            // Send history no longer refuses a delete, so a failure here is a
            // real one (provider error, permissions); naming the device makes
            // it obvious which of the ticked ones survived.
            setError(describeBulkFailures(result, (id) => accounts.find((a) => a.id === id)?.name));
        }
        selection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        await loadAccounts();
    };

    const openWizard = (account = null) => {
        setReconnectAccount(account);
        setShowWizard(true);
    };

    const connectedCount = accounts.filter((account) => account.status === "CONNECTED").length;
    const anyConnectedSelected = selection.selectedRows.some((account) => account.status === "CONNECTED");

    const columns = [
        {
            key: "name",
            header: "Device",
            render: (account) => (
                <div className="flex items-center gap-2.5 min-w-0">
                    <span className="shrink-0 w-8 h-8 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
                        <Smartphone size={16} aria-hidden="true" />
                    </span>
                    <div className="min-w-0">
                        <p className="font-semibold text-ink-900 truncate m-0">{account.name}</p>
                        <p className="text-xs text-ink-500 truncate m-0 sm:hidden">
                            {account.phone_number || "Not linked"}
                        </p>
                    </div>
                </div>
            ),
        },
        {
            key: "phone",
            header: "Number",
            hideBelow: "sm",
            render: (account) => (
                <span className="tabular-nums text-ink-600">{account.phone_number || "—"}</span>
            ),
        },
        {
            key: "type",
            header: "Provider",
            hideBelow: "md",
            render: (account) => (
                <div>
                    <p className="text-ink-700 m-0">{ACCOUNT_TYPE_LABEL[account.account_type] || account.account_type}</p>
                    <p className="text-xs text-ink-500 m-0">
                        {CONNECTION_METHOD_LABEL[account.connection_method] || account.connection_method}
                    </p>
                </div>
            ),
        },
        {
            key: "campaigns",
            header: "Campaigns",
            hideBelow: "lg",
            render: (account) => (
                <span className="text-ink-600 tabular-nums">
                    {account.active_campaign_count > 0 ? `${account.active_campaign_count} in progress` : "—"}
                </span>
            ),
        },
        {
            key: "health",
            header: "Health",
            render: (account) => <HealthBadge health={health[account.id]} />,
        },
        { key: "status", header: "Status", render: (account) => <StatusBadge status={account.status} /> },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (account) => {
                const isInProgress = ["CONNECTING", "QR_READY", "DISCONNECTING"].includes(account.status);
                return (
                    <div className="flex justify-end gap-3 whitespace-nowrap">
                        {account.status === "CONNECTED" ? (
                            <>
                                <RowAction tone="brand" onClick={() => openWizard(account)}>
                                    Details
                                </RowAction>
                                <RowAction
                                    onClick={() => runAction(account.id, "disconnect")}
                                    disabled={busyId === account.id}
                                >
                                    Disconnect
                                </RowAction>
                            </>
                        ) : (
                            !isInProgress && (
                                <RowAction tone="brand" onClick={() => openWizard(account)} disabled={busyId === account.id}>
                                    {NEEDS_RECONNECT.has(account.status) ? "Reconnect" : "Connect"}
                                </RowAction>
                            )
                        )}
                        {/* Offered mid-connection too -- see WhatsAppAccountCard. */}
                        <RowAction tone="danger" onClick={() => setPendingDelete(account)} disabled={busyId === account.id}>
                            Delete
                        </RowAction>
                    </div>
                );
            },
        },
    ];

    return (
        <div>
            <PageHeader
                title="WhatsApp Devices"
                description={
                    loading
                        ? "Connect and manage the WhatsApp accounts that send your messages."
                        : `${accounts.length} device${accounts.length === 1 ? "" : "s"} · ${connectedCount} connected`
                }
                actions={
                    <>
                        <div className="hidden sm:inline-flex rounded-lg border border-ink-200 bg-surface p-0.5 shadow-card">
                            {[
                                { id: "grid", icon: LayoutGrid, label: "Grid view" },
                                { id: "table", icon: Rows3, label: "Table view" },
                            ].map(({ id, icon: Icon, label }) => (
                                <button
                                    key={id}
                                    type="button"
                                    onClick={() => setView(id)}
                                    aria-label={label}
                                    title={label}
                                    className={`w-8 h-8 inline-flex items-center justify-center rounded-md cursor-pointer border-none transition-colors ${
                                        view === id ? "bg-ink-900 text-ink-50" : "bg-transparent text-ink-500 hover:text-ink-900"
                                    }`}
                                >
                                    <Icon size={15} />
                                </button>
                            ))}
                        </div>
                        <Button icon={Plus} onClick={() => openWizard(null)}>
                            Add Device
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            <BulkActionBar count={selection.selectedCount} label="device" onClear={selection.clear}>
                {anyConnectedSelected && (
                    <Button
                        size="sm"
                        variant="secondary"
                        icon={PlugZap}
                        disabled={bulkBusy}
                        onClick={() => setPendingBulk("disconnect")}
                    >
                        Disconnect
                    </Button>
                )}
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

            {loading ? (
                <CardGridSkeleton count={3} className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" />
            ) : accounts.length === 0 ? (
                <EmptyState
                    icon={Plug}
                    title="No devices connected"
                    description="Connect a WhatsApp account — by scanning a QR code or with Cloud API credentials — to start messaging."
                    action={
                        <Button icon={Plus} onClick={() => openWizard(null)}>
                            Connect WhatsApp
                        </Button>
                    }
                />
            ) : view === "table" ? (
                <DataTable columns={columns} rows={accounts} selection={selection} />
            ) : (
                <>
                    {/* The card grid has no table header to hang a
                        select-all off, so it gets its own. */}
                    <label className="inline-flex items-center gap-2 mb-3 text-sm font-medium text-ink-600 cursor-pointer select-none">
                        <Checkbox
                            checked={selection.allSelected}
                            indeterminate={selection.someSelected}
                            onChange={selection.toggleAll}
                            aria-label={selection.allSelected ? "Deselect all devices" : "Select all devices"}
                        />
                        Select all {accounts.length} device{accounts.length === 1 ? "" : "s"}
                    </label>
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                    {accounts.map((account) => (
                        <WhatsAppAccountCard
                            key={account.id}
                            account={account}
                            busy={busyId === account.id}
                            selected={selection.isSelected(account.id)}
                            onToggleSelect={() => selection.toggle(account.id)}
                            onReconnect={openWizard}
                            onDisconnect={(id) => runAction(id, "disconnect")}
                            onDelete={() => setPendingDelete(account)}
                            health={health[account.id]}
                            onPauseSending={(target) => setSending(target, true)}
                            onResumeSending={(target) => setSending(target, false)}
                            onOpenLimits={setLimitsAccount}
                        />
                    ))}
                </div>
                </>
            )}

            {limitsAccount && (
                <SendingLimitsModal
                    account={limitsAccount}
                    health={health[limitsAccount.id]}
                    onClose={() => setLimitsAccount(null)}
                    onSaved={loadHealth}
                />
            )}

            {showWizard && (
                <WhatsAppConnectionWizard
                    existingAccount={reconnectAccount}
                    onClose={() => setShowWizard(false)}
                    onConnected={loadAccounts}
                />
            )}

            {pendingBulk && (
                <ConfirmDialog
                    title={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount} device${selection.selectedCount === 1 ? "" : "s"}`
                            : `Disconnect ${selection.selectedCount} device${selection.selectedCount === 1 ? "" : "s"}`
                    }
                    message={
                        pendingBulk === "delete"
                            ? `Delete the ${selection.selectedCount} selected device${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? They are disconnected first, so anything still sending from them stops. Past sends stay in your reports. This cannot be undone.`
                            : `Disconnect the ${selection.selectedCount} selected device${
                                  selection.selectedCount === 1 ? "" : "s"
                              }? They stay in the list and can be reconnected, but nothing sends from them until you do.`
                    }
                    tone={pendingBulk === "delete" ? "danger" : "default"}
                    confirmLabel={
                        pendingBulk === "delete"
                            ? `Delete ${selection.selectedCount}`
                            : `Disconnect ${selection.selectedCount}`
                    }
                    onConfirm={() => runBulkAction(pendingBulk)}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {/* A device that has sent anything is never refused, only warned
                about: the delete goes through, its connection is dropped and
                it leaves this list, while the campaigns and messages behind
                it keep their history for reporting. */}
            {pendingDelete && (
                <ConfirmDialog
                    title="Delete device"
                    message={
                        pendingDelete.active_campaign_count > 0
                            ? `Delete "${pendingDelete.name}"? ${pendingDelete.active_campaign_count} campaign${
                                  pendingDelete.active_campaign_count === 1 ? " is" : "s are"
                              } still sending from it — they stop on this device the moment it goes. Past sends stay in your reports. This cannot be undone.`
                            : ["CONNECTING", "QR_READY"].includes(pendingDelete.status)
                            ? `Delete "${pendingDelete.name}"? The pending pairing is cancelled and this cannot be undone.`
                            : `Delete "${pendingDelete.name}"? Its connection is dropped and it leaves this list. Any messages sent from it stay in your reports. This cannot be undone.`
                    }
                    confirmLabel="Delete device"
                    onConfirm={() => deleteAccount(pendingDelete.id)}
                    onClose={() => setPendingDelete(null)}
                />
            )}
        </div>
    );
}

export default WhatsAppAccounts;
