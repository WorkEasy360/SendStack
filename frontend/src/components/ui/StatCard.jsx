import { Link } from "react-router-dom";

const TONES = {
    default: "bg-ink-100 text-ink-500",
    brand: "bg-brand-50 text-brand-600",
    blue: "bg-blue-50 text-blue-600",
    amber: "bg-amber-50 text-amber-600",
    red: "bg-red-50 text-red-600",
    violet: "bg-violet-50 text-violet-600",
};

function formatValue(value) {
    if (typeof value === "number") return value.toLocaleString();
    return value ?? "—";
}

/**
 * Dashboard metric tile. Pass `to` to make the whole card a link to the
 * page that owns the underlying data.
 */
export default function StatCard({ label, value, hint, icon: Icon, tone = "default", to }) {
    const body = (
        <>
            <div className="flex items-start justify-between gap-3">
                <p className="text-sm font-medium text-ink-500 m-0">{label}</p>
                {Icon && (
                    <span className={`shrink-0 w-8 h-8 rounded-lg flex items-center justify-center ${TONES[tone]}`}>
                        <Icon size={16} strokeWidth={2} aria-hidden="true" />
                    </span>
                )}
            </div>
            <p className="text-2xl font-bold text-ink-900 mt-2 mb-0 tabular-nums">{formatValue(value)}</p>
            {hint && <p className="text-xs text-ink-500 mt-1 mb-0">{hint}</p>}
        </>
    );

    const shell = "bg-surface border border-ink-200 rounded-card shadow-card p-5 block no-underline";

    if (to) {
        return (
            <Link to={to} className={`${shell} transition-colors hover:border-ink-300 hover:bg-ink-50/40`}>
                {body}
            </Link>
        );
    }

    return <div className={shell}>{body}</div>;
}
