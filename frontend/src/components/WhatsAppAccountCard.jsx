import { Smartphone } from "lucide-react";
import { StatusBadge } from "./ui/Badge";
import { Checkbox } from "./ui/Field";
import Button from "./ui/Button";
import Alert from "./ui/Alert";
import { SenderHealthStrip } from "./SenderHealth";
import { ACCOUNT_TYPE_LABEL, CONNECTION_METHOD_LABEL } from "../constants/whatsapp";

const NEEDS_RECONNECT = new Set(["DISCONNECTED", "ERROR", "EXPIRED"]);
const IN_PROGRESS = new Set(["CONNECTING", "QR_READY", "DISCONNECTING"]);

/** Relative "last active" from the account's last_seen_at timestamp. */
function formatLastActive(value) {
    if (!value) return "Never";
    const seenAt = new Date(value);
    if (Number.isNaN(seenAt.getTime())) return "Unknown";

    const minutes = Math.round((Date.now() - seenAt.getTime()) / 60000);
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes}m ago`;
    if (minutes < 1440) return `${Math.round(minutes / 60)}h ago`;
    return seenAt.toLocaleDateString();
}

function DetailRow({ label, value }) {
    return (
        <div className="flex items-center justify-between gap-3 py-1.5">
            <span className="text-xs text-ink-500">{label}</span>
            <span className="text-sm font-medium text-ink-800 truncate text-right">{value}</span>
        </div>
    );
}

function WhatsAppAccountCard({
    account, onReconnect, onDisconnect, onDelete, busy, selected = false, onToggleSelect,
    health, onPauseSending, onResumeSending, onOpenLimits,
}) {
    const isConnected = account.status === "CONNECTED";
    const isInProgress = IN_PROGRESS.has(account.status);
    const needsReconnect = NEEDS_RECONNECT.has(account.status);

    return (
        <div
            className={`bg-white border rounded-card shadow-card p-5 flex flex-col ${
                selected ? "border-brand-400 ring-1 ring-brand-500/20" : "border-ink-200"
            }`}
        >
            <div className="flex items-start gap-3">
                {onToggleSelect && (
                    <Checkbox
                        checked={selected}
                        onChange={onToggleSelect}
                        className="mt-3"
                        aria-label={`Select ${account.name}`}
                    />
                )}
                <span className="shrink-0 w-10 h-10 rounded-xl bg-brand-50 text-brand-600 flex items-center justify-center">
                    <Smartphone size={19} aria-hidden="true" />
                </span>
                <div className="flex-1 min-w-0">
                    <h3 className="text-sm font-bold text-ink-900 truncate m-0">{account.name}</h3>
                    <p className="text-sm text-ink-500 truncate m-0 tabular-nums">
                        {account.phone_number || "Phone not connected"}
                    </p>
                </div>
                <StatusBadge status={account.status} />
            </div>

            {account.error_message && (
                <Alert tone="error" className="mt-4 mb-0">
                    {account.error_message}
                </Alert>
            )}

            <div className="mt-4 pt-3 border-t border-ink-100">
                <DetailRow label="Type" value={ACCOUNT_TYPE_LABEL[account.account_type] || account.account_type} />
                <DetailRow
                    label="Connection"
                    value={CONNECTION_METHOD_LABEL[account.connection_method] || account.connection_method}
                />
                <DetailRow label="Last active" value={formatLastActive(account.last_seen_at)} />
                <DetailRow
                    label="Campaigns"
                    value={
                        account.active_campaign_count > 0
                            ? `${account.active_campaign_count} in progress`
                            : "None in progress"
                    }
                />
            </div>

            <SenderHealthStrip
                health={health}
                busy={busy}
                onPause={() => onPauseSending?.(account)}
                onResume={() => onResumeSending?.(account)}
                onOpenLimits={() => onOpenLimits?.(account)}
            />

            <div className="flex flex-wrap justify-end gap-2 mt-4 pt-4 border-t border-ink-100">
                {needsReconnect && (
                    <Button size="sm" onClick={() => onReconnect(account)} disabled={busy}>
                        Reconnect
                    </Button>
                )}
                {isInProgress && (
                    <Button size="sm" loading disabled>
                        {account.status === "QR_READY" ? "Waiting for scan" : "Connecting"}
                    </Button>
                )}
                {isConnected && (
                    <>
                        <Button variant="secondary" size="sm" onClick={() => onReconnect(account)} disabled={busy}>
                            Details
                        </Button>
                        <Button variant="secondary" size="sm" onClick={() => onDisconnect(account.id)} disabled={busy}>
                            Disconnect
                        </Button>
                    </>
                )}
                {/* Always offered, mid-connection included: a pairing attempt that
                    is never scanned sits at QR_READY, and hiding Delete there left
                    the device permanently unremovable. */}
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => onDelete(account.id)}>
                    Delete
                </Button>
            </div>
        </div>
    );
}

export default WhatsAppAccountCard;
