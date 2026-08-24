import { Search, X } from "lucide-react";

/** Search box with a leading icon and a clear button once text is entered. */
export default function SearchInput({ value, onChange, onClear, placeholder = "Search...", className = "" }) {
    return (
        <div className={`relative ${className}`}>
            <Search
                size={16}
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
            />
            <input
                type="search"
                value={value}
                onChange={onChange}
                placeholder={placeholder}
                aria-label={placeholder}
                className="w-full rounded-lg border border-ink-200 bg-white pl-9 pr-9 py-2.5 text-sm text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-500/15 [&::-webkit-search-cancel-button]:hidden"
            />
            {value && onClear && (
                <button
                    type="button"
                    onClick={onClear}
                    aria-label="Clear search"
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 w-5 h-5 inline-flex items-center justify-center rounded text-ink-400 hover:text-ink-700 hover:bg-ink-100 cursor-pointer bg-transparent border-none transition-colors"
                >
                    <X size={14} />
                </button>
            )}
        </div>
    );
}
