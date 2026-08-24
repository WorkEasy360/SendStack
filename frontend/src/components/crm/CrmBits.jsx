import Badge from "../ui/Badge";

/**
 * The small shared pieces every CRM screen renders: the two badges, and the
 * formatters for money and elapsed time.
 *
 * Kept in one file rather than one file each because they only exist together
 * -- a deal card, a contact row and the timeline all render the same three
 * things, and three of them drifting apart would be immediately visible.
 */

const BAND_TONE = { HOT: "red", WARM: "amber", COLD: "gray" };
const BAND_LABEL = { HOT: "Hot", WARM: "Warm", COLD: "Cold" };

/** HOT / WARM / COLD — the only form of the lead score a user is shown. */
export function ScoreBadge({ band, score, showScore = false }) {
    if (!band) return null;
    return (
        <Badge tone={BAND_TONE[band] || "gray"}>
            {BAND_LABEL[band] || band}
            {showScore && score != null ? ` · ${score}` : ""}
        </Badge>
    );
}

const LIFECYCLE_TONE = {
    NEW: "blue",
    ENGAGED: "violet",
    QUALIFIED: "amber",
    CUSTOMER: "green",
    INACTIVE: "gray",
    LOST: "gray",
};

export const LIFECYCLE_LABEL = {
    NEW: "New",
    ENGAGED: "Engaged",
    QUALIFIED: "Qualified",
    CUSTOMER: "Customer",
    INACTIVE: "Inactive",
    LOST: "Lost",
};

export const LIFECYCLE_OPTIONS = Object.entries(LIFECYCLE_LABEL).map(([value, label]) => ({ value, label }));

export function LifecycleBadge({ status }) {
    if (!status) return null;
    return <Badge tone={LIFECYCLE_TONE[status] || "gray"}>{LIFECYCLE_LABEL[status] || status}</Badge>;
}

const DEAL_TONE = { OPEN: "blue", WON: "green", LOST: "gray" };

export function DealStatusBadge({ status }) {
    if (!status) return null;
    return <Badge tone={DEAL_TONE[status] || "gray"}>{status[0] + status.slice(1).toLowerCase()}</Badge>;
}

/** Money, in the deal's own currency, with no decimals — deals are round numbers. */
export function formatMoney(value, currency = "INR") {
    const amount = Number(value || 0);
    try {
        return new Intl.NumberFormat(undefined, {
            style: "currency",
            currency,
            maximumFractionDigits: 0,
        }).format(amount);
    } catch {
        // An unknown currency code must not blank the whole card.
        return `${currency} ${amount.toLocaleString()}`;
    }
}

/**
 * "3 days ago" / "in 2 hours". Used everywhere a timestamp appears, because
 * on a CRM screen the question is always how long ago, never the date.
 */
export function timeAgo(value) {
    if (!value) return "—";
    const then = new Date(value).getTime();
    if (Number.isNaN(then)) return "—";

    const seconds = Math.round((then - Date.now()) / 1000);
    const future = seconds > 0;
    const abs = Math.abs(seconds);

    const units = [
        [60, "second"],
        [3600, "minute"],
        [86400, "hour"],
        [604800, "day"],
        [2629800, "week"],
        [31557600, "month"],
    ];

    if (abs < 45) return future ? "in a moment" : "just now";
    let label = "year";
    let amount = Math.round(abs / 31557600);
    for (let index = 0; index < units.length; index += 1) {
        const [limit, name] = units[index];
        if (abs < limit) {
            const divisor = index === 0 ? 1 : units[index - 1][0];
            amount = Math.round(abs / divisor);
            label = name;
            break;
        }
    }
    const plural = amount === 1 ? label : `${label}s`;
    return future ? `in ${amount} ${plural}` : `${amount} ${plural} ago`;
}

/** A date, for things a user schedules rather than things that happened. */
export function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function formatDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** The value a datetime-local input wants, from an ISO string. */
export function toLocalInput(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return "";
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
