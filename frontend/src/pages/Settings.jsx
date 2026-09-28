import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, Coins, Database, ExternalLink, FileText, Gauge, KeyRound, LifeBuoy, LogOut, ShieldCheck, Smartphone, UserRound } from "lucide-react";
import api, { apiErrorMessage } from "../services/api";
import { useAuth } from "../context/AuthContext";
import { useBranding } from "../context/BrandingContext";
import Button from "../components/ui/Button";
import Alert from "../components/ui/Alert";
import Badge from "../components/ui/Badge";
import Card, { CardHeader } from "../components/ui/Card";
import PageHeader from "../components/ui/PageHeader";
import { Field, Input, Toggle } from "../components/ui/Field";
import { useToast } from "../components/ui/Toast";
import SendingSettings from "../components/settings/SendingSettings";
import StorageSettings from "../components/settings/StorageSettings";

const COLLAPSE_KEY = "sidebar_collapsed";
const DENSITY_HINT = "Applies to this browser only.";

/**
 * Change your own password without leaving the app.
 *
 * Sits alongside the "email me a reset link" button rather than replacing it:
 * the two answer different questions. This one is for somebody who knows their
 * password and wants a different one; the link is for somebody who does not.
 *
 * The current password is required, because an access token found on an
 * unlocked laptop is enough to reach this form and must not be enough to take
 * the account.
 */
function ChangePassword() {
    const toast = useToast();
    const [current, setCurrent] = useState("");
    const [next, setNext] = useState("");
    const [confirmation, setConfirmation] = useState("");
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const [open, setOpen] = useState(false);

    const reset = () => {
        setCurrent("");
        setNext("");
        setConfirmation("");
        setError("");
    };

    const submit = async (event) => {
        event.preventDefault();
        setError("");
        if (next !== confirmation) {
            setError("The two new passwords do not match.");
            return;
        }
        setSaving(true);
        try {
            await api.post("/auth/password-change/", { current_password: current, new_password: next });
            reset();
            setOpen(false);
            toast.success("Password updated. You have been signed out on your other devices.");
        } catch (err) {
            setError(apiErrorMessage(err, "That password could not be changed."));
        } finally {
            setSaving(false);
        }
    };

    if (!open) {
        return (
            <div className="mt-5 pt-5 border-t border-ink-100 flex flex-wrap items-center justify-between gap-3">
                <div>
                    <p className="text-sm font-semibold text-ink-900 m-0">Change password</p>
                    <p className="text-xs text-ink-500 mt-0.5 mb-0">Signs you out on every other device.</p>
                </div>
                <Button variant="secondary" size="sm" icon={KeyRound} onClick={() => setOpen(true)}>
                    Change password
                </Button>
            </div>
        );
    }

    return (
        <form onSubmit={submit} className="mt-5 pt-5 border-t border-ink-100">
            <p className="text-sm font-semibold text-ink-900 m-0 mb-3">Change password</p>
            <Alert>{error}</Alert>

            <Field label="Current password">
                <Input
                    type="password"
                    value={current}
                    onChange={(event) => setCurrent(event.target.value)}
                    autoComplete="current-password"
                    required
                />
            </Field>
            <Field label="New password">
                <Input
                    type="password"
                    value={next}
                    onChange={(event) => setNext(event.target.value)}
                    placeholder="At least 12 characters"
                    autoComplete="new-password"
                    required
                />
            </Field>
            <Field label="Confirm new password">
                <Input
                    type="password"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    autoComplete="new-password"
                    required
                />
            </Field>

            <div className="flex gap-2">
                <Button type="submit" size="sm" loading={saving}>
                    {saving ? "Updating..." : "Update password"}
                </Button>
                <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                        reset();
                        setOpen(false);
                    }}
                >
                    Cancel
                </Button>
            </div>
        </form>
    );
}


function DefinitionRow({ label, children }) {
    return (
        <div className="flex flex-wrap items-center justify-between gap-2 py-3 border-b border-ink-100 last:border-0">
            <dt className="text-sm text-ink-500 m-0">{label}</dt>
            <dd className="text-sm font-semibold text-ink-900 m-0 text-right break-all">{children}</dd>
        </div>
    );
}

function Section({ icon: Icon, title, description, children }) {
    return (
        <section className="mb-6">
            <div className="flex items-start gap-3 mb-3">
                <span className="shrink-0 w-8 h-8 rounded-lg bg-ink-100 text-ink-500 flex items-center justify-center">
                    <Icon size={16} aria-hidden="true" />
                </span>
                <div>
                    <h2 className="text-sm font-bold text-ink-900 m-0">{title}</h2>
                    {description && <p className="text-xs text-ink-500 mt-0.5 mb-0">{description}</p>}
                </div>
            </div>
            {children}
        </section>
    );
}

