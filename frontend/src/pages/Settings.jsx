import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, Coins, ExternalLink, KeyRound, LogOut, ShieldCheck, Smartphone, UserRound } from "lucide-react";
import api, { apiErrorMessage } from "../services/api";
import { useAuth } from "../context/AuthContext";
import Button from "../components/ui/Button";
import Alert from "../components/ui/Alert";
import Badge from "../components/ui/Badge";
import Card, { CardHeader } from "../components/ui/Card";
import PageHeader from "../components/ui/PageHeader";
import { Toggle } from "../components/ui/Field";
import { useToast } from "../components/ui/Toast";

const COLLAPSE_KEY = "sidebar_collapsed";
const DENSITY_HINT = "Applies to this browser only.";

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

            <Section icon={ExternalLink} title="API" description="Programmatic access to your data.">
                <Card>
                    <CardHeader
                        title="REST API reference"
                        description="Interactive OpenAPI documentation for every endpoint this app uses."
                        action={
                            <a href="/api/docs/" target="_blank" rel="noreferrer" className="no-underline">
                                <Button variant="secondary" size="sm" icon={ExternalLink}>
                                    Open API docs
                                </Button>
                            </a>
                        }
                    />
                </Card>
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

                    <div className="mt-5 pt-5 border-t border-ink-100 flex flex-wrap items-center justify-between gap-3">
                        <div>
                            <p className="text-sm font-semibold text-ink-900 m-0">Sign out</p>
                            <p className="text-xs text-ink-500 mt-0.5 mb-0">
                                Ends this session on this device.
                            </p>
                        </div>
                        <Button variant="danger" size="sm" icon={LogOut} onClick={logout}>
                            Sign out
                        </Button>
                    </div>
                </Card>
            </Section>
        </div>
    );
}
