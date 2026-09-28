import { useEffect, useState } from "react";
import {
    Activity,
    BookOpen,
    CheckCircle2,
    KeyRound,
    Link2,
    ShieldCheck,
    XCircle,
    Zap,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import CodeBlock, { CopyField } from "../../components/ui/CodeBlock";
import Spinner from "../../components/ui/Spinner";
import { Field, Input } from "../../components/ui/Field";

/**
 * Developer -> API Overview.
 *
 * The screen that answers the two questions the old developer page did not:
 * *where do I take the URL from*, and *how do I know it works*. Everything on
 * it is server-supplied -- the base URL is derived from the deployment, not
 * typed into this file, so a self-hosted install shows its own address rather
 * than a placeholder somebody has to guess at.
 */
export default function ApiOverview({ onGoToTokens }) {
    const [overview, setOverview] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/developer/api/overview")
            .then((response) => setOverview(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the API overview.")));
    }, []);

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!overview) return <Spinner />;

    return (
        <div className="space-y-5">
            <BaseUrlCard overview={overview} />
            <AuthCard overview={overview} />
            <ConnectionTestCard baseUrl={overview.base_url} onGoToTokens={onGoToTokens} />
            <QuickStartCard steps={overview.quick_start} />
            <LimitsCard limits={overview.rate_limits} />
        </div>
    );
}

// ---------------------------------------------------------------------------

function BaseUrlCard({ overview }) {
    return (
        <Card>
            <CardHeader
                title="API base URL"
                description="Every endpoint below is relative to this address. Copy it into Postman, your code, or wherever you keep configuration."
                className="mb-4"
            />
            <CopyField value={overview.base_url} />

            {overview.environments.length > 1 && (
                <div className="mt-4 space-y-3">
                    <p className="text-xs font-semibold uppercase tracking-wide text-ink-500 m-0">
                        Other addresses for this deployment
                    </p>
                    {overview.environments
                        .filter((environment) => !environment.current)
                        .map((environment) => (
                            <CopyField
                                key={environment.base_url}
                                value={environment.base_url}
                                label={environment.name}
                                hint={environment.note}
                            />
                        ))}
                </div>
            )}

            <div className="flex flex-wrap gap-2 mt-4 pt-4 border-t border-ink-100">
                <Button
                    variant="secondary"
                    size="sm"
                    icon={BookOpen}
                    onClick={() => window.open(overview.links.reference, "_blank", "noopener")}
                >
                    Reference (HTML)
                </Button>
                <Button
                    variant="secondary"
                    size="sm"
                    icon={Link2}
                    onClick={() => window.open(overview.links.openapi, "_blank", "noopener")}
                >
                    OpenAPI schema
                </Button>
                <Badge tone="gray">API version {overview.version}</Badge>
            </div>
        </Card>
    );
}

function AuthCard({ overview }) {
    const { authentication } = overview;
    return (
        <Card>
            <CardHeader
                title="How to use your API token"
                description="Five steps, and the same header on every single request."
                className="mb-4"
            />

            <ol className="text-sm text-ink-700 space-y-1.5 mb-4 pl-5">
                <li>Create an API token on the API Tokens tab.</li>
                <li>Copy it — it is shown once and never again.</li>
                <li>Pick the endpoint you want from the Documentation tab.</li>
                <li>
                    Put the token in the <code className="font-mono text-ink-900">Authorization</code> header.
                </li>
                <li>Send your request.</li>
            </ol>

            <CodeBlock
                value={`${authentication.example}\nContent-Type: application/json`}
                language="http"
                label="Request headers"
            />

            <div className="mt-4 space-y-2">
                {authentication.notes.map((note) => (
                    <div key={note} className="flex items-start gap-2 text-sm text-ink-600">
                        <ShieldCheck size={15} className="shrink-0 mt-0.5 text-brand-600" aria-hidden="true" />
                        <span>{note}</span>
                    </div>
                ))}
            </div>
        </Card>
    );
}

/**
 * "Test API Connection".
 *
 * Calls the real public API from the browser with a token the customer pastes
 * in, then calls `/diagnostics` to walk the rest of the chain. Deliberately not
 * proxied through the dashboard's own backend: a proxy would prove that *the
 * server* can reach the API, which is never the thing in doubt. This proves the
 * token works, over the same URL the customer is about to put in their code.
 *
 * The token is held in component state and never persisted — there is nowhere
 * to persist it to, since the backend cannot recover it either.
 */
