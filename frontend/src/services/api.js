import axios from "axios";

const api = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
    headers: {
        "Content-Type": "application/json",
    },
});

api.interceptors.request.use((config) => {
    const token = localStorage.getItem("access_token");
    if (token) {
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

let refreshPromise = null;

async function refreshAccessToken() {
    const refreshToken = localStorage.getItem("refresh_token");
    if (!refreshToken) {
        throw new Error("No refresh token available");
    }

    const response = await axios.post(
        `${api.defaults.baseURL}/auth/token/refresh/`,
        { refresh: refreshToken }
    );
    localStorage.setItem("access_token", response.data.access);
    if (response.data.refresh) {
        localStorage.setItem("refresh_token", response.data.refresh);
    }
    return response.data.access;
}

api.interceptors.response.use(
    (response) => response,
    async (error) => {
        const { config, response } = error;

        if (!response) {
            return Promise.reject(error);
        }

        if (response.status === 401 && !config._retried && !config.url?.includes("/auth/token/")) {
            config._retried = true;
            try {
                refreshPromise = refreshPromise || refreshAccessToken();
                const newToken = await refreshPromise;
                refreshPromise = null;
                config.headers.Authorization = `Bearer ${newToken}`;
                return api(config);
            } catch (refreshError) {
                refreshPromise = null;
                localStorage.removeItem("access_token");
                localStorage.removeItem("refresh_token");
                if (window.location.pathname !== "/login") {
                    window.location.href = "/login";
                }
                return Promise.reject(refreshError);
            }
        }

        return Promise.reject(error);
    }
);

export function apiErrorMessage(error, fallback = "Something went wrong. Please try again.") {
    const data = error?.response?.data;
    if (!data) return fallback;
    if (typeof data.detail === "string") return data.detail;
    if (typeof data === "string") return data;
    const firstKey = Object.keys(data).find((key) => key !== "request_id");
    if (firstKey) {
        const value = data[firstKey];
        const text = Array.isArray(value) ? value.join(" ") : String(value);
        return firstKey === "non_field_errors" || firstKey === "detail" ? text : `${firstKey}: ${text}`;
    }
    return fallback;
}

export default api;
