/**
 * `variant="plain"` drops the dashed card shell, for use inside a surface
 * that already has its own border (e.g. the inbox thread pane).
 */
export default function EmptyState({ icon: Icon, title, description, action, variant = "card", className = "" }) {
    const shell =
        variant === "plain"
            ? "py-10 px-6 text-center"
            : "bg-white border border-dashed border-ink-300 rounded-card py-14 px-6 text-center";

    return (
        <div className={`${shell} ${className}`}>
            {Icon && (
                <div className="w-12 h-12 mx-auto mb-4 rounded-xl bg-ink-100 text-ink-400 flex items-center justify-center">
                    <Icon size={22} strokeWidth={1.75} aria-hidden="true" />
                </div>
            )}
            <h3 className="text-base font-bold text-ink-900 m-0 mb-1">{title}</h3>
            {description && <p className="text-sm text-ink-500 max-w-sm mx-auto mt-0 mb-5">{description}</p>}
            {action}
        </div>
    );
}
