/**
 * Formatting for plan prices and limits.
 *
 * Two conventions from the backend that every billing screen has to render the
 * same way, so they live here once rather than in each page:
 *
 * * Money is stored in the currency's **minor unit** (paise, cents), because
 *   storing money as a float is how a rounding error becomes a refund request.
 *   Nothing in the UI should divide by 100 by hand.
 * * **0 means unlimited**, throughout the whole codebase -- plan limits, team
 *   capacity, reply caps. A page that printed "0 contacts" for the Enterprise
 *   plan would be reading the data correctly and saying the opposite of what
 *   it means.
 */

/** A price, from its minor-unit integer. `9900, "INR"` -> "₹99". */
export function formatMoney(minorUnits, currency = "INR") {
    const amount = Number(minorUnits || 0) / 100;
    try {
        return new Intl.NumberFormat(undefined, {
            style: "currency",
            currency,
            // Whole rupees where the price is whole, which almost every plan
            // price is. "₹999" reads as a price; "₹999.00" reads as a receipt.
            minimumFractionDigits: Number.isInteger(amount) ? 0 : 2,
            maximumFractionDigits: 2,
        }).format(amount);
    } catch {
        // An unknown or malformed currency code must not blank the price.
        return `${currency} ${amount.toLocaleString()}`;
    }
}

/** A plan limit. 0 is unlimited -- see the module note. */
export function formatLimit(value) {
    const number = Number(value || 0);
    if (number === 0) return "Unlimited";
    return number.toLocaleString();
}

/** Percentage used, clamped, or null when the limit is unlimited. */
export function usagePercent(used, limit) {
    const cap = Number(limit || 0);
    if (cap === 0) return null;
    return Math.min(Math.round((Number(used || 0) / cap) * 100), 100);
}

/**
 * The tone a usage figure should be shown in.
 *
 * The thresholds are the ones the phase brief names -- 80 / 90 / 100 -- and
 * they are here rather than in each screen so the dashboard, the billing page
 * and the contacts page all call the same number "nearly full".
 */
export function usageTone(percent) {
    if (percent === null || percent === undefined) return "ok";
    if (percent >= 100) return "full";
    if (percent >= 90) return "critical";
    if (percent >= 80) return "warning";
    return "ok";
}

/** How a subscription status should read to a customer. */
export const SUBSCRIPTION_LABELS = {
    TRIALING: "Trial",
    ACTIVE: "Active",
    PAST_DUE: "Payment due",
    CANCELLED: "Cancelled",
    EXPIRED: "Expired",
};

export function subscriptionLabel(status) {
    return SUBSCRIPTION_LABELS[status] || status || "No plan";
}
