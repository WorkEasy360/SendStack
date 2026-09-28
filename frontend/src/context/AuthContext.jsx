import { createContext, useCallback, useContext, useEffect, useState } from "react";
import api, { apiErrorMessage } from "../services/api";

const AuthContext = createContext(null);

function readImpersonation() {
    const raw = localStorage.getItem("impersonation_meta");
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

export function AuthProvider({ children }) {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);
    const [impersonation, setImpersonation] = useState(readImpersonation);

    const loadCurrentUser = useCallback(async () => {
        const token = localStorage.getItem("access_token");
        if (!token) {
            setUser(null);
            setLoading(false);
            return;
        }
        try {
            const response = await api.get("/auth/me/");
            setUser(response.data.data);
        } catch {
            localStorage.removeItem("access_token");
            localStorage.removeItem("refresh_token");
            localStorage.removeItem("impersonation_original_access");
            localStorage.removeItem("impersonation_original_refresh");
            localStorage.removeItem("impersonation_meta");
            setUser(null);
            setImpersonation(null);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadCurrentUser();
    }, [loadCurrentUser]);

    const login = useCallback(async (username, password) => {
        try {
            const response = await api.post("/auth/token/", { username, password });
            localStorage.setItem("access_token", response.data.access);
            localStorage.setItem("refresh_token", response.data.refresh);
            localStorage.removeItem("impersonation_original_access");
            localStorage.removeItem("impersonation_original_refresh");
            localStorage.removeItem("impersonation_meta");
            setImpersonation(null);
            setUser(response.data.user);
            return { success: true };
        } catch (error) {
            return { success: false, message: apiErrorMessage(error, "Invalid username or password.") };
        }
    }, []);

    const signup = useCallback(async (details) => {
        // The response is a session, so a new customer lands in the product
        // rather than on a "now go and sign in" page. `next` comes from the
        // server, because the server is the only thing that knows whether a
        // trial actually started -- see apps/accounts/signup_views.py.
        try {
            const response = await api.post("/auth/signup/", details);
            localStorage.setItem("access_token", response.data.access);
            localStorage.setItem("refresh_token", response.data.refresh);
            localStorage.removeItem("impersonation_original_access");
            localStorage.removeItem("impersonation_original_refresh");
            localStorage.removeItem("impersonation_meta");
            setImpersonation(null);
            setUser(response.data.user);
            return {
                success: true,
                next: response.data.next || "/",
                subscription: response.data.subscription,
                notice: response.data.notice || "",
            };
        } catch (error) {
            return {
                success: false,
                // The backend's own sentence. It is deliberately the same for
                // "this address is taken" and "these details are not usable",
                // so the frontend must not try to be more specific than it.
                message: apiErrorMessage(error, "We could not create your account. Please try again."),
                code: error?.response?.data?.code || "",
                fields: error?.response?.status === 400 ? error?.response?.data : null,
            };
        }
    }, []);

    const logout = useCallback(async () => {
        // Tell the server first, then forget locally. Clearing localStorage on
        // its own left the refresh token valid for its full lifetime, so
        // signing out of a shared or lost machine protected nobody -- anyone
        // holding a copy of that token could keep minting access tokens for a
        // week. /auth/logout/ blacklists it.
        //
        // The local clear happens whatever the request does. A logout that
        // fails because the network is down or the token had already expired
        // must still get the user out of the session in front of them.
        const refreshToken = localStorage.getItem("refresh_token");
        if (refreshToken) {
            try {
                await api.post("/auth/logout/", { refresh: refreshToken });
            } catch {
                // Already expired, already blacklisted, or unreachable.
            }
        }
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
        localStorage.removeItem("impersonation_original_access");
        localStorage.removeItem("impersonation_original_refresh");
        localStorage.removeItem("impersonation_meta");
        setImpersonation(null);
        setUser(null);
    }, []);

    const loginAs = useCallback(async (targetUser, reason) => {
        try {
            const response = await api.post("/auth/impersonations/", { user_id: targetUser.id, reason });
            const currentAccess = localStorage.getItem("access_token");
            const currentRefresh = localStorage.getItem("refresh_token");
            localStorage.setItem("impersonation_original_access", currentAccess);
            localStorage.setItem("impersonation_original_refresh", currentRefresh);
            localStorage.setItem("access_token", response.data.access);
            localStorage.setItem("refresh_token", response.data.refresh);
            const meta = {
                sessionId: response.data.impersonation.session_id,
                originalUsername: response.data.impersonation.original_user,
                actingAsUsername: response.data.impersonation.acting_as.username,
            };
            localStorage.setItem("impersonation_meta", JSON.stringify(meta));
            setImpersonation(meta);
            await loadCurrentUser();
            return { success: true };
        } catch (error) {
            return { success: false, message: apiErrorMessage(error, "Unable to log in as this account.") };
        }
    }, [loadCurrentUser]);

    const exitImpersonation = useCallback(async () => {
        try {
            await api.post("/auth/impersonations/exit/");
        } catch {
            // Session may already be expired server-side; still restore the original session locally.
        }
        const originalAccess = localStorage.getItem("impersonation_original_access");
        const originalRefresh = localStorage.getItem("impersonation_original_refresh");
        localStorage.removeItem("impersonation_original_access");
        localStorage.removeItem("impersonation_original_refresh");
        localStorage.removeItem("impersonation_meta");
        setImpersonation(null);
        if (originalAccess && originalRefresh) {
            localStorage.setItem("access_token", originalAccess);
            localStorage.setItem("refresh_token", originalRefresh);
            await loadCurrentUser();
        } else {
            await logout();
        }
    }, [loadCurrentUser, logout]);

    return (
        <AuthContext.Provider value={{ user, loading, login, signup, logout, loginAs, impersonation, exitImpersonation, refresh: loadCurrentUser }}>
            {children}
        </AuthContext.Provider>
    );
}

export function useAuth() {
    const context = useContext(AuthContext);
    if (!context) {
        throw new Error("useAuth must be used within an AuthProvider");
    }
    return context;
}
