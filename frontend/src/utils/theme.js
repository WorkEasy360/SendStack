import { useSyncExternalStore } from "react";

/**
 * Light / dark / follow-the-system, for the signed-in app.
 *
 * The preference is the user's choice and is remembered. Whether the page is
 * actually dark is a separate question, because the `dark` class only goes on
 * <html> while the app shell is mounted (see `useAppTheme`): the public pages
 * -- landing, login, docs -- are designed light and stay light.
 *
 * Everything that has to *react* to the theme (the flow canvases, the brand
 * colour ramp) reads `useDarkMode()`, which is the class's real state rather
 * than the preference, so it can never disagree with what is painted.
 */

const STORAGE_KEY = "theme_preference";
const CHOICES = ["light", "dark", "system"];
const listeners = new Set();

let preference = readPreference();
let appMounted = false;
let dark = false;

function readPreference() {
    try {
        const stored = localStorage.getItem(STORAGE_KEY);
        return CHOICES.includes(stored) ? stored : "system";
    } catch {
        // Storage can be blocked (private windows); the system default is
        // the right fallback, not an error.
        return "system";
    }
}

const systemQuery =
    typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function apply() {
    const next = appMounted && (preference === "dark" || (preference === "system" && !!systemQuery?.matches));
    document.documentElement.classList.toggle("dark", next);

    // The browser chrome (mobile address bar) follows the page.
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", next ? "#0d1424" : "#0f172a");

    if (next !== dark) {
        dark = next;
        listeners.forEach((listener) => listener());
    }
}

function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
}

// "System" has to keep following the OS while the app is open.
systemQuery?.addEventListener?.("change", () => {
    if (preference === "system") apply();
});

/** Called by the app shell when it mounts / unmounts. */
export function setAppThemeActive(active) {
    appMounted = active;
    apply();
}

export function setThemePreference(next) {
    if (!CHOICES.includes(next)) return;
    preference = next;
    try {
        localStorage.setItem(STORAGE_KEY, next);
    } catch {
        // Not remembered, still applied for this session.
    }
    apply();
    // Preference changes must reach subscribers even when `dark` did not
    // flip (system -> dark while the OS is already dark).
    listeners.forEach((listener) => listener());
}

/** True only while the page is really painted dark. */
export function useDarkMode() {
    return useSyncExternalStore(subscribe, () => dark, () => false);
}

/** `[preference, setPreference]` -- "light" | "dark" | "system". */
export function useThemePreference() {
    const current = useSyncExternalStore(subscribe, () => preference, () => "system");
    return [current, setThemePreference];
}
