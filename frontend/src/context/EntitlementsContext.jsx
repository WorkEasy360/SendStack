import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import api from "../services/api";
import { useAuth } from "./AuthContext";

/**
 * What this workspace's plan allows, fetched once and shared.
 *
 * Every screen that wants to say "you have used 4,200 of 10,000 messages" or
 * to show a locked feature reads it from here rather than calling
 * `/billing/usage/` for itself -- otherwise the dashboard, the AI page and the
 * automation page would each fetch the same payload on every navigation.
 *
 * **This is presentation only.** It decides what is *shown*, never what is
 * allowed: the backend refuses the request regardless of what this thinks, and
 * a copy of the app with these checks edited out gets exactly the same 403.
 * See docs/ENTITLEMENTS.md.
 */

const EntitlementsContext = createContext(null);

//: Unknown means "not loaded yet", and an unknown feature reads as available.
//: Rendering an upgrade prompt at every feature for the half-second before the
//: response lands would be a worse lie than briefly showing a button the
//: server will refuse.
const OPTIMISTIC = {
    plan: null,
    subscription: null,
    usage: null,
    features: {},
    over_limit: [],
};

export function EntitlementsProvider({ children }) {
    const { user } = useAuth();
    const [entitlements, setEntitlements] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    //: The token this provider last fetched with. The access token is what
    //: /billing/usage/ actually authenticates with, so it -- not the resolved
    //: `user` -- is the thing worth keying on, and it is in localStorage
    //: before /auth/me/ has answered. See the effect below.
    const fetchedWithToken = useRef(null);

    const refresh = useCallback(async () => {
        if (!localStorage.getItem("access_token")) {
            fetchedWithToken.current = null;
            setEntitlements(null);
            setLoading(false);
            return;
        }
        try {
            const response = await api.get("/billing/usage/");
            setEntitlements(response.data);
            setError("");
        } catch {
            // Deliberately quiet. A usage panel that cannot load is a missing
            // panel, not a broken app, and this runs on every page.
            setEntitlements(null);
            setError("Your plan usage could not be loaded.");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        // This used to gate on `user`, which made a cold load serial: the app
        // waited for /auth/me/ purely to learn something localStorage already
        // knew, and only then asked for usage. Two round trips before the
        // shell could paint, where the second never depended on the first.
        //
        // Keying on the token instead is both earlier and more accurate.
        // Every event that should invalidate this -- signing in or out,
        // entering or leaving impersonation -- replaces the access token, and
        // `user` is still in the dependency list as the signal to come back
        // and look. When it changes but the token has not, the fetch is
        // skipped, so the cold-load pass costs one request rather than two.
        const token = localStorage.getItem("access_token");
        if (!token) {
            fetchedWithToken.current = null;
            setEntitlements(null);
            setLoading(false);
            return;
        }
        if (fetchedWithToken.current === token) return;
        fetchedWithToken.current = token;
        setLoading(true);
        refresh();
    }, [user, refresh]);

    const value = useMemo(() => {
        const data = entitlements || OPTIMISTIC;
        return {
            ...data,
            loading,
            error,
            refresh,
            planName: data.plan?.name || "",
            /** Unknown reads as available -- see OPTIMISTIC above. */
            hasFeature: (name) => data.features?.[name] !== false,
            /** One metric's block: `{used, limit, remaining, percentage, ...}`. */
            quota: (name) => data.usage?.[name] || null,
        };
    }, [entitlements, loading, error, refresh]);

    return <EntitlementsContext.Provider value={value}>{children}</EntitlementsContext.Provider>;
}

export function useEntitlements() {
    const context = useContext(EntitlementsContext);
    if (!context) {
        throw new Error("useEntitlements must be used inside an EntitlementsProvider");
    }
    return context;
}
