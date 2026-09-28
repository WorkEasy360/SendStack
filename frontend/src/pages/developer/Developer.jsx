import { useEffect, useState } from "react";
import {
    Activity,
    BookOpen,
    Check,
    Copy,
    ExternalLink,
    FileJson,
    FlaskConical,
    Home,
    KeyRound,
    Package,
    Pause,
    Play,
    Plus,
    RefreshCw,
    Send,
    ShieldOff,
    Trash2,
    TriangleAlert,
    Webhook,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge, { StatusBadge } from "../../components/ui/Badge";
import Button, { IconButton } from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import { CopyField } from "../../components/ui/CodeBlock";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import Tabs from "../../components/ui/Tabs";
import { CheckboxField, Field, Input, Select, Toggle } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import ApiDocumentation from "./ApiDocumentation";
import ApiOverview from "./ApiOverview";
import ApiPostman from "./ApiPostman";
import ApiTemplates from "./ApiTemplates";
import ApiTester from "./ApiTester";

/**
 * Settings -> Developer.
 *
 * Two screens for the two halves of the integration story: the credentials
 * something uses to call us, and the endpoints we call back.
 *
 * The one interaction worth calling out is the secret reveal. Both a new API
 * key and a new webhook secret exist exactly once, in the response to the
 * request that created them, and are never retrievable again. So the dialog
 * that shows one is deliberately more work to dismiss than an ordinary modal:
 * closing it destroys the only copy, and somebody who clicks past it without
 * reading has lost the key rather than merely closed a window.
 */

//: The developer portal, in the order somebody meets it: find the URL, make a
//: token, read what it can call, try one, take it away in Postman, then the
//: two operational tabs. Templates sits between Documentation and the tester
//: because "which template id do I send?" is the question that stops people
//: between reading and doing.
const TABS = [
    { id: "overview", label: "Overview", icon: Home },
    { id: "keys", label: "API Tokens", icon: KeyRound },
    { id: "docs", label: "Documentation", icon: BookOpen },
    { id: "templates", label: "Templates", icon: FileJson },
    { id: "tester", label: "API Tester", icon: FlaskConical },
    { id: "postman", label: "Postman", icon: Package },
    { id: "webhooks", label: "Webhooks", icon: Webhook },
    { id: "activity", label: "API Logs", icon: Activity },
];

const STATUS_TONE = {
    active: "green",
    disabled: "gray",
    expired: "amber",
    revoked: "red",
};

function formatDate(value) {
    if (!value) return "—";
    return new Date(value).toLocaleString(undefined, {
        day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
}

function formatRelative(value) {
    if (!value) return "Never";
    const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return "Just now";
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
}

export default function Developer() {
    const [tab, setTab] = useState("overview");
    //: What the Documentation tab's "Try it" and the Templates tab's "Test in
    //: API Tester" hand over. Held here rather than in the tester so the jump
    //: survives the tab switch, which is the whole point of the button.
    const [pendingTry, setPendingTry] = useState(null);

    const openTester = (endpoint) => {
        setPendingTry(endpoint);
        setTab("tester");
    };

    return (
        <div>
            <PageHeader
                title="Developer"
                description="Connect your own software to SendStack: API tokens to call us, webhooks for us to call you."
                actions={
                    <Button
                        variant="secondary"
                        icon={BookOpen}
                        onClick={() => window.open("/api/public/v1/docs", "_blank", "noopener")}
                    >
                        Reference
                        <ExternalLink size={13} aria-hidden="true" />
                    </Button>
                }
            />

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "overview" && <ApiOverview onGoToTokens={() => setTab("keys")} />}
            {tab === "keys" && <ApiKeysTab />}
            {tab === "docs" && <ApiDocumentation onTry={openTester} />}
            {tab === "templates" && (
                <ApiTemplates
                    onTry={(described) =>
                        // The tester speaks the reference's endpoint shape, so a
                        // template arrives as the send endpoint with its own body
                        // already filled in.
                        openTester({
                            id: "post--messages-send",
                            method: "POST",
                            path: "/messages/send",
                            summary: `Send ${described.template_name}`,
                            request_body: described.request_body,
                            required_permission: described.endpoint.required_scope,
                            required_permission_label: "Send WhatsApp messages",
                        })
                    }
                />
            )}
            {tab === "tester" && <ApiTester initialEndpoint={pendingTry} />}
            {tab === "postman" && <ApiPostman />}
            {tab === "webhooks" && <WebhooksTab />}
            {tab === "activity" && <ActivityTab />}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Secret reveal
// ---------------------------------------------------------------------------

/**
 * The one-and-only sighting of a new credential.
 *
 * The confirm button stays disabled until the value has been copied, which is
 * the whole point: this dialog is the last moment the secret exists anywhere
 * we can show it.
 */
function SecretRevealDialog({ title, description, secret, onClose }) {
    const [copied, setCopied] = useState(false);
    const toast = useToast();

    const copy = async () => {
        try {
            await navigator.clipboard.writeText(secret);
            setCopied(true);
        } catch {
            // Clipboard access is refused in some browsers and over plain
            // HTTP. Selecting the text by hand still works, so unlock the
            // button rather than trapping somebody in the dialog.
            setCopied(true);
            toast.error("Could not copy automatically — select the value and copy it by hand.");
        }
    };

    return (
        <Modal
            title={title}
            description={description}
            width="lg"
            onClose={copied ? onClose : undefined}
            footer={
                <div className="flex justify-end gap-2">
                    <Button variant="primary" disabled={!copied} onClick={onClose}>
                        {copied ? "Done" : "Copy it first"}
                    </Button>
                </div>
            }
        >
            <Alert tone="warning" className="mb-4">
                This is the only time this value is shown. It is stored hashed and cannot be
                retrieved again — if you lose it, rotate the credential.
            </Alert>

            <div className="flex items-stretch gap-2">
                <code className="flex-1 min-w-0 theme-static px-3 py-2.5 bg-ink-900 text-ink-100 rounded-lg text-xs break-all font-mono select-all">
                    {secret}
                </code>
                <Button
                    variant={copied ? "success" : "secondary"}
                    icon={copied ? Check : Copy}
                    onClick={copy}
                    className="shrink-0"
                >
                    {copied ? "Copied" : "Copy"}
                </Button>
            </div>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// API keys
// ---------------------------------------------------------------------------

function ApiKeysTab() {
    const toast = useToast();
    const [keys, setKeys] = useState(null);
    const [error, setError] = useState("");
    const [creating, setCreating] = useState(false);
    const [revealed, setRevealed] = useState(null);
    const [confirm, setConfirm] = useState(null);
    const [usageFor, setUsageFor] = useState(null);

    const load = async () => {
        try {
            const { data } = await api.get("/developer/api-keys/");
            setKeys(data.results ?? data);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err));
            setKeys([]);
        }
    };

    useEffect(() => {
        load();
    }, []);

    const act = async (key, action, successMessage) => {
        try {
            const { data } = await api.post(`/developer/api-keys/${key.id}/${action}/`);
            toast.success(successMessage);
            if (data?.key) {
                setRevealed({
                    title: "Your rotated API key",
                    description: `${data.name} — the previous key stopped working immediately.`,
                    secret: data.key,
                });
            }
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    const remove = async (key) => {
        try {
            await api.delete(`/developer/api-keys/${key.id}/`);
            toast.success(`Deleted "${key.name}".`);
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    if (keys === null) return <Spinner />;

    const columns = [
        {
            key: "name",
            header: "Name",
            render: (row) => (
                <div className="min-w-0">
                    <div className="font-semibold text-ink-900 truncate">{row.name}</div>
                    <code className="text-xs text-ink-500 font-mono">{row.masked_key}</code>
                </div>
            ),
        },
        {
            key: "environment",
            header: "Env",
            hideBelow: "md",
            render: (row) => (
                <Badge tone={row.environment === "live" ? "blue" : "gray"}>{row.environment}</Badge>
            ),
        },
        {
            key: "scopes",
            header: "Permissions",
            hideBelow: "lg",
            render: (row) => (
                <span className="text-sm text-ink-600" title={(row.scopes || []).join(", ")}>
                    {row.scopes?.length === 0
                        ? "None"
                        : `${row.scopes.length} scope${row.scopes.length === 1 ? "" : "s"}`}
                </span>
            ),
        },
        {
            key: "created_at",
            header: "Created",
            hideBelow: "xl",
            render: (row) => <span className="text-sm text-ink-600">{formatDate(row.created_at)}</span>,
        },
        {
            key: "expires_at",
            header: "Expires",
            hideBelow: "xl",
            render: (row) => (
                <span className="text-sm text-ink-600">
                    {row.expires_at ? formatDate(row.expires_at) : "Never"}
                </span>
            ),
        },
        {
            key: "last_used_at",
            header: "Last used",
            hideBelow: "md",
            render: (row) => (
                <div className="text-sm">
                    <div className="text-ink-700">{formatRelative(row.last_used_at)}</div>
                    {row.last_user_agent && (
                        <div className="text-xs text-ink-400 truncate max-w-[180px]" title={row.last_user_agent}>
                            {row.last_user_agent}
                        </div>
                    )}
                </div>
            ),
        },
        {
            key: "status",
            header: "Status",
            render: (row) => <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>,
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (row) => (
                <div className="flex items-center justify-end gap-1">
                    <IconButton
                        icon={Activity}
                        title="Usage"
                        aria-label={`Usage for ${row.name}`}
                        onClick={() => setUsageFor(row)}
                    />
                    {row.status !== "revoked" && (
                        <>
                            <IconButton
                                icon={RefreshCw}
                                title="Rotate"
                                aria-label={`Rotate ${row.name}`}
                                onClick={() =>
                                    setConfirm({
                                        title: `Rotate "${row.name}"?`,
                                        message:
                                            "A new key is issued and this one stops working immediately — there is no " +
                                            "overlap period. Anything still using the old key will start getting 401s.",
                                        confirmLabel: "Rotate",
                                        onConfirm: () => act(row, "rotate", "Key rotated."),
                                    })
                                }
                            />
                            <IconButton
                                icon={row.is_active ? Pause : Play}
                                title={row.is_active ? "Disable" : "Enable"}
                                aria-label={`${row.is_active ? "Disable" : "Enable"} ${row.name}`}
                                onClick={() =>
                                    act(
                                        row,
                                        row.is_active ? "disable" : "enable",
                                        row.is_active ? "Key disabled." : "Key enabled.",
                                    )
                                }
                            />
                            <IconButton
                                icon={ShieldOff}
                                title="Revoke"
                                aria-label={`Revoke ${row.name}`}
                                onClick={() =>
                                    setConfirm({
                                        title: `Revoke "${row.name}"?`,
                                        message:
                                            "This is permanent. The key stops working immediately and cannot be " +
                                            "re-enabled. Use Disable instead if you only want to pause it.",
                                        confirmLabel: "Revoke permanently",
                                        onConfirm: () => act(row, "revoke", "Key revoked."),
                                    })
                                }
                            />
                        </>
                    )}
                    <IconButton
                        icon={Trash2}
                        title="Delete"
                        aria-label={`Delete ${row.name}`}
                        onClick={() =>
                            setConfirm({
                                title: `Delete "${row.name}"?`,
                                message:
                                    "The key and its usage history are removed. Revoking instead keeps the record " +
                                    "of what this key did, which is usually what you want.",
                                confirmLabel: "Delete",
                                onConfirm: () => remove(row),
                            })
                        }
                    />
                </div>
            ),
        },
    ];

    return (
        <div className="space-y-4">
            {error && <Alert tone="error">{error}</Alert>}

            <HowToUseCard />

            <div className="flex justify-end">
                <Button icon={Plus} onClick={() => setCreating(true)}>
                    Create API token
                </Button>
            </div>

            {keys.length === 0 ? (
                <EmptyState
                    icon={KeyRound}
                    title="No API tokens yet"
                    description="Create a token to let your own software send messages, run campaigns and read delivery status."
                    action={
                        <Button icon={Plus} onClick={() => setCreating(true)}>
                            Create API token
                        </Button>
                    }
                />
            ) : (
                <DataTable columns={columns} rows={keys} />
            )}

            {creating && (
                <CreateKeyModal
                    onClose={() => setCreating(false)}
                    onCreated={(data) => {
                        setCreating(false);
                        setRevealed({
                            title: "Your new API token",
                            description: `${data.name} — copy it now. This is the only time it is shown.`,
                            secret: data.key,
                        });
                        load();
                    }}
                />
            )}

            {revealed && (
                <SecretRevealDialog {...revealed} onClose={() => setRevealed(null)} />
            )}

            {usageFor && <UsageModal apiKey={usageFor} onClose={() => setUsageFor(null)} />}

            {confirm && (
                <ConfirmDialog
                    {...confirm}
                    onClose={() => setConfirm(null)}
                    onConfirm={async () => {
                        await confirm.onConfirm();
                        setConfirm(null);
                    }}
                />
            )}
        </div>
    );
}

/**
 * The five-line answer to "I have a token, now what?", shown above the list.
 *
 * The base URL is fetched rather than written, so a self-hosted deployment
 * shows its own address here. A hardcoded `https://yourdomain.com/api/v1` is
 * precisely the thing that made this section useless before -- it is not an
 * example a customer can act on, it is a puzzle.
 */
function HowToUseCard() {
    const [overview, setOverview] = useState(null);

    useEffect(() => {
        // A failure here is not worth an error banner: the token list below is
        // the point of the screen and still works without this strip.
        api.get("/developer/api/overview")
            .then((response) => setOverview(response.data))
            .catch(() => {});
    }, []);

    return (
        <Card>
            <CardHeader
                title="How to use your API token"
                description="Create a token, copy it, pick an endpoint, put the token in the Authorization header, send your request."
                className="mb-4"
            />
            {overview ? (
                <div className="space-y-3">
                    <CopyField
                        value={overview.base_url}
                        label="API base URL"
                        hint="Every endpoint is relative to this. It is generated from this deployment, so it is the address that actually works."
                    />
                    <CopyField
                        value={`Authorization: Bearer ${overview.authentication.live_prefix}YOUR_API_TOKEN`}
                        label="Authorization header"
                        hint="Send this on every request. Never put the token in the URL or a query string."
                    />
                </div>
            ) : (
                <Spinner />
            )}
        </Card>
    );
}

function CreateKeyModal({ onClose, onCreated }) {
    const [name, setName] = useState("");
    const [environment, setEnvironment] = useState("live");
    const [expiresAt, setExpiresAt] = useState("");
    const [scopes, setScopes] = useState([]);
    const [available, setAvailable] = useState(null);
    const [presets, setPresets] = useState([]);
    const [accounts, setAccounts] = useState([]);
    const [allowedAccounts, setAllowedAccounts] = useState([]);
    const [restrictAccounts, setRestrictAccounts] = useState(false);
    const [limits, setLimits] = useState({ requests: "", messages: "", campaigns: "" });
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        (async () => {
            try {
                const [permissionResponse, accountResponse] = await Promise.all([
                    api.get("/developer/api/permissions"),
                    api.get("/whatsapp/accounts/"),
                ]);
                setAvailable(permissionResponse.data.permissions);
                setPresets(permissionResponse.data.presets || []);
                // Least privilege, pre-ticked. Not the empty set, which trains
                // people to hit "select all"; not everything, which is how a
                // token that only needed to send an OTP ends up able to delete
                // templates. The server decides what the default is -- see
                // scopes.DEFAULT_SCOPES.
                setScopes(permissionResponse.data.defaults || []);
                setAccounts(accountResponse.data.results ?? accountResponse.data ?? []);
            } catch (err) {
                setError(apiErrorMessage(err));
                setAvailable([]);
            }
        })();
    }, []);

    const toggleScope = (value) =>
        setScopes((current) =>
            current.includes(value) ? current.filter((item) => item !== value) : [...current, value],
        );

    const submit = async (event) => {
        event.preventDefault();
        setError("");
        if (!scopes.length) {
            setError("Select at least one permission. A key with no scopes cannot do anything.");
            return;
        }
        setSaving(true);
        try {
            const { data } = await api.post("/developer/api-keys/", {
                name: name.trim(),
                environment,
                scopes,
                expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
                allowed_account_ids: restrictAccounts ? allowedAccounts : [],
                requests_per_minute: Number(limits.requests) || 0,
                messages_per_minute: Number(limits.messages) || 0,
                campaigns_per_day: Number(limits.campaigns) || 0,
            });
            onCreated(data);
        } catch (err) {
            setError(apiErrorMessage(err));
        } finally {
            setSaving(false);
        }
    };

    // Grouped by the resource's display name, so the list reads as sections
    // rather than sixteen equally-weighted checkboxes.
    const groups = (available || []).reduce((accumulator, permission) => {
        (accumulator[permission.group] ||= []).push(permission);
        return accumulator;
    }, {});

    const sensitiveCount = (available || []).filter(
        (permission) => permission.sensitive && scopes.includes(permission.value),
    ).length;

    return (
        <Modal
            title="Create API token"
            description="The full token is shown once, immediately after it is created, and never again."
            width="xl"
            onClose={onClose}
            footer={
                <div className="flex justify-end gap-2">
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button form="create-api-key" type="submit" loading={saving}>
                        Create token
                    </Button>
                </div>
            }
        >
            <form id="create-api-key" onSubmit={submit} className="space-y-5">
                {error && <Alert tone="error">{error}</Alert>}

                <Field label="Name" required hint="What will be using this key — 'Shopify store', 'Internal CRM'.">
                    <Input
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                        placeholder="Shopify store"
                        required
                        maxLength={100}
                    />
                </Field>

                <div className="grid sm:grid-cols-2 gap-4">
                    <Field
                        label="Environment"
                        hint="Test keys are refused by every send endpoint."
                    >
                        <Select value={environment} onChange={(event) => setEnvironment(event.target.value)}>
                            <option value="live">Live — sends real messages</option>
                            <option value="test">Test — cannot send</option>
                        </Select>
                    </Field>

                    <Field label="Expires" hint="Leave empty for a key that never expires.">
                        <Input
                            type="date"
                            value={expiresAt}
                            onChange={(event) => setExpiresAt(event.target.value)}
                            min={new Date(Date.now() + 86400000).toISOString().slice(0, 10)}
                        />
                    </Field>
                </div>

                <div>
                    <div className="flex items-center justify-between mb-2">
                        <label className="text-sm font-semibold text-ink-800">What may this token do?</label>
                        <Button type="button" variant="ghost" size="xs" onClick={() => setScopes([])}>
                            Clear all
                        </Button>
                    </div>
                    <p className="text-xs text-ink-500 mt-0 mb-3">
                        A token gets only what you tick here &mdash; anything else it tries is refused with a
                        403. Permissions cannot be changed later; create a new token instead.
                    </p>

                    {/* Presets first. "What do I tick to send an order
                        confirmation?" is the real question, and a list of
                        sixteen checkboxes does not answer it. */}
                    {presets.length > 0 && (
                        <div className="grid sm:grid-cols-3 gap-2 mb-4">
                            {presets.map((preset) => {
                                const active =
                                    preset.scopes.length === scopes.length &&
                                    preset.scopes.every((scope) => scopes.includes(scope));
                                return (
                                    <button
                                        key={preset.id}
                                        type="button"
                                        onClick={() => setScopes(preset.scopes)}
                                        className={`text-left p-3 rounded-lg border cursor-pointer transition-colors ${
                                            active
                                                ? "border-brand-400 bg-brand-50 ring-1 ring-brand-500/20"
                                                : "border-ink-200 bg-surface hover:border-ink-300"
                                        }`}
                                    >
                                        <div className="text-sm font-semibold text-ink-900 flex items-center gap-1.5 flex-wrap">
                                            {preset.label}
                                            {preset.recommended && <Badge tone="green">Recommended</Badge>}
                                        </div>
                                        <p className="text-xs text-ink-500 mt-1 mb-0">{preset.description}</p>
                                    </button>
                                );
                            })}
                        </div>
                    )}

                    {available === null ? (
                        <Spinner />
                    ) : (
                        <div className="space-y-4 max-h-80 overflow-y-auto border border-ink-200 rounded-lg p-4">
                            {Object.entries(groups).map(([group, entries]) => (
                                <div key={group}>
                                    <div className="text-xs font-bold uppercase tracking-wide text-ink-500 mb-1.5">
                                        {group}
                                    </div>
                                    <div className="space-y-1.5">
                                        {entries.map((permission) => (
                                            <CheckboxField
                                                key={permission.value}
                                                label={
                                                    <span className="inline-flex items-center gap-1.5 flex-wrap">
                                                        {permission.label}
                                                        {/* The contract string, kept and shown
                                                            small: somebody reading a 403 that
                                                            names `messages:send` has to be able
                                                            to find the box that grants it. */}
                                                        <code className="text-[11px] font-mono text-ink-400">
                                                            {permission.value}
                                                        </code>
                                                    </span>
                                                }
                                                description={permission.help}
                                                checked={scopes.includes(permission.value)}
                                                onChange={() => toggleScope(permission.value)}
                                            />
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}

                    {sensitiveCount > 0 && (
                        <p className="text-xs text-amber-700 mt-2 mb-0 flex items-start gap-1.5">
                            <TriangleAlert size={13} className="shrink-0 mt-0.5" aria-hidden="true" />
                            <span>
                                {sensitiveCount} of the permissions you have ticked can spend credits, message
                                your customers, or change data. Grant only what this integration needs.
                            </span>
                        </p>
                    )}
                </div>

                {accounts.length > 0 && (
                    <div>
                        <Toggle
                            checked={restrictAccounts}
                            onChange={(event) => setRestrictAccounts(event.target.checked)}
                            label="Restrict to specific WhatsApp numbers"
                            description="By default a key may send from any connected number in this workspace."
                        />
                        {restrictAccounts && (
                            <div className="mt-3 space-y-1.5 pl-1">
                                {accounts.map((account) => (
                                    <CheckboxField
                                        key={account.id}
                                        label={account.name}
                                        description={account.phone_number || "No number yet"}
                                        checked={allowedAccounts.includes(account.id)}
                                        onChange={() =>
                                            setAllowedAccounts((current) =>
                                                current.includes(account.id)
                                                    ? current.filter((id) => id !== account.id)
                                                    : [...current, account.id],
                                            )
                                        }
                                    />
                                ))}
                            </div>
                        )}
                    </div>
                )}

                <details className="border border-ink-200 rounded-lg p-4">
                    <summary className="text-sm font-semibold text-ink-800 cursor-pointer">
                        Rate limits
                    </summary>
                    <p className="text-xs text-ink-500 mt-2 mb-3">
                        Leave at 0 to use this deployment's defaults. These are ceilings for this key alone.
                    </p>
                    <div className="grid sm:grid-cols-3 gap-3">
                        <Field label="Requests / min">
                            <Input
                                type="number"
                                min="0"
                                value={limits.requests}
                                placeholder="0"
                                onChange={(event) => setLimits({ ...limits, requests: event.target.value })}
                            />
                        </Field>
                        <Field label="Messages / min">
                            <Input
                                type="number"
                                min="0"
                                value={limits.messages}
                                placeholder="0"
                                onChange={(event) => setLimits({ ...limits, messages: event.target.value })}
                            />
                        </Field>
                        <Field label="Campaigns / day">
                            <Input
                                type="number"
                                min="0"
                                value={limits.campaigns}
                                placeholder="0"
                                onChange={(event) => setLimits({ ...limits, campaigns: event.target.value })}
                            />
                        </Field>
                    </div>
                </details>
            </form>
        </Modal>
    );
}

function UsageModal({ apiKey, onClose }) {
    const [usage, setUsage] = useState(null);
    const [error, setError] = useState("");

    useEffect(() => {
        (async () => {
            try {
                const { data } = await api.get(`/developer/api-keys/${apiKey.id}/usage/?days=7`);
                setUsage(data);
            } catch (err) {
                setError(apiErrorMessage(err));
            }
        })();
    }, [apiKey.id]);

    return (
        <Modal title={`Usage — ${apiKey.name}`} description="Last 7 days" width="lg" onClose={onClose}>
            {error && <Alert tone="error">{error}</Alert>}
            {!usage && !error ? (
                <Spinner />
            ) : usage ? (
                <div className="space-y-5">
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                        {[
                            ["Requests", usage.totals.requests],
                            ["Messages", usage.totals.messages],
                            ["Errors", usage.totals.errors],
                            ["Rate limited", usage.totals.rate_limited],
                        ].map(([label, value]) => (
                            <div key={label} className="border border-ink-200 rounded-lg p-3">
                                <div className="text-xs text-ink-500">{label}</div>
                                <div className="text-xl font-bold text-ink-900">{value ?? 0}</div>
                            </div>
                        ))}
                    </div>

                    <div className="text-sm text-ink-600">
                        Lifetime: {usage.lifetime.requests} requests, {usage.lifetime.messages} messages.
                    </div>

                    {usage.top_endpoints?.length > 0 && (
                        <div>
                            <h3 className="text-sm font-bold text-ink-900 mb-2">Most used endpoints</h3>
                            <div className="space-y-1">
                                {usage.top_endpoints.map((row) => (
                                    <div
                                        key={`${row.method} ${row.path}`}
                                        className="flex items-center justify-between text-sm py-1 border-b border-ink-100 last:border-0"
                                    >
                                        <code className="text-xs text-ink-600 truncate">
                                            {row.method} {row.path}
                                        </code>
                                        <span className="text-ink-700 shrink-0 ml-3">
                                            {row.count}
                                            {row.errors > 0 && (
                                                <span className="text-red-600"> ({row.errors} failed)</span>
                                            )}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {usage.top_errors?.length > 0 && (
                        <div>
                            <h3 className="text-sm font-bold text-ink-900 mb-2">Most common errors</h3>
                            <div className="flex flex-wrap gap-2">
                                {usage.top_errors.map((row) => (
                                    <Badge key={row.error_code} tone="red">
                                        {row.error_code} × {row.count}
                                    </Badge>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            ) : null}
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

function WebhooksTab() {
    const toast = useToast();
    const [endpoints, setEndpoints] = useState(null);
    const [deliveries, setDeliveries] = useState([]);
    const [error, setError] = useState("");
    const [editing, setEditing] = useState(null);
    const [revealed, setRevealed] = useState(null);
    const [confirm, setConfirm] = useState(null);

    const load = async () => {
        try {
            const [endpointResponse, deliveryResponse] = await Promise.all([
                api.get("/developer/webhooks/"),
                api.get("/developer/webhooks/deliveries/"),
            ]);
            setEndpoints(endpointResponse.data.results ?? endpointResponse.data);
            setDeliveries(deliveryResponse.data.results ?? []);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err));
            setEndpoints([]);
        }
    };

    useEffect(() => {
        load();
    }, []);

    const test = async (endpoint) => {
        try {
            await api.post(`/developer/webhooks/${endpoint.id}/test/`);
            toast.success("Test delivery queued — check the log below in a moment.");
            setTimeout(load, 1500);
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    const toggle = async (endpoint) => {
        try {
            await api.patch(`/developer/webhooks/${endpoint.id}/`, {
                url: endpoint.url,
                is_active: !endpoint.is_active,
            });
            toast.success(endpoint.is_active ? "Webhook disabled." : "Webhook enabled.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    const rotate = async (endpoint) => {
        try {
            const { data } = await api.post(`/developer/webhooks/${endpoint.id}/rotate-secret/`);
            setRevealed({
                title: "New signing secret",
                description: `${endpoint.url} — the previous secret stopped working immediately.`,
                secret: data.secret,
            });
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    const remove = async (endpoint) => {
        try {
            await api.delete(`/developer/webhooks/${endpoint.id}/`);
            toast.success("Webhook deleted.");
            await load();
        } catch (err) {
            toast.error(apiErrorMessage(err));
        }
    };

    if (endpoints === null) return <Spinner />;

    return (
        <div className="space-y-6">
            {error && <Alert tone="error">{error}</Alert>}

            <div className="flex justify-end">
                <Button icon={Plus} onClick={() => setEditing({})}>
                    Add webhook
                </Button>
            </div>

            {endpoints.length === 0 ? (
                <EmptyState
                    icon={Webhook}
                    title="No webhooks yet"
                    description="Add an endpoint and we'll push delivery receipts, replies and button taps to it instead of you polling."
                    action={
                        <Button icon={Plus} onClick={() => setEditing({})}>
                            Add webhook
                        </Button>
                    }
                />
            ) : (
                <div className="space-y-3">
                    {endpoints.map((endpoint) => (
                        <Card key={endpoint.id}>
                            <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <span className="font-semibold text-ink-900">
                                            {endpoint.name || "Unnamed endpoint"}
                                        </span>
                                        <Badge tone={endpoint.is_active ? "green" : "gray"}>
                                            {endpoint.is_active ? "Enabled" : "Disabled"}
                                        </Badge>
                                        {endpoint.auto_disabled_at && (
                                            <Badge tone="red">Auto-disabled after repeated failures</Badge>
                                        )}
                                    </div>
                                    <code className="block text-xs text-ink-500 font-mono mt-1 break-all">
                                        {endpoint.url}
                                    </code>
                                    <div className="text-xs text-ink-500 mt-2">
                                        {endpoint.events?.length
                                            ? `${endpoint.events.length} event${endpoint.events.length === 1 ? "" : "s"}: ${endpoint.events.join(", ")}`
                                            : "All events"}
                                    </div>
                                    {endpoint.last_error && (
                                        <div className="text-xs text-red-600 mt-1">
                                            Last error: {endpoint.last_error}
                                        </div>
                                    )}
                                    <div className="text-xs text-ink-400 mt-1">
                                        Last success {formatRelative(endpoint.last_success_at)}
                                        {endpoint.consecutive_failures > 0 &&
                                            ` · ${endpoint.consecutive_failures} consecutive failures`}
                                    </div>
                                </div>

                                <div className="flex items-center gap-1 shrink-0">
                                    <IconButton
                                        icon={Send}
                                        title="Send a test delivery"
                                        aria-label="Send a test delivery"
                                        onClick={() => test(endpoint)}
                                    />
                                    <IconButton
                                        icon={RefreshCw}
                                        title="Rotate signing secret"
                                        aria-label="Rotate signing secret"
                                        onClick={() =>
                                            setConfirm({
                                                title: "Rotate the signing secret?",
                                                message:
                                                    "A new secret is issued and the old one stops verifying immediately. " +
                                                    "Deliveries will fail your signature check until you update it.",
                                                confirmLabel: "Rotate",
                                                onConfirm: () => rotate(endpoint),
                                            })
                                        }
                                    />
                                    <IconButton
                                        icon={endpoint.is_active ? Pause : Play}
                                        title={endpoint.is_active ? "Disable" : "Enable"}
                                        aria-label={endpoint.is_active ? "Disable" : "Enable"}
                                        onClick={() => toggle(endpoint)}
                                    />
                                    <IconButton
                                        icon={Trash2}
                                        title="Delete"
                                        aria-label="Delete webhook"
                                        onClick={() =>
                                            setConfirm({
                                                title: "Delete this webhook?",
                                                message: `Events will stop being delivered to ${endpoint.url}.`,
                                                confirmLabel: "Delete",
                                                onConfirm: () => remove(endpoint),
                                            })
                                        }
                                    />
                                </div>
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            <Card padded={false}>
                <div className="px-5 sm:px-6 pt-5 pb-4">
                    <CardHeader
                        title="Delivery log"
                        description="Every attempt, with the status your endpoint returned."
                        action={
                            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={load}>
                                Refresh
                            </Button>
                        }
                    />
                </div>
                {deliveries.length === 0 ? (
                    <div className="px-5 sm:px-6 pb-6 text-sm text-ink-500">No deliveries yet.</div>
                ) : (
                    <DataTable
                        columns={[
                            {
                                key: "event",
                                header: "Event",
                                render: (row) => <code className="text-xs">{row.event_type}</code>,
                            },
                            {
                                key: "status",
                                header: "Status",
                                render: (row) => <StatusBadge status={row.status} />,
                            },
                            {
                                key: "http",
                                header: "HTTP",
                                hideBelow: "sm",
                                render: (row) => (
                                    <span
                                        className={
                                            row.http_status >= 400 || row.http_status === null
                                                ? "text-red-600"
                                                : "text-ink-700"
                                        }
                                    >
                                        {row.http_status ?? "—"}
                                    </span>
                                ),
                            },
                            {
                                key: "attempts",
                                header: "Attempt",
                                hideBelow: "md",
                                render: (row) => row.attempt_count,
                            },
                            {
                                key: "response_ms",
                                header: "Time",
                                hideBelow: "lg",
                                render: (row) => (row.response_ms == null ? "—" : `${row.response_ms} ms`),
                            },
                            {
                                key: "created_at",
                                header: "When",
                                render: (row) => (
                                    <span className="text-sm text-ink-600">{formatRelative(row.created_at)}</span>
                                ),
                            },
                            {
                                key: "error",
                                header: "Error",
                                hideBelow: "xl",
                                render: (row) => (
                                    <span className="text-xs text-red-600 truncate max-w-[220px] block" title={row.error}>
                                        {row.error || ""}
                                    </span>
                                ),
                            },
                        ]}
                        rows={deliveries}
                    />
                )}
            </Card>

            {editing && (
                <WebhookModal
                    onClose={() => setEditing(null)}
                    onSaved={(data) => {
                        setEditing(null);
                        if (data?.secret) {
                            setRevealed({
                                title: "Your webhook signing secret",
                                description: "Use it to verify the X-SendStack-Signature header.",
                                secret: data.secret,
                            });
                        }
                        load();
                    }}
                />
            )}

            {revealed && <SecretRevealDialog {...revealed} onClose={() => setRevealed(null)} />}

            {confirm && (
                <ConfirmDialog
                    {...confirm}
                    onClose={() => setConfirm(null)}
                    onConfirm={async () => {
                        await confirm.onConfirm();
                        setConfirm(null);
                    }}
                />
            )}
        </div>
    );
}

function WebhookModal({ onClose, onSaved }) {
    const [name, setName] = useState("");
    const [url, setUrl] = useState("");
    const [events, setEvents] = useState([]);
    const [available, setAvailable] = useState(null);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        (async () => {
            try {
                const { data } = await api.get("/developer/webhooks/events/");
                setAvailable(data.events);
            } catch (err) {
                setError(apiErrorMessage(err));
                setAvailable([]);
            }
        })();
    }, []);

    const submit = async (event) => {
        event.preventDefault();
        setError("");
        setSaving(true);
        try {
            const { data } = await api.post("/developer/webhooks/", {
                name: name.trim(),
                url: url.trim(),
                events,
            });
            onSaved(data);
        } catch (err) {
            setError(apiErrorMessage(err));
        } finally {
            setSaving(false);
        }
    };

    return (
        <Modal
            title="Add webhook"
            description="We POST a signed JSON body to this URL whenever a subscribed event happens."
            width="lg"
            onClose={onClose}
            footer={
                <div className="flex justify-end gap-2">
                    <Button variant="secondary" onClick={onClose}>
                        Cancel
                    </Button>
                    <Button form="create-webhook" type="submit" loading={saving}>
                        Add webhook
                    </Button>
                </div>
            }
        >
            <form id="create-webhook" onSubmit={submit} className="space-y-5">
                {error && <Alert tone="error">{error}</Alert>}

                <Field label="Name" hint="Optional — just a label for this list.">
                    <Input
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                        placeholder="Order service"
                        maxLength={100}
                    />
                </Field>

                <Field
                    label="Endpoint URL"
                    required
                    hint="Must be https in production. The payload carries message content and is signed with a shared secret."
                >
                    <Input
                        type="url"
                        value={url}
                        onChange={(event) => setUrl(event.target.value)}
                        placeholder="https://your-app.com/webhooks/sendstack"
                        required
                    />
                </Field>

                <div>
                    <div className="flex items-center justify-between mb-2">
                        <label className="text-sm font-semibold text-ink-800">Events</label>
                        <Button type="button" variant="ghost" size="xs" onClick={() => setEvents([])}>
                            All events
                        </Button>
                    </div>
                    <p className="text-xs text-ink-500 mt-0 mb-3">
                        Tick nothing to receive everything, including events added later.
                    </p>
                    {available === null ? (
                        <Spinner />
                    ) : (
                        <div className="space-y-1.5 max-h-64 overflow-y-auto border border-ink-200 rounded-lg p-4">
                            {available.map((event) => (
                                <CheckboxField
                                    key={event.value}
                                    label={event.value}
                                    description={event.label}
                                    checked={events.includes(event.value)}
                                    onChange={() =>
                                        setEvents((current) =>
                                            current.includes(event.value)
                                                ? current.filter((item) => item !== event.value)
                                                : [...current, event.value],
                                        )
                                    }
                                />
                            ))}
                        </div>
                    )}
                </div>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Activity
// ---------------------------------------------------------------------------

function ActivityTab() {
    const [logs, setLogs] = useState(null);
    const [errorsOnly, setErrorsOnly] = useState(false);
    const [error, setError] = useState("");

    const load = async (only) => {
        try {
            const { data } = await api.get(`/developer/api-keys/logs/?errors_only=${only ? "true" : "false"}`);
            setLogs(data.results);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err));
            setLogs([]);
        }
    };

    useEffect(() => {
        load(errorsOnly);
    }, [errorsOnly]);

    if (logs === null) return <Spinner />;

    return (
        <div className="space-y-4">
            {error && <Alert tone="error">{error}</Alert>}

            <div className="flex items-center justify-between gap-3">
                <Toggle
                    checked={errorsOnly}
                    onChange={(event) => setErrorsOnly(event.target.checked)}
                    label="Failures only"
                    description="Show only calls that returned 4xx or 5xx."
                />
                <Button variant="secondary" size="sm" icon={RefreshCw} onClick={() => load(errorsOnly)}>
                    Refresh
                </Button>
            </div>

            {logs.length === 0 ? (
                <EmptyState
                    icon={Activity}
                    title={errorsOnly ? "No failed API calls" : "No API calls yet"}
                    description={
                        errorsOnly
                            ? "Nothing has returned an error recently."
                            : "Once something calls the API with one of your keys, its requests appear here."
                    }
                />
            ) : (
                <DataTable
                    columns={[
                        {
                            key: "created_at",
                            header: "When",
                            render: (row) => (
                                <span
                                    className="text-sm text-ink-600"
                                    title={new Date(row.created_at).toLocaleString()}
                                >
                                    {formatRelative(row.created_at)}
                                </span>
                            ),
                        },
                        {
                            key: "key_prefix",
                            header: "Token",
                            hideBelow: "md",
                            render: (row) => (
                                <div className="min-w-0">
                                    {/* The name is what somebody recognises --
                                        "Production API" says which integration
                                        made the call; a bare prefix does not.
                                        The masked key beside it is never more
                                        than the prefix. */}
                                    <div className="text-sm text-ink-700 truncate">
                                        {row.key_name || <span className="text-ink-400">Deleted token</span>}
                                    </div>
                                    <code className="text-xs text-ink-400 font-mono">{row.key_masked}</code>
                                </div>
                            ),
                        },
                        {
                            key: "user",
                            header: "User",
                            hideBelow: "xl",
                            render: (row) => (
                                <span className="text-sm text-ink-600">{row.user || "\u2014"}</span>
                            ),
                        },
                        {
                            key: "endpoint",
                            header: "Endpoint",
                            render: (row) => (
                                <code className="text-xs">
                                    {row.method} {row.path}
                                </code>
                            ),
                        },
                        {
                            key: "status_code",
                            header: "Status",
                            render: (row) => (
                                <Badge tone={row.status_code >= 400 ? "red" : "green"}>{row.status_code}</Badge>
                            ),
                        },
                        {
                            key: "error_code",
                            header: "Error",
                            hideBelow: "lg",
                            render: (row) =>
                                row.error_code ? (
                                    <code className="text-xs text-red-600">{row.error_code}</code>
                                ) : (
                                    <span className="text-ink-400">—</span>
                                ),
                        },
                        {
                            key: "message_id",
                            header: "Result",
                            hideBelow: "xl",
                            render: (row) =>
                                row.message_id || row.campaign_id ? (
                                    <code className="text-xs text-ink-600 font-mono">
                                        {row.message_id || row.campaign_id}
                                    </code>
                                ) : (
                                    <span className="text-ink-400">&mdash;</span>
                                ),
                        },
                        {
                            key: "ip_address",
                            header: "IP",
                            hideBelow: "xl",
                            render: (row) => (
                                <code className="text-xs text-ink-500 font-mono">
                                    {row.ip_address || "\u2014"}
                                </code>
                            ),
                        },
                        {
                            key: "duration_ms",
                            header: "Time",
                            hideBelow: "lg",
                            align: "right",
                            render: (row) => <span className="text-sm text-ink-600">{row.duration_ms} ms</span>,
                        },
                    ]}
                    rows={logs}
                />
            )}
        </div>
    );
}
