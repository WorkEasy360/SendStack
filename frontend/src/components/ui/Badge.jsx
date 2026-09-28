const TONES = {
    green: "bg-brand-50 text-brand-700 ring-brand-600/20",
    amber: "bg-amber-50 text-amber-700 ring-amber-600/20",
    red: "bg-red-50 text-red-700 ring-red-600/20",
    gray: "bg-ink-100 text-ink-600 ring-ink-500/20",
    blue: "bg-blue-50 text-blue-700 ring-blue-600/20",
    violet: "bg-violet-50 text-violet-700 ring-violet-600/20",
};

const DOTS = {
    green: "bg-brand-500",
    amber: "bg-amber-500",
    red: "bg-red-500",
    gray: "bg-ink-400",
    blue: "bg-blue-500",
    violet: "bg-violet-500",
};

// Backend status strings -> tone. Kept exhaustive so any status the API can
// return still renders with a sensible colour rather than falling to gray.
const STATUS_TONE = {
    CONNECTED: "green",
    SENT: "blue",
    DELIVERED: "green",
    READ: "green",
    RUNNING: "blue",
    QUEUED: "amber",
    SCHEDULED: "amber",
    CONNECTING: "amber",
    QR_READY: "amber",
    DISCONNECTING: "amber",
    SENDING: "amber",
    PENDING: "amber",
    PROCESSING: "amber",
    COMPLETED: "green",
    // Webhook delivery outcomes (Settings -> Developer).
    SUCCEEDED: "green",
    PAUSED: "gray",
    DRAFT: "gray",
    DISCONNECTED: "gray",
    CANCELLED: "gray",
    SKIPPED: "amber",
    FAILED: "red",
    ERROR: "red",
    EXPIRED: "red",
    OPEN: "green",
    CLOSED: "gray",
};

// Statuses that mean "something is happening right now" get a pulsing dot.
const LIVE_STATUSES = new Set(["RUNNING", "CONNECTING", "QR_READY", "SENDING", "PROCESSING", "DISCONNECTING"]);

function labelFor(value) {
    if (typeof value !== "string") return value;
    return value
        .split("_")
        .map((word) => word.charAt(0) + word.slice(1).toLowerCase())
        .join(" ");
}

export default function Badge({ children, tone, className = "" }) {
    const resolvedTone = tone || STATUS_TONE[children] || "gray";
    return (
        <span
            className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-semibold ring-1 ring-inset ${TONES[resolvedTone]} ${className}`}
        >
            {children}
        </span>
    );
}

/**
 * Status pill with a leading dot, for connection/campaign/message states.
 * Pass the raw backend status (e.g. `CONNECTED`) — it is title-cased here.
 */
export function StatusBadge({ status, tone, className = "" }) {
    const resolvedTone = tone || STATUS_TONE[status] || "gray";
    const isLive = LIVE_STATUSES.has(status);

    return (
        <span
            className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-xs font-semibold ring-1 ring-inset ${TONES[resolvedTone]} ${className}`}
        >
            <span className={`w-1.5 h-1.5 rounded-full ${DOTS[resolvedTone]} ${isLive ? "animate-pulse" : ""}`} />
            {labelFor(status)}
        </span>
    );
}
