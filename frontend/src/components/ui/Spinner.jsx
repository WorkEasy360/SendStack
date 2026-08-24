const SIZES = {
    xs: "w-3.5 h-3.5 border-2",
    sm: "w-5 h-5 border-2",
    md: "w-8 h-8 border-[3px]",
};

/**
 * `inline` drops the centering wrapper and draws the arc in `currentColor`,
 * so the spinner picks up whatever text colour its button already has.
 */
export default function Spinner({ size = "md", inline = false, label = "Loading", className = "" }) {
    if (inline) {
        return (
            <span
                role="status"
                aria-label={label}
                className={`inline-block rounded-full border-transparent border-t-current animate-spin ${SIZES[size]} ${className}`}
            />
        );
    }

    return (
        <div className={`flex items-center justify-center py-16 ${className}`}>
            <span
                role="status"
                aria-label={label}
                className={`inline-block rounded-full border-ink-200 border-t-brand-600 animate-spin ${SIZES[size]}`}
            />
        </div>
    );
}