export default function Settings() {
    const { user, logout } = useAuth();
    const toast = useToast();

    const [error, setError] = useState("");
    const [resetSent, setResetSent] = useState(false);
    const [sendingReset, setSendingReset] = useState(false);
    const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === "1");

    // The sidebar reads this key on mount, so writing it here keeps the two
    // in sync without a shared store.
    useEffect(() => {
        localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    }, [collapsed]);

    const requestPasswordReset = async () => {
        if (!user?.email) {
            setError("Your account has no email address, so a reset link cannot be sent.");
            return;
        }
        setSendingReset(true);
        setError("");
        try {
            await api.post("/auth/password-reset/", { email: user.email });
            setResetSent(true);
            toast.success("Password reset link sent.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to send a password reset link."));
        } finally {
            setSendingReset(false);
        }
    };

    return (
        <div className="max-w-3xl">
            <PageHeader title="Settings" description="Your account, workspace and security preferences." />

            <Alert>{error}</Alert>

            <Section icon={UserRound} title="Account" description="Details of the account you are signed in with.">
                <Card>
                    <dl className="m-0">
                        <DefinitionRow label="Username">{user?.username}</DefinitionRow>
                        <DefinitionRow label="Email">{user?.email || "—"}</DefinitionRow>
                        <DefinitionRow label="Role">
                            <Badge tone="blue">{user?.role}</Badge>
                        </DefinitionRow>
                        <DefinitionRow label="Business">{user?.tenant_name}</DefinitionRow>
                    </dl>
                </Card>
            </Section>

            <Section icon={Coins} title="Credits" description="Message credits available to this account.">
                <Card>
                    <dl className="m-0">
                        <DefinitionRow label="Total credits">
                            <span className="tabular-nums">{user?.credits ?? "—"}</span>
                        </DefinitionRow>
                        <DefinitionRow label="Reserved">
                            <span className="tabular-nums">{user?.reserved_credits ?? "—"}</span>
                        </DefinitionRow>
                        <DefinitionRow label="Available">
                            <span className="tabular-nums">{user?.available_credits ?? "—"}</span>
                        </DefinitionRow>
                    </dl>
                    <div className="mt-4 pt-4 border-t border-ink-100">
                        <Link to="/credits" className="no-underline">
                            <Button variant="secondary" size="sm">
                                View transaction ledger
                            </Button>
                        </Link>
                    </div>
                </Card>
            </Section>

            <Section icon={Smartphone} title="Messaging" description="Where your campaigns and automations send from.">
                <Card>
                    <CardHeader
                        title="WhatsApp devices"
                        description="Connect, reconnect or remove the WhatsApp accounts that deliver your messages."
                        action={
                            <Link to="/whatsapp" className="no-underline">
                                <Button variant="secondary" size="sm">
                                    Manage devices
                                </Button>
                            </Link>
                        }
                    />
                </Card>
            </Section>

            <Section
                icon={Gauge}
                title="Sending"
                description="How fast this workspace sends, and what happens when a message fails."
            >
                <SendingSettings />
            </Section>

            <Section
                icon={Database}
                title="Storage"
                description="What this workspace is storing, and how long finished campaigns are kept."
            >
                <StorageSettings />
            </Section>

            <Section
                icon={ShieldCheck}
                title="Compliance"
                description="Opt-out handling and the hours your workspace is allowed to send in."
            >
                <Card>
                    <CardHeader
                        title="Consent and quiet hours"
                        description="Stop words, unsubscribe confirmations, sending hours, and the full consent history."
                        action={
                            <Link to="/compliance" className="no-underline">
                                <Button variant="secondary" size="sm">
                                    Open compliance
                                </Button>
                            </Link>
                        }
                    />
                </Card>
            </Section>

            <Section icon={BookOpen} title="Appearance" description="How the app looks in this browser.">
                <Card>
                    <Toggle
                        label="Collapsed sidebar"
                        description={`Show only icons in the navigation. ${DENSITY_HINT}`}
                        checked={collapsed}
                        onChange={(event) => setCollapsed(event.target.checked)}
                    />
                </Card>
            </Section>

            <Section
                icon={ExternalLink}
                title="Developer"
                description="Let your own software send through this workspace, and receive events back."
            >
                <div className="space-y-4">
                    <Card>
                        <CardHeader
                            title="API keys and webhooks"
                            description="Create scoped API keys for external applications, and register endpoints for delivery receipts and replies."
                            action={
                                <Link to="/developer" className="no-underline">
                                    <Button variant="secondary" size="sm" icon={KeyRound}>
                                        Open developer settings
                                    </Button>
                                </Link>
                            }
                        />
                    </Card>

                    <Card>
                        <CardHeader
                            title="Messaging API reference"
                            description="How to authenticate, send, use templates and campaigns, and verify webhook signatures. With examples in curl, Python, JavaScript, PHP and C#."
                            action={
                                <a href="/api/public/v1/docs" target="_blank" rel="noreferrer" className="no-underline">
                                    <Button variant="secondary" size="sm" icon={ExternalLink}>
                                        Open API docs
                                    </Button>
                                </a>
                            }
                        />
                    </Card>

                    <Card>
                        <CardHeader
                            title="Internal REST reference"
                            description="The endpoints this dashboard itself uses. Not the stable public contract -- integrate against the Messaging API above."
                            action={
                                <a href="/api/docs/" target="_blank" rel="noreferrer" className="no-underline">
                                    <Button variant="secondary" size="sm" icon={ExternalLink}>
                                        Open internal docs
                                    </Button>
                                </a>
                            }
                        />
                    </Card>
                </div>
            </Section>

            <Section icon={KeyRound} title="Security" description="Password and session controls.">
                <Card>
                    <CardHeader
                        title="Password"
                        description={
                            resetSent
                                ? "A reset link has been emailed to you. It expires after a short time."
                                : `We'll email a password reset link to ${user?.email || "your account email"}.`
                        }
                        action={
                            <Button
                                variant="secondary"
                                size="sm"
                                icon={KeyRound}
                                onClick={requestPasswordReset}
                                loading={sendingReset}
                                disabled={resetSent}
                            >
                                {resetSent ? "Link sent" : "Send reset link"}
                            </Button>
                        }
                    />

                    <ChangePassword />

                    <div className="mt-5 pt-5 border-t border-ink-100 flex flex-wrap items-center justify-between gap-3">
                        <div>
                            <p className="text-sm font-semibold text-ink-900 m-0">Sign out</p>
                            <p className="text-xs text-ink-500 mt-0.5 mb-0">
                                Ends this session on this device, and revokes it on the server.
                            </p>
                        </div>
                        <Button variant="danger" size="sm" icon={LogOut} onClick={logout}>
                            Sign out
                        </Button>
                    </div>
                </Card>
            </Section>

            <Section
                icon={FileText}
                title="Your data"
                description="Download everything in this workspace, and read the terms it is held under."
            >
                <Card>
                    <CardHeader
                        title="Export your data"
                        description="A single JSON file with your contacts, consent records, campaigns, messages, templates, CRM records and settings. It never contains passwords, tokens or WhatsApp credentials."
                        action={
                            <a href="/api/v1/tenants/export/" className="no-underline">
                                <Button variant="secondary" size="sm" icon={ExternalLink}>
                                    Download export
                                </Button>
                            </a>
                        }
                    />

                    <div className="mt-5 pt-5 border-t border-ink-100 flex flex-wrap items-center gap-4 text-sm">
                        <Link to="/terms" className="text-emerald-600 hover:text-emerald-700">
                            Terms of Service
                        </Link>
                        <Link to="/privacy" className="text-emerald-600 hover:text-emerald-700">
                            Privacy Policy
                        </Link>
                        <Link to="/dpa" className="text-emerald-600 hover:text-emerald-700">
                            Data Processing Agreement
                        </Link>
                    </div>
                </Card>
            </Section>

            <Section icon={LifeBuoy} title="Support" description="Where to go when something is wrong.">
                <Card>
                    <SupportContact />
                </Card>
            </Section>
        </div>
    );
}

/**
 * Where to get help.
 *
 * Read from the workspace's own branding, so a reseller's customer is sent to
 * the reseller and not to the platform. Nothing here is hardcoded -- a blank
 * support address renders as an honest "not configured" rather than as
 * somebody's personal inbox baked into the bundle.
 */
function SupportContact() {
    const { branding } = useBranding();
    const email = branding?.support_email;

    if (!email) {
        return (
            <p className="text-sm text-ink-500 m-0">
                No support contact has been configured for this workspace yet. Ask whoever
                administers it to set one.
            </p>
        );
    }

    return (
        <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
                <p className="text-sm font-semibold text-ink-900 m-0">Contact support</p>
                <p className="text-xs text-ink-500 mt-0.5 mb-0">
                    Quote the reference shown with any error message -- it lets us find the exact request.
                </p>
            </div>
            <a href={"mailto:" + email} className="no-underline">
                <Button variant="secondary" size="sm" icon={LifeBuoy}>
                    {email}
                </Button>
            </a>
        </div>
    );
}
