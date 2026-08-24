import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";

const ToastContext = createContext(null);

const TONES = {
    success: { classes: "border-brand-200 bg-white", icon: CheckCircle2, iconColor: "text-brand-600" },
    error: { classes: "border-red-200 bg-white", icon: AlertCircle, iconColor: "text-red-500" },
    info: { classes: "border-ink-200 bg-white", icon: Info, iconColor: "text-blue-500" },
};

const DEFAULT_DURATION_MS = 4000;

export function ToastProvider({ children }) {
    const [toasts, setToasts] = useState([]);
    const timers = useRef(new Map());

    const dismiss = useCallback((id) => {
        setToasts((prev) => prev.filter((toast) => toast.id !== id));
        const timer = timers.current.get(id);
        if (timer) {
            clearTimeout(timer);
            timers.current.delete(id);
        }
    }, []);

    const push = useCallback(
        (message, { tone = "info", duration = DEFAULT_DURATION_MS } = {}) => {
            const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
            setToasts((prev) => [...prev, { id, message, tone }]);
            timers.current.set(
                id,
                setTimeout(() => dismiss(id), duration)
            );
            return id;
        },
        [dismiss]
    );

    const value = useMemo(
        () => ({
            toast: push,
            success: (message, options) => push(message, { ...options, tone: "success" }),
            error: (message, options) => push(message, { ...options, tone: "error" }),
            info: (message, options) => push(message, { ...options, tone: "info" }),
            dismiss,
        }),
        [push, dismiss]
    );

    return (
        <ToastContext.Provider value={value}>
            {children}
            <div
                className="fixed z-[60] bottom-4 right-4 left-4 sm:left-auto sm:w-96 flex flex-col gap-2 pointer-events-none"
                aria-live="polite"
                aria-atomic="false"
            >
                {toasts.map((toast) => {
                    const { classes, icon: Icon, iconColor } = TONES[toast.tone] || TONES.info;
                    return (
                        <div
                            key={toast.id}
                            className={`pointer-events-auto flex items-start gap-2.5 border rounded-xl shadow-raised px-3.5 py-3 text-sm text-ink-800 animate-slide-in-right ${classes}`}
                        >
                            <Icon size={17} className={`shrink-0 mt-0.5 ${iconColor}`} aria-hidden="true" />
                            <span className="flex-1 min-w-0">{toast.message}</span>
                            <button
                                type="button"
                                onClick={() => dismiss(toast.id)}
                                aria-label="Dismiss notification"
                                className="shrink-0 text-ink-400 hover:text-ink-700 cursor-pointer bg-transparent border-none p-0 transition-colors"
                            >
                                <X size={15} />
                            </button>
                        </div>
                    );
                })}
            </div>
        </ToastContext.Provider>
    );
}

export function useToast() {
    const context = useContext(ToastContext);
    if (!context) {
        throw new Error("useToast must be used within a ToastProvider");
    }
    return context;
}
