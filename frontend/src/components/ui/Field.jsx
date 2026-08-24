import { useEffect, useRef } from "react";
import { ChevronDown } from "lucide-react";

const base =
    "w-full rounded-lg border bg-white px-3 py-2.5 text-sm text-ink-900 placeholder:text-ink-400 transition-colors " +
    "focus:outline-none focus:ring-4 disabled:bg-ink-100 disabled:text-ink-500 disabled:cursor-not-allowed";

const normal = "border-ink-200 hover:border-ink-300 focus:border-brand-500 focus:ring-brand-500/15";
const invalid = "border-red-400 focus:border-red-500 focus:ring-red-500/15";

function control(hasError, className) {
    return `${base} ${hasError ? invalid : normal} ${className}`;
}

/**
 * Label + control + hint/error wrapper. `error` takes precedence over `hint`
 * so a field never shows contradictory help text.
 */
export function Field({ label, error, hint, required, children, className = "" }) {
    // `className` REPLACES the default spacing rather than being appended —
    // appending `mb-0` would lose to `mb-4` on CSS source order.
    return (
        <label className={`block ${className || "mb-4"}`}>
            {label && (
                <span className="block text-sm font-semibold text-ink-700 mb-1.5">
                    {label}
                    {required && <span className="text-red-500 ml-0.5">*</span>}
                </span>
            )}
            {children}
            {hint && !error && <span className="block text-xs text-ink-500 mt-1.5">{hint}</span>}
            {error && <span className="block text-xs font-medium text-red-600 mt-1.5">{error}</span>}
        </label>
    );
}

export function Input({ className = "", invalid: hasError = false, ...props }) {
    return <input aria-invalid={hasError || undefined} className={control(hasError, className)} {...props} />;
}

export function Textarea({ className = "", invalid: hasError = false, rows = 4, ...props }) {
    return (
        <textarea
            rows={rows}
            aria-invalid={hasError || undefined}
            className={`${control(hasError, className)} resize-y`}
            {...props}
        />
    );
}

export function Select({ className = "", invalid: hasError = false, children, ...props }) {
    return (
        <div className="relative">
            <select
                aria-invalid={hasError || undefined}
                className={`${control(hasError, className)} appearance-none pr-9 cursor-pointer`}
                {...props}
            >
                {children}
            </select>
            <ChevronDown
                size={16}
                aria-hidden="true"
                className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-ink-400"
            />
        </div>
    );
}

/**
 * Bare checkbox for selecting rows.
 *
 * `indeterminate` is a DOM property, not an attribute, so React cannot set it
 * through JSX — it has to be assigned on the node. That third state is what
 * makes "some of these rows are ticked" readable at a glance.
 */
export function Checkbox({ indeterminate = false, className = "", ...props }) {
    const ref = useRef(null);

    useEffect(() => {
        if (ref.current) ref.current.indeterminate = indeterminate;
    }, [indeterminate]);

    return (
        <input
            ref={ref}
            type="checkbox"
            className={`w-4 h-4 shrink-0 accent-brand-600 cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 ${className}`}
            {...props}
        />
    );
}

/** Styled file picker — native inputs cannot be restyled consistently. */
export function FileInput({ className = "", ...props }) {
    return (
        <input
            type="file"
            className={`w-full text-sm text-ink-600 file:mr-3 file:rounded-lg file:border-0 file:bg-ink-900 file:px-3.5 file:py-2 file:text-sm file:font-semibold file:text-white hover:file:bg-ink-800 file:cursor-pointer cursor-pointer ${className}`}
            {...props}
        />
    );
}

/** Checkbox / radio with an aligned label and optional description. */
export function CheckboxField({ type = "checkbox", label, description, className = "", ...props }) {
    return (
        <label className={`flex items-start gap-2.5 cursor-pointer select-none ${className}`}>
            <input
                type={type}
                className="mt-0.5 w-4 h-4 shrink-0 accent-brand-600 cursor-pointer"
                {...props}
            />
            <span className="min-w-0">
                <span className="block text-sm font-medium text-ink-800">{label}</span>
                {description && <span className="block text-xs text-ink-500 mt-0.5">{description}</span>}
            </span>
        </label>
    );
}

/** Pill-shaped multi-select chip, used for contact groups and tags. */
export function ChipToggle({ checked, onChange, children }) {
    return (
        <label
            className={`inline-flex items-center gap-1.5 text-sm font-medium rounded-full border px-3 py-1.5 cursor-pointer transition-colors ${
                checked
                    ? "border-brand-500 bg-brand-50 text-brand-700"
                    : "border-ink-200 text-ink-600 hover:border-ink-300 hover:bg-ink-50"
            }`}
        >
            <input type="checkbox" checked={checked} onChange={onChange} className="w-3.5 h-3.5 accent-brand-600 cursor-pointer" />
            {children}
        </label>
    );
}

/** On/off switch for boolean settings. */
export function Toggle({ checked, onChange, label, description, disabled = false }) {
    return (
        <label className={`flex items-start justify-between gap-4 ${disabled ? "opacity-60" : "cursor-pointer"}`}>
            <span className="min-w-0">
                <span className="block text-sm font-semibold text-ink-800">{label}</span>
                {description && <span className="block text-xs text-ink-500 mt-0.5">{description}</span>}
            </span>
            <span className="relative shrink-0">
                <input
                    type="checkbox"
                    role="switch"
                    checked={checked}
                    disabled={disabled}
                    onChange={onChange}
                    className="peer sr-only"
                />
                <span className="block w-10 h-6 rounded-full bg-ink-300 transition-colors peer-checked:bg-brand-600 peer-focus-visible:ring-4 peer-focus-visible:ring-brand-500/20" />
                <span className="absolute left-0.5 top-0.5 w-5 h-5 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4" />
            </span>
        </label>
    );
}
