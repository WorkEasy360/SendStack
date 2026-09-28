import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import axios from "axios";
import Spinner from "../../components/ui/Spinner";
import { LogoMark } from "../../components/layout/Logo";

// Unauthenticated. These pages have to render for somebody who has no account
// and is deciding whether to create one -- a privacy policy behind a login is
// not a privacy policy. The shared `api` instance would attach a token and
// bounce a 401 to /login, so this uses its own client.
const client = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
});

/**
 * Render one legal document, fetched from the API.
 *
 * The text is served by the backend rather than duplicated here, so there is
 * one copy of the wording and the version stamped on an acceptance record is
 * the version that was actually displayed.
 */
function LegalDocument({ slug }) {
    const [document, setDocument] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        let cancelled = false;
        setDocument(null);
        setError("");
        client
            .get(`/legal/documents/${slug}/`)
            .then((response) => {
                if (!cancelled) setDocument(response.data);
            })
            .catch(() => {
                if (!cancelled) setError("This document could not be loaded. Please try again.");
            });
        return () => {
            cancelled = true;
        };
    }, [slug]);

    return (
        <div className="min-h-screen bg-ink-50">
            <header className="bg-surface border-b border-ink-200">
                <div className="max-w-3xl mx-auto px-5 py-4 flex items-center justify-between">
                    <Link to="/" className="flex items-center gap-2.5 text-ink-900 no-underline">
                        <LogoMark size={28} />
                        <span className="font-semibold">{document?.title || "Legal"}</span>
                    </Link>
                    <Link
                        to="/login"
                        className="flex items-center gap-1.5 text-sm text-ink-500 hover:text-ink-700 no-underline"
                    >
                        <ArrowLeft size={14} aria-hidden="true" />
                        Sign in
                    </Link>
                </div>
            </header>

            <main className="max-w-3xl mx-auto px-5 py-8">
                {error ? <p className="text-sm text-red-600">{error}</p> : null}

                {!document && !error ? (
                    <div className="flex justify-center py-16">
                        <Spinner />
                    </div>
                ) : null}

                {document ? (
                    <>
                        {document.is_draft ? (
                            // Loud, and not dismissible. A document with
                            // [TO BE COMPLETED] in it that looks finished is
                            // worse than no document, because somebody will
                            // rely on it.
                            <div className="flex gap-3 items-start bg-amber-50 border border-amber-200 rounded-card p-4 mb-6">
                                <AlertTriangle size={18} className="text-amber-600 shrink-0 mt-0.5" aria-hidden="true" />
                                <div className="text-sm text-amber-900">
                                    <p className="font-semibold m-0 mb-1">Draft — not legally reviewed</p>
                                    <p className="m-0">
                                        This document still contains placeholders and has not been reviewed by a
                                        qualified lawyer. It describes how the service works, but it is not yet a
                                        binding agreement.
                                    </p>
                                </div>
                            </div>
                        ) : null}

                        <div className="flex items-baseline justify-between mb-6 pb-4 border-b border-ink-200">
                            <span className="text-xs font-medium text-ink-500 uppercase tracking-wide">
                                Version {document.version}
                            </span>
                            {document.updated ? (
                                <span className="text-xs text-ink-400">Last updated {document.updated}</span>
                            ) : null}
                        </div>

                        <article className="legal-body">{renderMarkdown(document.body)}</article>

                        <nav className="mt-10 pt-5 border-t border-ink-200 flex gap-4 text-sm">
                            <Link to="/terms" className="text-emerald-600 hover:text-emerald-700">
                                Terms of Service
                            </Link>
                            <Link to="/privacy" className="text-emerald-600 hover:text-emerald-700">
                                Privacy Policy
                            </Link>
                            <Link to="/dpa" className="text-emerald-600 hover:text-emerald-700">
                                Data Processing Agreement
                            </Link>
                        </nav>
                    </>
                ) : null}
            </main>
        </div>
    );
}

/**
 * The small subset of Markdown these documents use, rendered as React elements.
 *
 * Elements, not `dangerouslySetInnerHTML`. The text is ours and the risk today
 * is nil, but a legal page is exactly the kind of thing that later becomes
 * editable from an admin screen, and at that moment an HTML sink here would be
 * stored XSS on an unauthenticated page. Building elements costs a few lines
 * now and cannot become that.
 */
