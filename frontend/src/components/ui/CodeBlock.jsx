import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { useToast } from "./Toast";

/**
 * A code panel with a copy button, and a small JSON highlighter.
 *
 * The highlighter is hand-rolled rather than pulled from a library on purpose.
 * Everything this app shows in a code block is JSON, a curl command or an HTTP
 * header line -- three grammars, all tiny -- and a syntax-highlighting package
 * is 100-300KB in the bundle to colour them. It tokenises with one regex over
 * escaped text, so it cannot inject markup even if a template body contains
 * `<script>`: `escapeHtml` runs first and the regex only ever wraps its own
 * output in spans.
 *
 * `copy` is the reason this is a component at all. The brief asks for a copy
 * button on every JSON and curl example, and a per-page implementation is a
 * per-page chance to forget the clipboard-refused fallback -- which is not
 * hypothetical: `navigator.clipboard` throws over plain HTTP, which is exactly
 * how a self-hosted deployment is first tried.
 */

const TOKEN = /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|(\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|(\b(?:true|false|null)\b)/g;

const CLASSES = {
    key: "text-sky-300",
    string: "text-emerald-300",
    number: "text-amber-300",
    literal: "text-violet-300",
};

function escapeHtml(value) {
    return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function highlightJson(text) {
    return escapeHtml(text).replace(TOKEN, (match, key, string, number, literal) => {
        if (key) return `<span class="${CLASSES.key}">${key}</span>`;
        if (string) return `<span class="${CLASSES.string}">${string}</span>`;
        if (number) return `<span class="${CLASSES.number}">${number}</span>`;
        if (literal) return `<span class="${CLASSES.literal}">${literal}</span>`;
        return match;
    });
}

/** `value` may be a string or an object; objects are pretty-printed as JSON. */
export function toText(value) {
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
}

export default function CodeBlock({
    value,
    language = "json",
    label,
    actions,
    copyable = true,
    maxHeight = "24rem",
    className = "",
}) {
    const text = toText(value);
    const html = language === "json" ? highlightJson(text) : escapeHtml(text);

    return (
        <div className={`theme-static rounded-lg overflow-hidden border border-ink-800 bg-ink-900 ${className}`}>
            {(label || copyable || actions) && (
                <div className="flex items-center justify-between gap-2 px-3 py-1.5 bg-ink-800/60 border-b border-ink-800">
                    <span className="text-xs font-semibold text-ink-300 uppercase tracking-wide truncate">
                        {label || language}
                    </span>
                    <div className="flex items-center gap-1 shrink-0">
                        {actions}
                        {copyable && <CopyButton value={text} />}
                    </div>
                </div>
            )}
            <pre
                className="m-0 p-3.5 text-xs leading-relaxed overflow-auto text-ink-100 font-mono"
                style={{ maxHeight }}
            >
                <code dangerouslySetInnerHTML={{ __html: html }} />
            </pre>
        </div>
    );
}

/**
 * Copies `value`, and says so.
 *
 * The catch branch is not a nicety. `navigator.clipboard` is undefined on a
 * plain-HTTP origin and rejects when the document is not focused, both of
 * which happen on a self-hosted deployment before TLS is set up. Falling back
 * to `document.execCommand` keeps the button working there rather than failing
 * silently, which is how somebody concludes the whole page is broken.
 */
export function CopyButton({ value, label = "Copy", copiedLabel = "Copied", size = "xs", className = "" }) {
    const [copied, setCopied] = useState(false);
    const toast = useToast();

    const copy = async () => {
        const text = toText(value);
        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                const field = document.createElement("textarea");
                field.value = text;
                field.setAttribute("readonly", "");
                field.style.position = "fixed";
                field.style.opacity = "0";
                document.body.appendChild(field);
                field.select();
                document.execCommand("copy");
                document.body.removeChild(field);
            }
            setCopied(true);
            setTimeout(() => setCopied(false), 1800);
        } catch {
            toast.error("Could not copy — select the text and copy it by hand.");
        }
    };

    const padding = size === "xs" ? "px-2 py-1 text-xs gap-1" : "px-2.5 py-1.5 text-sm gap-1.5";

    return (
        <button
            type="button"
            onClick={copy}
            className={`inline-flex items-center rounded-md font-semibold cursor-pointer transition-colors border-0 ${padding} ${
                copied
                    ? "bg-brand-600 text-white"
                    : "bg-ink-700 text-ink-100 hover:bg-ink-600"
            } ${className}`}
        >
            {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
            {copied ? copiedLabel : label}
        </button>
    );
}

/** An HTTP method chip. Method colour is the fastest way to scan an endpoint list. */
const METHOD_TONE = {
    GET: "bg-blue-100 text-blue-800 ring-blue-600/20",
    POST: "bg-brand-100 text-brand-800 ring-brand-600/20",
    PUT: "bg-amber-100 text-amber-900 ring-amber-600/20",
    PATCH: "bg-amber-100 text-amber-900 ring-amber-600/20",
    DELETE: "bg-red-100 text-red-800 ring-red-600/20",
};

export function MethodBadge({ method, className = "" }) {
    return (
        <span
            className={`inline-flex items-center px-1.5 py-0.5 rounded font-mono text-[11px] font-bold ring-1 ring-inset ${
                METHOD_TONE[method] || "bg-ink-100 text-ink-700 ring-ink-500/20"
            } ${className}`}
        >
            {method}
        </span>
    );
}

/**
 * A read-only value with a copy button beside it -- the base URL, an endpoint
 * URL, a header line. Distinct from CodeBlock because these are one line and
 * belong inline in a form rather than in a dark panel.
 */
export function CopyField({ value, label, hint, mono = true, className = "" }) {
    return (
        <div className={className}>
            {label && <div className="text-sm font-semibold text-ink-800 mb-1.5">{label}</div>}
            <div className="flex items-stretch gap-2">
                <div
                    className={`flex-1 min-w-0 px-3 py-2 bg-ink-50 border border-ink-200 rounded-lg text-sm text-ink-800 overflow-x-auto whitespace-nowrap ${
                        mono ? "font-mono" : ""
                    }`}
                >
                    {value}
                </div>
                <CopyButton
                    value={value}
                    size="sm"
                    className="shrink-0 !bg-surface !text-ink-700 border border-ink-200 hover:!bg-ink-50"
                />
            </div>
            {hint && <p className="text-xs text-ink-500 mt-1.5 mb-0">{hint}</p>}
        </div>
    );
}
