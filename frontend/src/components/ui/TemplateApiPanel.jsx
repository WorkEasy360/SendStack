import { useEffect, useState } from "react";
import { ExternalLink, Terminal } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "./Alert";
import Badge from "./Badge";
import CodeBlock, { CopyButton, CopyField, MethodBadge, toText } from "./CodeBlock";
import Spinner from "./Spinner";

/**
 * "How do I send this template from my own software?" -- answered for one
 * template, with the answer generated on the server.
 *
 * Rendered in two places: the Templates page (per template, from its card) and
 * the Developer -> Templates tab (all of them, in a list). Both mount this same
 * component, so a customer who finds it from either direction reads identical
 * JSON.
 *
 * **Nothing here is composed in the browser.** The request body, the variable
 * list, the curl command and the caveats all arrive from
 * `/developer/api/templates/<id>`, which builds them from the template row and
 * the send serializer. A frontend that assembled the JSON itself would be a
 * second definition of the API contract, and the first one to drift.
 */
export default function TemplateApiPanel({ templateId, described = null }) {
    const [data, setData] = useState(described);
    const [error, setError] = useState("");

    useEffect(() => {
        if (described) {
            setData(described);
            return;
        }
        let cancelled = false;
        setData(null);
        setError("");
        api.get(`/developer/api/templates/${templateId}`)
            .then((response) => !cancelled && setData(response.data))
            .catch((err) => !cancelled && setError(apiErrorMessage(err, "Unable to build the API example.")));
        return () => {
            cancelled = true;
        };
    }, [templateId, described]);

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!data) return <Spinner />;

    return (
        <div className="space-y-5">
            <Endpoint data={data} />
            <Variables rows={data.variables} />

            <div>
                <SectionLabel>Request body</SectionLabel>
                <CodeBlock
                    value={data.request_body}
                    label="JSON"
                    actions={
                        <CopyButton
                            value={data.curl}
                            label="Copy cURL"
                            copiedLabel="cURL copied"
                            className="!bg-ink-700"
                        />
                    }
                />
            </div>

            <details className="group">
                <summary className="text-sm font-semibold text-ink-700 cursor-pointer inline-flex items-center gap-1.5">
                    <Terminal size={14} aria-hidden="true" />
                    Full cURL command
                </summary>
                <CodeBlock value={data.curl} language="bash" label="bash" className="mt-2.5" />
            </details>

            <div>
                <SectionLabel>Successful response</SectionLabel>
                <CodeBlock value={data.response_example} label="202 Accepted" />
                <p className="text-xs text-ink-500 mt-2 mb-0">
                    202 means accepted, not delivered. Poll{" "}
                    <code className="font-mono text-ink-700">GET /messages/{"{message_id}"}</code> or subscribe
                    to the <code className="font-mono text-ink-700">message.delivered</code> webhook to find out
                    what happened to it.
                </p>
            </div>

            <SendsWith detail={data.sends_with} />
            <Notes notes={data.notes} />
        </div>
    );
}

function SectionLabel({ children }) {
    return <div className="text-sm font-semibold text-ink-800 mb-1.5">{children}</div>;
}

function Endpoint({ data }) {
    return (
        <div className="space-y-3">
            <div className="flex items-center gap-2 flex-wrap">
                <MethodBadge method={data.endpoint.method} />
                <code className="text-sm font-mono text-ink-800">{data.endpoint.path}</code>
                <Badge tone="blue">Needs: {data.endpoint.required_scope}</Badge>
            </div>
            <CopyField value={data.endpoint.url} label="Full URL" />
            <CopyField
                value={`Authorization: Bearer ${"YOUR_API_TOKEN"}`}
                label="Authorization header"
                hint="Replace YOUR_API_TOKEN with the token you copied when you created it."
            />
        </div>
    );
}

function Variables({ rows }) {
    if (!rows?.length) {
        return (
            <Alert tone="info" className="mb-0">
                This template has no variables — send it with just{" "}
                <code className="font-mono">to</code> and <code className="font-mono">template_id</code>.
            </Alert>
        );
    }

    return (
        <div>
            <SectionLabel>Variables</SectionLabel>
            <div className="border border-ink-200 rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                    <thead className="bg-ink-50">
                        <tr>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Name</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Type</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Required</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Example</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.name} className="border-t border-ink-100">
                                <td className="px-3 py-2 font-mono text-ink-800">{row.name}</td>
                                <td className="px-3 py-2 text-ink-500">{row.type}</td>
                                <td className="px-3 py-2">
                                    {row.required ? (
                                        <Badge tone="amber">Required</Badge>
                                    ) : (
                                        // Not required because the template
                                        // carries a fallback for it. Worth
                                        // saying, or somebody sends a field
                                        // they never needed to compute.
                                        <span className="text-ink-500">
                                            Optional — falls back to “{row.example}”
                                        </span>
                                    )}
                                </td>
                                <td className="px-3 py-2 font-mono text-ink-600">{row.example}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

function SendsWith({ detail }) {
    if (!detail) return null;
    const extras = ["media", "buttons", "menu", "poll", "caption"].filter((key) => detail[key]);
    if (!extras.length) return null;

    return (
        <div>
            <SectionLabel>Also sent, from the template</SectionLabel>
            <p className="text-xs text-ink-500 mt-0 mb-2">
                These come from the template itself. There is no request field for them — edit the template
                to change them.
            </p>
            <CodeBlock value={detail} label="Included automatically" copyable={false} maxHeight="14rem" />
        </div>
    );
}

function Notes({ notes }) {
    if (!notes?.length) return null;
    return (
        <div className="space-y-2">
            {notes.map((note) => (
                <Alert
                    key={note}
                    tone={note.toLowerCase().includes("paused") ? "warning" : "info"}
                    className="mb-0"
                >
                    {note}
                </Alert>
            ))}
        </div>
    );
}

/**
 * The compact form: three buttons and no panel.
 *
 * Used on a template card, where the full panel would not fit and where the
 * commonest thing somebody wants is the JSON on their clipboard rather than on
 * their screen.
 */
export function TemplateApiActions({ described, onOpen }) {
    if (!described) return null;
    return (
        <div className="flex items-center gap-1.5 flex-wrap">
            <CopyButton
                value={toText(described.request_body)}
                label="Copy JSON"
                copiedLabel="JSON copied"
                size="sm"
                className="!bg-surface !text-ink-700 border border-ink-200 hover:!bg-ink-50"
            />
            <CopyButton
                value={described.curl}
                label="Copy cURL"
                copiedLabel="cURL copied"
                size="sm"
                className="!bg-surface !text-ink-700 border border-ink-200 hover:!bg-ink-50"
            />
            {onOpen && (
                <button
                    type="button"
                    onClick={onOpen}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-sm font-semibold rounded-md cursor-pointer bg-surface text-ink-700 border border-ink-200 hover:bg-ink-50"
                >
                    <ExternalLink size={13} aria-hidden="true" />
                    Test in API Tester
                </button>
            )}
        </div>
    );
}
