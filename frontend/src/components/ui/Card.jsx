export default function Card({ className = "", padded = true, children, ...props }) {
    return (
        <div
            className={`bg-surface border border-ink-200 rounded-card shadow-card ${padded ? "p-5 sm:p-6" : ""} ${className}`}
            {...props}
        >
            {children}
        </div>
    );
}

/** Title row for a card, with optional description and right-aligned action. */
export function CardHeader({ title, description, action, className = "" }) {
    return (
        <div className={`flex items-start justify-between gap-4 ${className}`}>
            <div className="min-w-0">
                <h2 className="text-base font-bold text-ink-900 m-0">{title}</h2>
                {description && <p className="text-sm text-ink-500 mt-1 mb-0">{description}</p>}
            </div>
            {action && <div className="shrink-0">{action}</div>}
        </div>
    );
}

/** A labelled section inside a settings-style page. */
export function CardSection({ title, description, children, className = "" }) {
    return (
        <section className={className}>
            <div className="mb-4">
                <h2 className="text-sm font-bold text-ink-900 uppercase tracking-wide m-0">{title}</h2>
                {description && <p className="text-sm text-ink-500 mt-1 mb-0">{description}</p>}
            </div>
            {children}
        </section>
    );
}
