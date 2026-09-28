import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, PlayCircle } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Card, { CardHeader } from "../../components/ui/Card";
import CodeBlock, { CopyButton, CopyField, MethodBadge } from "../../components/ui/CodeBlock";
import SearchInput from "../../components/ui/SearchInput";
import Spinner from "../../components/ui/Spinner";

/**
 * Developer -> Documentation.
 *
 * Every endpoint, with its method, URL, required permission, headers, example
 * body, responses (successes *and* failures) and a copyable curl command.
 *
 * The whole page is one server payload built by walking the OpenAPI document,
 * so an endpoint that is added, renamed or has its scope changed appears here
 * correctly without anybody editing this file. There is deliberately no
 * hardcoded endpoint list in the frontend -- that is the thing the brief's
 * point 24 forbids, and it is how documentation ends up describing an API that
 * no longer exists.
 */
export default function ApiDocumentation({ onTry }) {
    const [reference, setReference] = useState(null);
    const [error, setError] = useState("");
    const [query, setQuery] = useState("");
    const [open, setOpen] = useState(null);

    useEffect(() => {
        api.get("/developer/api/reference")
            .then((response) => setReference(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the API reference.")));
    }, []);

    const groups = useMemo(() => {
        if (!reference) return [];
        const needle = query.trim().toLowerCase();
        if (!needle) return reference.groups;
        return reference.groups
            .map((group) => ({
                ...group,
                endpoints: group.endpoints.filter((endpoint) =>
                    `${endpoint.method} ${endpoint.path} ${endpoint.summary}`.toLowerCase().includes(needle),
                ),
            }))
            .filter((group) => group.endpoints.length);
    }, [reference, query]);

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!reference) return <Spinner />;

    return (
        <div className="space-y-5">
            <Card>
                <CardHeader
                    title="API reference"
                    description="Every endpoint this deployment serves. The URLs below are live — you can paste any of them straight into a terminal."
                    className="mb-4"
                />
                <CopyField value={reference.base_url} label="Base URL" />
            </Card>

            <SearchInput
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onClear={() => setQuery("")}
                placeholder="Search endpoints — 'send', 'campaign', 'contacts'…"
            />

            {groups.length === 0 ? (
                <Alert tone="info">No endpoint matches “{query}”.</Alert>
            ) : (
                groups.map((group) => (
                    <Card key={group.name} padded={false}>
                        <div className="px-5 sm:px-6 pt-5 sm:pt-6 pb-3">
                            <h2 className="text-base font-bold text-ink-900 m-0">{group.name}</h2>
                            {group.description && (
                                <p className="text-sm text-ink-500 mt-1 mb-0">{group.description}</p>
                            )}
                        </div>
                        <div className="border-t border-ink-100">
                            {group.endpoints.map((endpoint) => (
                                <EndpointRow
                                    key={endpoint.id}
                                    endpoint={endpoint}
                                    expanded={open === endpoint.id}
                                    onToggle={() => setOpen(open === endpoint.id ? null : endpoint.id)}
                                    onTry={onTry}
                                />
                            ))}
                        </div>
                    </Card>
                ))
            )}

            <ErrorCodes rows={reference.error_codes} />
            <WebhookEvents rows={reference.webhook_events} />
        </div>
    );
}

function EndpointRow({ endpoint, expanded, onToggle, onTry }) {
    const Chevron = expanded ? ChevronDown : ChevronRight;

    return (
        <div className="border-b border-ink-100 last:border-b-0">
            <button
                type="button"
                onClick={onToggle}
                aria-expanded={expanded}
                className="w-full flex items-center gap-3 px-5 sm:px-6 py-3 text-left bg-transparent border-0 cursor-pointer hover:bg-ink-50 transition-colors"
            >
                <Chevron size={15} className="shrink-0 text-ink-400" aria-hidden="true" />
                <MethodBadge method={endpoint.method} />
                <code className="text-sm font-mono text-ink-800 truncate">{endpoint.path}</code>
                <span className="text-sm text-ink-500 truncate hidden md:inline ml-auto">
                    {endpoint.summary}
                </span>
            </button>

            {expanded && (
                <div className="px-5 sm:px-6 pb-5 space-y-4">
                    {endpoint.description && (
                        <p className="text-sm text-ink-600 m-0">{endpoint.description}</p>
                    )}

                    <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={endpoint.authentication === "None" ? "gray" : "blue"}>
                            Auth: {endpoint.authentication}
                        </Badge>
                        {endpoint.required_permission ? (
                            <Badge tone="violet" title={endpoint.required_permission}>
                                Needs: {endpoint.required_permission_label}
                            </Badge>
                        ) : (
                            <Badge tone="gray">No permission required</Badge>
                        )}
                        {onTry && (
                            <button
                                type="button"
                                onClick={() => onTry(endpoint)}
                                className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-md cursor-pointer bg-surface text-ink-700 border border-ink-200 hover:bg-ink-50"
                            >
                                <PlayCircle size={13} aria-hidden="true" />
                                Try it
                            </button>
                        )}
                    </div>

                    <CopyField value={endpoint.url} label="URL" />

                    {Object.keys(endpoint.headers).length > 0 && (
                        <div>
                            <Label>Headers</Label>
                            <CodeBlock
                                value={Object.entries(endpoint.headers)
                                    .map(([name, value]) => `${name}: ${value}`)
                                    .join("\n")}
                                language="http"
                                label="Headers"
                            />
                        </div>
                    )}

                    {endpoint.parameters.length > 0 && <Parameters rows={endpoint.parameters} />}

                    {endpoint.request_body && (
                        <div>
                            <Label>Request body</Label>
                            <CodeBlock value={endpoint.request_body} label="JSON" />
                        </div>
                    )}

                    <div>
                        <Label>Responses</Label>
                        <div className="space-y-2">
                            {endpoint.responses.map((response) => (
                                <div key={response.status}>
                                    <div className="flex items-center gap-2 mb-1">
                                        <Badge tone={response.is_error ? "red" : "green"}>{response.status}</Badge>
                                        <span className="text-sm text-ink-600">{response.description}</span>
                                    </div>
                                    {response.example && (
                                        <CodeBlock
                                            value={response.example}
                                            label={`${response.status} body`}
                                            maxHeight="14rem"
                                        />
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>

                    <div>
                        <Label>Example</Label>
                        <CodeBlock
                            value={endpoint.curl}
                            language="bash"
                            label="cURL"
                            actions={
                                endpoint.request_body ? (
                                    <CopyButton
                                        value={endpoint.request_body}
                                        label="Copy JSON"
                                        copiedLabel="JSON copied"
                                        className="!bg-ink-700"
                                    />
                                ) : null
                            }
                        />
                    </div>
                </div>
            )}
        </div>
    );
}

function Label({ children }) {
    return <div className="text-sm font-semibold text-ink-800 mb-1.5">{children}</div>;
}

function Parameters({ rows }) {
    return (
        <div>
            <Label>Parameters</Label>
            <div className="border border-ink-200 rounded-lg overflow-x-auto">
                <table className="w-full text-sm min-w-[32rem]">
                    <thead className="bg-ink-50">
                        <tr>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Name</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">In</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Required</th>
                            <th className="text-left font-semibold text-ink-600 px-3 py-2">Description</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={`${row.in}-${row.name}`} className="border-t border-ink-100">
                                <td className="px-3 py-2 font-mono text-ink-800">{row.name}</td>
                                <td className="px-3 py-2 text-ink-500">{row.in}</td>
                                <td className="px-3 py-2 text-ink-500">{row.required ? "Yes" : "No"}</td>
                                <td className="px-3 py-2 text-ink-600">{row.description || "—"}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

function ErrorCodes({ rows }) {
    return (
        <Card>
            <CardHeader
                title="Error codes"
                description="Every failure comes back as { success: false, error: { code, message } }. Branch on the code — the message is prose and may be reworded."
                className="mb-4"
            />
            <div className="border border-ink-200 rounded-lg overflow-x-auto">
                <table className="w-full text-sm min-w-[30rem]">
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.code} className="border-b border-ink-100 last:border-b-0">
                                <td className="px-3 py-2 font-mono text-xs text-red-700 align-top whitespace-nowrap">
                                    {row.code}
                                </td>
                                <td className="px-3 py-2 text-ink-600">{row.description}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </Card>
    );
}

function WebhookEvents({ rows }) {
    return (
        <Card>
            <CardHeader
                title="Webhook events"
                description="Subscribe on the Webhooks tab to receive these instead of polling."
                className="mb-4"
            />
            <div className="border border-ink-200 rounded-lg overflow-x-auto">
                <table className="w-full text-sm min-w-[30rem]">
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.event} className="border-b border-ink-100 last:border-b-0">
                                <td className="px-3 py-2 font-mono text-xs text-ink-800 align-top whitespace-nowrap">
                                    {row.event}
                                </td>
                                <td className="px-3 py-2 text-ink-600">{row.description}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </Card>
    );
}
