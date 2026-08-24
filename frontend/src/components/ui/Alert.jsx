import { AlertCircle, CheckCircle2, Info, TriangleAlert } from "lucide-react";

const TONES = {
    error: { classes: "bg-red-50 text-red-800 border-red-200", icon: AlertCircle, iconColor: "text-red-500" },
    success: { classes: "bg-brand-50 text-brand-800 border-brand-200", icon: CheckCircle2, iconColor: "text-brand-600" },
    info: { classes: "bg-blue-50 text-blue-800 border-blue-200", icon: Info, iconColor: "text-blue-500" },
    warning: { classes: "bg-amber-50 text-amber-900 border-amber-200", icon: TriangleAlert, iconColor: "text-amber-500" },
};

/**
 * Renders nothing when `children` is empty, so pages can keep using
 * `<Alert>{error}</Alert>` with an empty-string error state.
 */
export default function Alert({ tone = "error", title, children, className = "" }) {
    if (!children && !title) return null;
    const { classes, icon: Icon, iconColor } = TONES[tone] || TONES.error;
    // Tailwind emits `mb-0` before `mb-4`, so an appended override would lose
    // on source order -- drop the default whenever the caller sets its own.
    const spacing = className.includes("mb-") ? "" : "mb-4";

    return (
        <div
            role={tone === "error" ? "alert" : "status"}
            className={`flex items-start gap-2.5 border rounded-lg px-3.5 py-3 text-sm ${spacing} ${classes} ${className}`}
        >
            <Icon size={17} className={`shrink-0 mt-0.5 ${iconColor}`} aria-hidden="true" />
            <div className="min-w-0">
                {title && <p className="font-semibold m-0 mb-0.5">{title}</p>}
                {children && <div className="min-w-0">{children}</div>}
            </div>
        </div>
    );
}
