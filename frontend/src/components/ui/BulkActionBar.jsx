import { X } from "lucide-react";
import Button from "./Button";

/**
 * The strip that appears above a list once rows are ticked: how many are
 * selected, what can be done to them, and a way out.
 *
 * Sticky, because a bulk action is chosen after scrolling through a long list
 * and the controls have to still be reachable when you get there. Actions wrap
 * onto their own line on narrow screens rather than being pushed off the edge.
 */
export default function BulkActionBar({ count, label = "item", onClear, children, className = "" }) {
    if (!count) return null;

    const noun = count === 1 ? label : `${label}s`;
    // Tailwind emits `mb-0` before `mb-3`, so an appended override would lose on
    // source order -- drop the default whenever the caller sets its own, the
    // same way Alert does.
    const spacing = className.includes("mb-") ? "" : "mb-3";
    const position = className.includes("static") ? "" : "sticky top-2 z-20";

    return (
        <div
            role="region"
            aria-label="Bulk actions"
            className={`${position} ${spacing} flex flex-wrap items-center gap-x-3 gap-y-2 rounded-card border border-brand-200 bg-brand-50 px-3 py-2.5 shadow-card ${className}`}
        >
            <span className="text-sm font-semibold text-brand-800 tabular-nums" aria-live="polite">
                {count} {noun} selected
            </span>

            <div className="flex flex-wrap items-center gap-2 sm:ml-auto">{children}</div>

            <Button
                variant="ghost"
                size="sm"
                icon={X}
                onClick={onClear}
                className="text-brand-800 hover:bg-brand-100"
            >
                Clear
            </Button>
        </div>
    );
}

/** A bulk action button — danger tone for anything destructive. */
export function BulkAction({ tone = "secondary", ...props }) {
    return <Button variant={tone} size="sm" {...props} />;
}
