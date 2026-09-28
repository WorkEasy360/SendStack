import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
    ArrowRight,
    CheckCircle2,
    LifeBuoy,
    MessageSquare,
    RefreshCw,
    ShieldCheck,
    Smartphone,
    Users,
} from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useBranding } from "../../context/BrandingContext";
import { useToast } from "../../components/ui/Toast";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import WhatsAppConnectionWizard from "../../components/WhatsAppConnectionWizard";

/**
 * The guided first run.
 *
 * Not a wizard that traps anybody: every step links into the real screen that
 * does the job, the customer can leave at any point, and coming back tomorrow
 * resumes exactly where they were -- because the progress is derived
 * server-side from what the workspace contains rather than held in this
 * component (see apps/onboarding/services.py).
 *
 * The WhatsApp step reuses the existing connection wizard rather than
 * rebuilding the QR flow. There is one implementation of scanning a code, and
 * it is the one that already handles expiry, retries and provider failures.
 */
function Onboarding() {
    const navigate = useNavigate();
    const toast = useToast();
    const { branding } = useBranding();

    const [state, setState] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [refreshing, setRefreshing] = useState(false);
    const [wizardOpen, setWizardOpen] = useState(false);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (!quiet) setRefreshing(true);
        try {
            const response = await api.get("/onboarding/");
            setState(response.data);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Your setup progress could not be loaded."));
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        load({ quiet: true });
    }, [load]);

    const skip = async () => {
        try {
            await api.post("/onboarding/", { action: "dismiss" });
        } catch {
            // Hiding a checklist is not worth blocking somebody over.
        }
        navigate("/", { replace: true });
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center py-20">
                <Spinner />
            </div>
        );
    }

    const steps = state?.steps || [];
    const byKey = Object.fromEntries(steps.map((step) => [step.key, step]));
    const complete = state?.is_complete;
    const trial = state?.trial;

    return (
        <div className="max-w-3xl">
            <PageHeader
                title={complete ? "You are all set" : `Welcome to ${branding.product_name}`}
                description={
                    complete
                        ? "Everything is connected. This page stays here if you want to check anything."
                        : "Five short steps and you will have sent your first message."
                }
                actions={
                    <>
                        <Button
                            variant="ghost"
                            size="sm"
                            icon={RefreshCw}
                            onClick={() => load()}
                            loading={refreshing}
                        >
                            Refresh
                        </Button>
                        <Button variant="secondary" size="sm" onClick={skip}>
                            {complete ? "Go to dashboard" : "Skip for now"}
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            {trial?.active && trial.days_remaining !== null && (
                <Card className="mb-5 bg-brand-50 border-brand-200">
                    <p className="text-sm text-ink-800 m-0">
                        <strong>
                            {trial.days_remaining} day{trial.days_remaining === 1 ? "" : "s"} left
                        </strong>{" "}
                        in your free trial. Everything you create is yours to keep whatever you decide at the end.
                    </p>
                </Card>
            )}

            {trial && trial.active === false && trial.access_state === "EXPIRED" && (
                <Card className="mb-5 border-amber-200 bg-amber-50">
                    <p className="text-sm text-amber-900 m-0">
                        Your trial has ended, so sending is paused. <strong>Nothing has been deleted.</strong>{" "}
                        <Link to="/billing" className="font-semibold text-amber-900 underline">
                            Choose a plan
                        </Link>{" "}
                        to start sending again.
                    </p>
                </Card>
            )}

            <div className="space-y-4">
                <Step
                    number={1}
                    icon={ShieldCheck}
                    title="Accept the terms"
                    done={byKey.legal_accepted?.done}
                    description="You did this when you created your workspace."
                />

                <Step
                    number={2}
                    icon={Smartphone}
                    title="Connect WhatsApp"
                    done={byKey.whatsapp_connected?.done}
                    description="Link the number you send from. It stays on your phone; we only relay messages."
                    action={
                        <Button onClick={() => setWizardOpen(true)} icon={Smartphone}>
                            Connect a number
                        </Button>
                    }
                >
                    {/*
                      The instructions, spelled out. Somebody who has never
                      linked a device does not know that "Linked devices" is
                      inside the three-dot menu, and a QR code with no
                      explanation beside it is where onboarding stalls.
                    */}
                    <ol className="text-sm text-ink-600 list-decimal pl-5 space-y-1 mt-3 mb-0">
                        <li>Open WhatsApp on the phone that owns the number.</li>
                        <li>
                            Go to <strong>Settings → Linked devices</strong>, then{" "}
                            <strong>Link a device</strong>.
                        </li>
                        <li>Scan the QR code we show you.</li>
                        <li>Wait for it to say connected — that comes from our side, not the phone.</li>
                    </ol>
                </Step>

                <Step
                    number={3}
                    icon={Users}
                    title="Add your contacts"
                    done={byKey.contacts_added?.done}
                    description="Add one by hand to try it, or import a CSV. You will be asked about consent — answer honestly; it decides who we may market to."
                    action={
                        <Link to="/contacts" className="no-underline">
                            <Button variant="secondary" icon={Users}>Go to contacts</Button>
                        </Link>
                    }
                />

                <Step
                    number={4}
                    icon={CheckCircle2}
                    title="Confirm consent"
                    done={byKey.consent_confirmed?.done}
                    description="At least one contact you are allowed to send marketing to. Anyone who has opted out stays opted out, whatever a later import says."
                    action={
                        <Link to="/compliance" className="no-underline">
                            <Button variant="secondary">Consent settings</Button>
                        </Link>
                    }
                />

                <Step
                    number={5}
                    icon={MessageSquare}
                    title="Send a test message"
                    done={byKey.test_message_sent?.done}
                    description="One real message to a number you own. It goes through every check a campaign does — there is no shortcut for onboarding."
                    action={
                        <Link to="/inbox" className="no-underline">
                            <Button variant="secondary" icon={MessageSquare}>Open the inbox</Button>
                        </Link>
                    }
                />

                <Step
                    number={6}
                    icon={ArrowRight}
                    title="Create your first campaign"
                    done={byKey.first_campaign_created?.done}
                    description="Now the real thing. Start small — a handful of recipients you know."
                    action={
                        <Link to="/campaigns/new" className="no-underline">
                            <Button variant="secondary">New campaign</Button>
                        </Link>
                    }
                />
            </div>

            {complete && (
                <Card className="mt-5 text-center">
                    <CheckCircle2 size={28} className="text-brand-600 mx-auto mb-2" aria-hidden="true" />
                    <h2 className="text-base font-bold text-ink-900 m-0 mb-1">Setup complete</h2>
                    <p className="text-sm text-ink-500 mb-4">Your workspace is fully up and running.</p>
                    <Link to="/" className="no-underline">
                        <Button size="lg">Go to your dashboard</Button>
                    </Link>
                </Card>
            )}

            {/*
              A way out of every stuck state. Somebody whose QR never scans and
              whose import keeps failing must be able to reach a person, not
              re-read the same step.
            */}
            {(branding.support_email || branding.support_url) && (
                <Card className="mt-5">
                    <CardHeader
                        title="Stuck on something?"
                        description="We would rather hear about it than have you give up on a step."
                        className="mb-3"
                    />
                    <div className="flex flex-wrap gap-2">
                        {branding.support_email && (
                            <a href={`mailto:${branding.support_email}`} className="no-underline">
                                <Button variant="secondary" size="sm" icon={LifeBuoy}>
                                    Email {branding.support_email}
                                </Button>
                            </a>
                        )}
                        {branding.support_url && (
                            <a
                                href={branding.support_url}
                                target="_blank"
                                rel="noreferrer"
                                className="no-underline"
                            >
                                <Button variant="secondary" size="sm">Help centre</Button>
                            </a>
                        )}
                    </div>
                </Card>
            )}

            {wizardOpen && (
                <WhatsAppConnectionWizard
                    onClose={() => setWizardOpen(false)}
                    onConnected={() => {
                        // Re-read from the server rather than assuming. The
                        // wizard closing means the wizard finished, not that
                        // the backend has recorded a connected account.
                        load();
                        toast.success("Number connected.");
                    }}
                />
            )}
        </div>
    );
}

function Step({ number, icon: Icon, title, description, done, action, children }) {
    return (
        <Card className={done ? "border-brand-200 bg-brand-50/30" : ""}>
            <div className="flex items-start gap-4">
                <div
                    className={`w-9 h-9 shrink-0 rounded-full flex items-center justify-center ${
                        done ? "bg-brand-600 text-white" : "bg-ink-100 text-ink-500"
                    }`}
                >
                    {done ? (
                        <CheckCircle2 size={19} aria-hidden="true" />
                    ) : (
                        <Icon size={18} aria-hidden="true" />
                    )}
                </div>

                <div className="min-w-0 grow">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                        <h2 className="text-base font-bold text-ink-900 m-0">
                            <span className="text-ink-400 font-semibold mr-1.5">{number}.</span>
                            {title}
                        </h2>
                        {done && (
                            <span className="text-xs font-semibold text-brand-700 uppercase tracking-wide">
                                Done
                            </span>
                        )}
                    </div>
                    {description && <p className="text-sm text-ink-500 mt-1.5 mb-0">{description}</p>}

                    {!done && children}

                    {!done && action && <div className="mt-4">{action}</div>}
                </div>
            </div>
        </Card>
    );
}

export default Onboarding;
