import Spinner from "./Spinner";

const VARIANTS = {
    primary:
        "bg-brand-600 text-white border border-transparent hover:bg-brand-700 active:bg-brand-800 disabled:bg-brand-300",
    secondary:
        "bg-white text-ink-700 border border-ink-200 shadow-card hover:bg-ink-50 hover:border-ink-300 active:bg-ink-100 disabled:opacity-50",
    danger: "bg-red-600 text-white border border-transparent hover:bg-red-700 active:bg-red-800 disabled:bg-red-300",
    success:
        "bg-brand-600 text-white border border-transparent hover:bg-brand-700 active:bg-brand-800 disabled:bg-brand-300",
    ghost: "bg-transparent text-ink-600 border border-transparent hover:bg-ink-100 hover:text-ink-900 active:bg-ink-200 disabled:opacity-50",
    dark: "bg-ink-900 text-white border border-transparent hover:bg-ink-800 active:bg-ink-700 disabled:bg-ink-400",
};

const SIZES = {
    xs: "px-2.5 py-1 text-xs gap-1.5 rounded-md",
    sm: "px-3 py-1.5 text-sm gap-1.5 rounded-lg",
    md: "px-4 py-2.5 text-sm gap-2 rounded-lg",
    lg: "px-5 py-3 text-base gap-2 rounded-xl",
};

/**
 * `icon` renders a leading lucide icon component; `loading` swaps it for a
 * spinner and disables the button so double submits are impossible.
 */
export default function Button({
    variant = "primary",
    size = "md",
    icon: Icon,
    loading = false,
    disabled = false,
    className = "",
    children,
    ...props
}) {
    return (
        <button
            disabled={disabled || loading}
            className={`inline-flex items-center justify-center font-semibold whitespace-nowrap cursor-pointer transition-all duration-150 disabled:cursor-not-allowed ${VARIANTS[variant]} ${SIZES[size]} ${className}`}
            {...props}
        >
            {loading ? (
                <Spinner size="xs" inline />
            ) : (
                Icon && <Icon size={size === "lg" ? 18 : 16} strokeWidth={2.25} aria-hidden="true" />
            )}
            {children}
        </button>
    );
}

/** Compact icon-only button — always give it an `aria-label` or `title`. */
export function IconButton({ icon: Icon, variant = "ghost", className = "", ...props }) {
    return (
        <button
            className={`inline-flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
            {...props}
        >
            <Icon size={17} strokeWidth={2} aria-hidden="true" />
        </button>
    );
}
