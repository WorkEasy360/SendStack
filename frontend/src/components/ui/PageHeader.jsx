import { Link } from "react-router-dom";
import { ChevronLeft } from "lucide-react";

/**
 * Standard page title block: optional back link, title, subtitle and a
 * right-aligned actions slot that wraps below the title on mobile.
 */
export default function PageHeader({ title, description, actions, backTo, backLabel = "Back", className = "" }) {
    return (
        <div className={`mb-6 ${className}`}>
            {backTo && (
                <Link
                    to={backTo}
                    className="inline-flex items-center gap-1 text-sm font-medium text-ink-500 hover:text-ink-900 mb-2 no-underline transition-colors"
                >
                    <ChevronLeft size={15} aria-hidden="true" />
                    {backLabel}
                </Link>
            )}
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div className="min-w-0">
                    <h1 className="text-xl sm:text-2xl font-bold text-ink-900 m-0">{title}</h1>
                    {description && <p className="text-sm text-ink-500 mt-1 mb-0">{description}</p>}
                </div>
                {actions && <div className="flex flex-wrap items-center gap-2 shrink-0">{actions}</div>}
            </div>
        </div>
    );
}
