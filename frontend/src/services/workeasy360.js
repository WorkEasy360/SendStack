/**
 * Sign-in with WorkEasy360 Accounts, from the browser's side.
 *
 * A WorkEasy360 sign-in never puts a token in localStorage. The backend keeps
 * a server-side session in an httpOnly cookie (docs/workeasy360-sso.md); this
 * module only remembers, in memory, what the backend said about it:
 *
 * - whether WorkEasy360 sign-in is on, and whether it is the only way in;
 * - whether this browser has a WorkEasy360 session;
 * - the CSRF token a cookie session must send back on every write (the CSRF
 *   cookie itself is httpOnly in production).
 */

const BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const SESSION_URL = `${BASE}/auth/workeasy360/session/`;

let state = {
    loaded: false,
    enabled: false,
    ssoOnly: false,
    signedIn: false,
    csrfToken: "",
    startUrl: "",
    logoutUrl: "",
    logoutWorkeasy360Url: "",
    messages: {},
};

/** Asks the backend; never throws, never redirects (the sign-in page calls it). */
export async function loadWorkeasy360Status() {
    try {
        const response = await fetch(SESSION_URL, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        state = {
            loaded: true,
            enabled: Boolean(data.enabled),
            ssoOnly: Boolean(data.sso_only),
            signedIn: Boolean(data.signed_in),
            csrfToken: data.csrf_token || "",
            startUrl: data.start_url || "",
            logoutUrl: data.logout_url || "",
            logoutWorkeasy360Url: data.logout_workeasy360_url || "",
            messages: data.messages || {},
        };
    } catch {
        state = { ...state, loaded: true, signedIn: false };
    }
    return state;
}

export function workeasy360State() {
    return state;
}

export function csrfToken() {
    return state.csrfToken;
}

/** Signed in at all: a password session (tokens) or a WorkEasy360 session (cookie). */
export function hasSession() {
    return Boolean(localStorage.getItem("access_token")) || state.signedIn;
}

/** Changes whenever who is signed in changes; what the plan-usage cache keys on. */
export function sessionKey() {
    return localStorage.getItem("access_token") || (state.signedIn ? "workeasy360-session" : null);
}

export function startUrl(next = "/") {
    if (!state.startUrl) return "";
    return `${state.startUrl}?${new URLSearchParams({ next })}`;
}

/** The connector's own vocabulary (workeasy360_sso.errors), as the sign-in page shows it. */
const MESSAGES = {
    cancelled: "Sign-in was cancelled. You can try again.",
    expired: "The sign-in took too long or was already used. Please try again.",
    unavailable: "WorkEasy360 sign-in is temporarily unavailable. Please try again shortly.",
    refused: "SendStack could not complete sign-in with WorkEasy360. Contact support.",
    rate_limited: "Too many sign-in attempts. Wait a minute and try again.",
    not_entitled: "Your organization does not have SendStack in WorkEasy360 yet.",
    no_seat: "You do not have a SendStack seat. Ask an administrator for one.",
    provider_membership_inactive: "You are not an active member of that organization in WorkEasy360.",
    organization_inactive: "That organization cannot be signed in to right now.",
    access_policy: "Your organization's sign-in policy refused this session. Open WorkEasy360 for details.",
    email_missing: "WorkEasy360 did not share an email address for this account.",
    email_unverified: "Verify your email address in WorkEasy360, then try again.",
    account_conflict:
        "An account with this email already exists here and is not linked to your WorkEasy360 account. Contact your administrator.",
    account_disabled: "Your SendStack account is disabled.",
    membership_inactive: "Your access to this organization has ended.",
    wrong_product: "The sign-in was issued for a different product.",
    wrong_organization: "The sign-in was for a different organization than the one requested.",
    session_ended: "Your WorkEasy360 session was ended. Please sign in again.",
};

export function ssoErrorMessage(code) {
    if (!code) return "";
    return state.messages[code] || MESSAGES[code] || MESSAGES.refused;
}

/**
 * Signs out with a real form POST, as the backend's sign-out routes expect:
 * they end the session on the server and redirect -- to the sign-in page, or
 * for "WorkEasy360 too" to WorkEasy360's own sign-out confirmation.
 */
export function submitSignOut(url) {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = url;
    const token = document.createElement("input");
    token.type = "hidden";
    token.name = "csrfmiddlewaretoken";
    token.value = state.csrfToken;
    form.appendChild(token);
    document.body.appendChild(form);
    state = { ...state, signedIn: false };
    form.submit();
}