function renderMarkdown(markdown) {
    const lines = (markdown || "").split("\n");
    const nodes = [];
    let list = [];
    let table = [];

    const flushList = () => {
        if (!list.length) return;
        nodes.push(
            <ul key={`ul-${nodes.length}`} className="list-disc pl-5 my-3 space-y-1.5 text-[15px] text-ink-700">
                {list.map((item, index) => (
                    <li key={index}>{inline(item)}</li>
                ))}
            </ul>
        );
        list = [];
    };

    const flushTable = () => {
        if (!table.length) return;
        // A markdown table's second row is the |---|---| separator, which is
        // formatting rather than content.
        const [header, ...rest] = table;
        const body = rest.filter((row) => !/^[\s|:-]+$/.test(row));
        const cells = (row) =>
            row.split("|").slice(1, -1).map((cell) => cell.trim());
        nodes.push(
            <div key={`table-${nodes.length}`} className="my-4 overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr>
                            {cells(header).map((cell, index) => (
                                <th
                                    key={index}
                                    className="text-left font-semibold text-ink-900 border-b border-ink-300 px-3 py-2"
                                >
                                    {inline(cell)}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {body.map((row, rowIndex) => (
                            <tr key={rowIndex}>
                                {cells(row).map((cell, index) => (
                                    <td key={index} className="border-b border-ink-100 px-3 py-2 text-ink-700 align-top">
                                        {inline(cell)}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        );
        table = [];
    };

    const flushAll = () => {
        flushList();
        flushTable();
    };

    lines.forEach((raw, index) => {
        const line = raw.trimEnd();

        if (line.startsWith("|")) {
            flushList();
            table.push(line);
            return;
        }
        flushTable();

        if (!line.trim()) {
            flushList();
            return;
        }
        if (line.startsWith("- ")) {
            list.push(line.slice(2));
            return;
        }
        flushList();

        if (line.startsWith("> ")) {
            nodes.push(
                <blockquote
                    key={index}
                    className="border-l-4 border-amber-300 bg-amber-50 pl-4 py-2 my-4 text-sm text-amber-900"
                >
                    {inline(line.slice(2))}
                </blockquote>
            );
        } else if (line.startsWith(">")) {
            nodes.push(
                <blockquote
                    key={index}
                    className="border-l-4 border-amber-300 bg-amber-50 pl-4 py-1 text-sm text-amber-900"
                >
                    {inline(line.slice(1))}
                </blockquote>
            );
        } else if (line.startsWith("### ")) {
            nodes.push(
                <h3 key={index} className="text-base font-semibold text-ink-900 mt-6 mb-2">
                    {inline(line.slice(4))}
                </h3>
            );
        } else if (line.startsWith("## ")) {
            nodes.push(
                <h2 key={index} className="text-lg font-bold text-ink-900 mt-8 mb-3">
                    {inline(line.slice(3))}
                </h2>
            );
        } else if (line.startsWith("# ")) {
            nodes.push(
                <h1 key={index} className="text-2xl font-bold text-ink-900 mb-4">
                    {inline(line.slice(2))}
                </h1>
            );
        } else if (line.trim() === "---") {
            nodes.push(<hr key={index} className="my-6 border-ink-200" />);
        } else {
            nodes.push(
                <p key={index} className="text-[15px] leading-relaxed text-ink-700 my-3">
                    {inline(line)}
                </p>
            );
        }
    });

    flushAll();
    return nodes;
}

/** Bold, code spans and links, as elements. */
function inline(text) {
    const parts = [];
    // One pass over three alternatives, so the segments never overlap.
    const pattern = /(\*\*[^*]+\*\*)|(`[^`]+`)|(\[[^\]]+\]\([^)]+\))/g;
    let last = 0;
    let match;

    while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) parts.push(text.slice(last, match.index));
        const token = match[0];
        if (token.startsWith("**")) {
            parts.push(
                <strong key={parts.length} className="font-semibold text-ink-900">
                    {token.slice(2, -2)}
                </strong>
            );
        } else if (token.startsWith("`")) {
            parts.push(
                <code
                    key={parts.length}
                    className="bg-amber-50 text-amber-900 border border-amber-200 rounded px-1 py-0.5 text-[13px]"
                >
                    {token.slice(1, -1)}
                </code>
            );
        } else {
            const label = token.slice(1, token.indexOf("]"));
            const href = token.slice(token.indexOf("(") + 1, -1);
            // Only in-app paths become links. An href from the document text
            // is not a place to accept "javascript:" or an arbitrary origin.
            parts.push(
                href.startsWith("/") ? (
                    <Link key={parts.length} to={href} className="text-emerald-600 hover:text-emerald-700">
                        {label}
                    </Link>
                ) : (
                    <span key={parts.length}>{label}</span>
                )
            );
        }
        last = match.index + token.length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
}

export default LegalDocument;