function ConnectionTestCard({ baseUrl, onGoToTokens }) {
    const [token, setToken] = useState("");
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(null);

    const run = async () => {
        setBusy(true);
        setResult(null);
        try {
            const headers = { Authorization: `Bearer ${token.trim()}`, Accept: "application/json" };
            const healthResponse = await fetch(`${baseUrl}/health`, { headers });
            const health = await healthResponse.json();

            if (!healthResponse.ok || !health.authenticated) {
                setResult({
                    ok: false,
                    // The API's own error code and sentence, not a rewritten
                    // one: the customer will see the same pair in their own
                    // client and the two must match.
                    checks: [
                        {
                            name: "authentication",
                            label: "API token",
                            status: "fail",
                            detail:
                                health?.error?.message ||
                                "The API token was not accepted. Check you copied all of it.",
                            code: health?.error?.code,
                        },
                    ],
                    summary: health?.error?.message || "Authentication failed.",
                });
                return;
            }

            const diagnosticsResponse = await fetch(`${baseUrl}/diagnostics`, { headers });
            const diagnostics = await diagnosticsResponse.json();
            setResult({
                ok: Boolean(diagnostics.ready_to_send),
                checks: diagnostics.checks || [],
                summary: diagnostics.summary,
                workspace: health.workspace?.name,
            });
        } catch (err) {
            // A network-level failure, which on this page almost always means
            // the base URL is unreachable from the browser rather than that
            // the API is down -- so say which.
            setResult({
                ok: false,
                checks: [
                    {
                        name: "network",
                        label: "Connection",
                        status: "fail",
                        detail: `Could not reach ${baseUrl}. ${err.message}`,
                    },
                ],
                summary: "The API could not be reached from this browser.",
            });
        } finally {
            setBusy(false);
        }
    };

    return (
        <Card>
            <CardHeader
                title="Test API connection"
                description="Paste a token and we will check it end to end — authentication, permissions, your WhatsApp connection and your credits."
                className="mb-4"
                action={
                    onGoToTokens && (
                        <Button variant="ghost" size="sm" icon={KeyRound} onClick={onGoToTokens}>
                            Create a token
                        </Button>
                    )
                }
            />

            <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
                <Field label="API token" className="flex-1 mb-0">
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
                <Button icon={Zap} onClick={run} loading={busy} disabled={!token.trim()}>
                    Test connection
                </Button>
            </div>

            {result && (
                <div className="mt-4">
                    <Alert tone={result.ok ? "success" : "error"}>
                        {result.ok
                            ? `Connected${result.workspace ? ` to ${result.workspace}` : ""}. Everything needed to send is in place.`
                            : result.summary}
                    </Alert>
                    <div className="space-y-1.5">
                        {result.checks.map((check) => (
                            <div key={check.name} className="flex items-start gap-2 text-sm">
                                {check.status === "pass" ? (
                                    <CheckCircle2
                                        size={16}
                                        className="shrink-0 mt-0.5 text-brand-600"
                                        aria-hidden="true"
                                    />
                                ) : (
                                    <XCircle size={16} className="shrink-0 mt-0.5 text-red-500" aria-hidden="true" />
                                )}
                                <div className="min-w-0">
                                    <span
                                        className={`font-semibold ${
                                            check.status === "pass" ? "text-ink-800" : "text-red-700"
                                        }`}
                                    >
                                        {check.label}: {check.status === "pass" ? "PASS" : "FAIL"}
                                    </span>
                                    <span className="text-ink-600"> — {check.detail}</span>
                                    {check.code && (
                                        <code className="ml-1.5 text-xs text-red-600 font-mono">{check.code}</code>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </Card>
    );
}

function QuickStartCard({ steps }) {
    return (
        <Card>
            <CardHeader
                title="Quick start"
                description="From nothing to a delivered WhatsApp message, in ten steps."
                className="mb-4"
            />
            <ol className="space-y-3 m-0 p-0 list-none">
                {steps.map((step) => (
                    <li key={step.step} className="flex gap-3">
                        <span className="shrink-0 w-6 h-6 rounded-full bg-brand-50 text-brand-700 text-xs font-bold inline-flex items-center justify-center ring-1 ring-inset ring-brand-600/20">
                            {step.step}
                        </span>
                        <div className="min-w-0 pt-0.5">
                            <div className="text-sm font-semibold text-ink-900">{step.title}</div>
                            <p className="text-sm text-ink-600 m-0">{step.detail}</p>
                        </div>
                    </li>
                ))}
            </ol>
        </Card>
    );
}

function LimitsCard({ limits }) {
    return (
        <Card>
            <CardHeader title="Rate limits" description={limits.note} className="mb-4" />
            <div className="grid sm:grid-cols-3 gap-3">
                {[
                    ["Requests", limits.requests_per_minute, "per minute"],
                    ["Messages", limits.messages_per_minute, "per minute"],
                    ["Campaigns", limits.campaigns_per_day, "per day"],
                ].map(([label, value, unit]) => (
                    <div key={label} className="border border-ink-200 rounded-lg p-3">
                        <div className="text-xs font-semibold uppercase tracking-wide text-ink-500">{label}</div>
                        <div className="text-xl font-bold text-ink-900 mt-0.5">{value || "—"}</div>
                        <div className="text-xs text-ink-500">{unit}</div>
                    </div>
                ))}
            </div>
            <div className="flex items-start gap-2 text-sm text-ink-600 mt-4">
                <Activity size={15} className="shrink-0 mt-0.5 text-ink-400" aria-hidden="true" />
                <span>
                    Every response carries <code className="font-mono text-ink-800">X-RateLimit-Remaining</code>{" "}
                    and <code className="font-mono text-ink-800">X-RateLimit-Reset</code>. A 429 also carries{" "}
                    <code className="font-mono text-ink-800">Retry-After</code> — wait that many seconds rather
                    than retrying immediately.
                </span>
            </div>
        </Card>
    );
}
