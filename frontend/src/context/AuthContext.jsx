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

    const logout = useCallback(() => {
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
            logout();
        }
    }, [loadCurrentUser, logout]);

    return (
        <AuthContext.Provider value={{ user, loading, login, logout, loginAs, impersonation, exitImpersonation, refresh: loadCurrentUser }}>
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
