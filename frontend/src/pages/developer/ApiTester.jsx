import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Clock, Send } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import CodeBlock, { MethodBadge } from "../../components/ui/CodeBlock";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";

/**
 * Developer -> API Tester.
 *
 * A request builder that calls the **real public API** from the browser, over
 * the same base URL a customer will put in their own code, with a token they
 * paste in.
 *
 * It is not a proxy. Sending these through the dashboard's own backend would
 * prove that our server can reach our API -- which is never in question -- and
 * would hide every class of failure that actually happens here: a token that
 * was copied short, a URL that resolves differently from outside, a CORS or
 * TLS problem, a rate limit. `fetch` from the page reproduces the customer's
 * situation exactly, and the status line, headers and body shown below are the
 * ones they will get.
 *
 * The token deliberately never leaves this component: it is not stored, not
 * sent to our backend, and gone on reload.
 */
export default function ApiTester({ initialEndpoint = null }) {
    const [reference, setReference] = useState(null);
    const [error, setError] = useState("");

    const [selectedId, setSelectedId] = useState(null);
    const [token, setToken] = useState("");
    const [path, setPath] = useState("");
    const [method, setMethod] = useState("GET");
    const [body, setBody] = useState("");
    const [bodyError, setBodyError] = useState("");
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(null);

    useEffect(() => {
        api.get("/developer/api/reference")
            .then((response) => setReference(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the endpoint list.")));
    }, []);

    const endpoints = useMemo(
        () => (reference?.groups || []).flatMap((group) => group.endpoints),
        [reference],
    );

    const choose = (endpoint) => {
        if (!endpoint) return;
        setSelectedId(endpoint.id);
        setMethod(endpoint.method);
        setPath(endpoint.path);
        setBody(endpoint.request_body ? JSON.stringify(endpoint.request_body, null, 2) : "");
        setBodyError("");
        setResult(null);
    };

    // Preselect whatever the Documentation tab's "Try it" sent over, and
    // otherwise open on the health check -- the one request that is safe to
    // run without reading anything first.
    useEffect(() => {
        if (!endpoints.length || selectedId) return;
        const wanted = initialEndpoint
            ? endpoints.find((endpoint) => endpoint.id === initialEndpoint.id)
            : endpoints.find((endpoint) => endpoint.path === "/health");
        choose(wanted || endpoints[0]);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [endpoints, initialEndpoint]);

    const send = async () => {
        setBodyError("");
        let parsed;
        if (body.trim() && method !== "GET" && method !== "DELETE") {
            try {
                parsed = JSON.parse(body);
            } catch (parseError) {
                // Caught here rather than let through: a malformed body would
                // come back as our own 400 INVALID_REQUEST, which reads like
                // the API rejected the data rather than the JSON being broken.
                setBodyError(`That is not valid JSON — ${parseError.message}`);
                return;
            }
        }

        setBusy(true);
        setResult(null);
        const startedAt = performance.now();
        const url = `${reference.base_url}${path}`;

        try {
            const response = await fetch(url, {
                method,
                headers: {
                    Authorization: `Bearer ${token.trim()}`,
                    Accept: "application/json",
                    ...(parsed !== undefined ? { "Content-Type": "application/json" } : {}),
                },
                ...(parsed !== undefined ? { body: JSON.stringify(parsed) } : {}),
            });

            const text = await response.text();
            let payload;
            try {
                payload = JSON.parse(text);
            } catch {
                payload = text;
            }

            setResult({
                url,
                method,
                status: response.status,
                statusText: response.statusText,
                ok: response.ok,
                durationMs: Math.round(performance.now() - startedAt),
                // The rate-limit and request-id headers, which are the ones a
                // developer needs when something is refused and the body alone
                // does not explain the pacing.
                headers: Object.fromEntries(
                    [...response.headers.entries()].filter(([name]) =>
                        name.toLowerCase().startsWith("x-") || name.toLowerCase() === "retry-after",
                    ),
                ),
                body: payload,
            });
        } catch (networkError) {
            setResult({
                url,
                method,
                status: 0,
                statusText: "Network error",
                ok: false,
                durationMs: Math.round(performance.now() - startedAt),
                headers: {},
                body: {
                    success: false,
                    error: {
                        code: "NETWORK_ERROR",
                        message: `Could not reach ${url}. ${networkError.message}`,
                    },
                },
            });
        } finally {
            setBusy(false);
        }
    };

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!reference) return <Spinner />;

    const selected = endpoints.find((endpoint) => endpoint.id === selectedId);
    const takesBody = method !== "GET" && method !== "DELETE";

    return (
        <div className="space-y-5">
            <Card>
                <CardHeader
                    title="API tester"
                    description="Runs against the live API with a token you paste in. Nothing is proxied — this is exactly the request your own software would make."
                    className="mb-4"
                />

                <Field label="Endpoint" hint={selected?.summary}>
                    <Select
                        value={selectedId || ""}
                        onChange={(event) =>
                            choose(endpoints.find((endpoint) => endpoint.id === event.target.value))
                        }
                    >
                        {(reference.groups || []).map((group) => (
                            <optgroup key={group.name} label={group.name}>
                                {group.endpoints.map((endpoint) => (
                                    <option key={endpoint.id} value={endpoint.id}>
                                        {endpoint.method} {endpoint.path} — {endpoint.summary}
                                    </option>
                                ))}
                            </optgroup>
                        ))}
                    </Select>
                </Field>

                <div className="grid sm:grid-cols-[auto_1fr] gap-3 items-end">
                    <Field label="Method">
                        <Select value={method} onChange={(event) => setMethod(event.target.value)}>
                            {["GET", "POST", "PUT", "PATCH", "DELETE"].map((verb) => (
                                <option key={verb}>{verb}</option>
                            ))}
                        </Select>
                    </Field>
                    <Field
                        label="Path"
                        hint={`Full URL: ${reference.base_url}${path}`}
                    >
                        <Input
                            value={path}
                            onChange={(event) => setPath(event.target.value)}
                            className="font-mono"
                            spellCheck="false"
                        />
                    </Field>
                </div>

                <Field
                    label="API token"
                    hint="Held in this page only. It is never sent to the dashboard and is gone when you reload."
                >
                    <Input
                        type="password"
                        value={token}
                        onChange={(event) => setToken(event.target.value)}
                        placeholder="ss_live_..."
                        autoComplete="off"
                        spellCheck="false"
                        className="font-mono"
                    />
                </Field>

                {selected?.required_permission && (
                    <Alert tone="info">
                        This endpoint needs the <strong>{selected.required_permission_label}</strong> permission
                        (<code className="font-mono">{selected.required_permission}</code>). A token without it
                        gets a 403 <code className="font-mono">INSUFFICIENT_PERMISSION</code>.
                    </Alert>
                )}

                {takesBody && (
                    <Field label="Request body (JSON)" error={bodyError}>
                        <Textarea
                            value={body}
                            onChange={(event) => setBody(event.target.value)}
                            rows={10}
                            className="font-mono text-xs"
                            spellCheck="false"
                        />
                    </Field>
                )}

                <Button icon={Send} onClick={send} loading={busy} disabled={!token.trim() || !path.trim()}>
                    Send test request
                </Button>
            </Card>

            {result && <ResultCard result={result} />}
        </div>
    );
}

function ResultCard({ result }) {
    const tone = result.ok ? "green" : result.status === 0 ? "gray" : "red";
    const errorCode = result.body?.error?.code;

    return (
        <Card>
            <CardHeader
                title="Response"
                className="mb-4"
                action={
                    <div className="flex items-center gap-2">
                        <span className="inline-flex items-center gap-1 text-xs text-ink-500">
                            <Clock size={13} aria-hidden="true" />
                            {result.durationMs} ms
                        </span>
                        <Badge tone={tone}>
                            {result.status || "—"} {result.statusText}
                        </Badge>
                    </div>
                }
            />

            <div className="flex items-center gap-2 mb-3 flex-wrap">
                <MethodBadge method={result.method} />
                <code className="text-sm font-mono text-ink-700 break-all">{result.url}</code>
            </div>

            <Alert tone={result.ok ? "success" : "error"}>
                {result.ok ? (
                    <span className="inline-flex items-center gap-1.5">
                        <CheckCircle2 size={14} aria-hidden="true" />
                        The API accepted this request.
                    </span>
                ) : (
                    <span className="inline-flex items-center gap-1.5">
                        <AlertCircle size={14} aria-hidden="true" />
                        {result.body?.error?.message || "The request was refused."}
                        {errorCode && <code className="font-mono text-xs">({errorCode})</code>}
                    </span>
                )}
            </Alert>

            <CodeBlock value={result.body} label="Response body" />

            {Object.keys(result.headers).length > 0 && (
                <details className="mt-3">
                    <summary className="text-sm font-semibold text-ink-700 cursor-pointer">
                        Response headers
                    </summary>
                    <CodeBlock
                        value={Object.entries(result.headers)
                            .map(([name, value]) => `${name}: ${value}`)
                            .join("\n")}
                        language="http"
                        label="Headers"
                        className="mt-2"
                    />
                </details>
            )}
        </Card>
    );
}
