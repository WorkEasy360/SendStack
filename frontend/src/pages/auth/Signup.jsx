import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AtSign, Building2, Lock, User } from "lucide-react";
import { useAuth } from "../../context/AuthContext";
import { useBranding } from "../../context/BrandingContext";
import api from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Field";
import Spinner from "../../components/ui/Spinner";
import { LogoMark } from "../../components/layout/Logo";

/**
 * Create a workspace.
 *
 * Four fields, because four is what the backend needs and asking for more
 * would be collecting data the product has no use for yet. No phone number,
 * no card, no team size -- those are asked for at the point they are needed,
 * if ever.
 *
 * Two things this page deliberately does **not** do:
 *
 * * It does not know how long the trial is. It asks
 *   `GET /auth/signup/` and prints what comes back, so the number on this page
 *   and the number the send gate enforces cannot drift apart.
 * * It does not pre-tick anything. Accepting the Terms is an unticked box the
 *   customer has to tick, and the marketing box is separate, unticked, and
 *   optional. Bundling them would make the acceptance record evidence of
 *   something it is not.
 */
function Signup() {
    const navigate = useNavigate();
    const { signup } = useAuth();
    const { branding, reloadBranding } = useBranding();
    const [searchParams] = useSearchParams();

    const [offer, setOffer] = useState(null);
    const [loadingOffer, setLoadingOffer] = useState(true);

    const [fullName, setFullName] = useState("");
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [company, setCompany] = useState("");
    const [acceptTerms, setAcceptTerms] = useState(false);
    const [marketingOptIn, setMarketingOptIn] = useState(false);

    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState("");
    const [fieldErrors, setFieldErrors] = useState({});

    // The plan the visitor clicked on the pricing page, carried as a query
    // parameter. It is only ever a *preference*: the server matches it against
    // its own public plans and falls back to the trial plan for anything it
    // does not recognise, so a stale or edited link cannot buy anything.
    const requestedPlan = searchParams.get("plan") || "";

    useEffect(() => {
        reloadBranding();
    }, [reloadBranding]);

    useEffect(() => {
        let cancelled = false;
        api.get("/auth/signup/")
            .then((response) => {
                if (!cancelled) setOffer(response.data);
            })
            .catch(() => {
                // Treat an unreachable endpoint as "we cannot tell", and let
                // the form render. The POST is the authority either way.
                if (!cancelled) setOffer(null);
            })
            .finally(() => {
                if (!cancelled) setLoadingOffer(false);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError("");
        setFieldErrors({});
        setSubmitting(true);

        const result = await signup({
            full_name: fullName,
            email,
            password,
            company,
            plan: requestedPlan,
            accept_terms: acceptTerms,
            marketing_opt_in: marketingOptIn,
        });
        setSubmitting(false);

        if (result.success) {
            reloadBranding();
            navigate(result.next, { replace: true });
            return;
        }

        setError(result.message);
        // Field-level messages where DRF gave them, so "this password is too
        // common" lands next to the password box rather than at the top.
        if (result.fields && typeof result.fields === "object") {
            const { detail, code, ...fields } = result.fields;
            setFieldErrors(fields);
        }
    };

    const fieldError = (name) => {
        const value = fieldErrors[name];
        if (!value) return "";
        return Array.isArray(value) ? value.join(" ") : String(value);
    };

    if (loadingOffer) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-ink-50">
                <Spinner />
            </div>
        );
    }

    // Signups closed. Said plainly with a way forward, rather than a broken
    // form or a 404 -- somebody arriving here wants an account and there is
    // still a way to get one.
    if (offer && offer.signup_enabled === false) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-ink-50 px-4 py-10">
                <div className="w-full max-w-sm text-center">
                    <LogoMark size={44} />
                    <h1 className="text-2xl font-bold text-ink-900 mt-4 mb-2">{branding.product_name}</h1>
                    <p className="text-sm text-ink-500 mb-6">
                        Accounts here are opened by our team rather than online.
                        {branding.support_email ? " Get in touch and we will set you up." : ""}
                    </p>
                    {branding.support_email && (
                        <Button
                            size="lg"
                            className="w-full mb-3"
                            onClick={() => {
                                window.location.href = `mailto:${branding.support_email}`;
                            }}
                        >
                            Email {branding.support_email}
                        </Button>
                    )}
                    <Link to="/login" className="block text-sm text-ink-500 hover:text-ink-700">
                        Already have an account? Sign in
                    </Link>
                </div>
            </div>
        );
    }

    const trialDays = offer?.trials_enabled ? offer.trial_days : 0;

    return (
        <div className="min-h-screen flex items-center justify-center bg-ink-50 px-4 py-10">
            <div className="w-full max-w-md">
                <div className="flex flex-col items-center text-center mb-8">
                    <LogoMark size={44} />
                    <h1 className="text-2xl font-bold text-ink-900 mt-4 mb-1">
                        Create your {branding.product_name} workspace
                    </h1>
                    <p className="text-sm text-ink-500 m-0">
                        {trialDays > 0
                            ? `Start with a ${trialDays}-day free trial. No card needed.`
                            : "Set up your workspace in a minute."}
                    </p>
                </div>

                <div className="bg-surface border border-ink-200 rounded-card shadow-raised p-6 sm:p-7">
                    <form onSubmit={handleSubmit} noValidate>
                        <Alert>{error}</Alert>

                        <Field label="Your name" error={fieldError("full_name")}>
                            <div className="relative">
                                <User
                                    size={16}
                                    aria-hidden="true"
                                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                />
                                <Input
                                    type="text"
                                    value={fullName}
                                    onChange={(event) => setFullName(event.target.value)}
                                    placeholder="Priya Sharma"
                                    autoComplete="name"
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        <Field label="Business name" error={fieldError("company")}>
                            <div className="relative">
                                <Building2
                                    size={16}
                                    aria-hidden="true"
                                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                />
                                <Input
                                    type="text"
                                    value={company}
                                    onChange={(event) => setCompany(event.target.value)}
                                    placeholder="Sharma Traders"
                                    autoComplete="organization"
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        <Field label="Work email" error={fieldError("email")}>
                            <div className="relative">
                                <AtSign
                                    size={16}
                                    aria-hidden="true"
                                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                />
                                <Input
                                    type="email"
                                    value={email}
                                    onChange={(event) => setEmail(event.target.value)}
                                    placeholder="you@yourbusiness.com"
                                    autoComplete="email"
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        <Field
                            label="Password"
                            error={fieldError("password")}
                            hint="At least 12 characters. A short phrase you will remember works well."
                        >
                            <div className="relative">
                                <Lock
                                    size={16}
                                    aria-hidden="true"
                                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                />
                                <Input
                                    type="password"
                                    value={password}
                                    onChange={(event) => setPassword(event.target.value)}
                                    placeholder="Choose a password"
                                    autoComplete="new-password"
                                    minLength={12}
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        {/*
                          Two separate boxes, both unticked. The first is a legal
                          acceptance and is required; the second is marketing
                          permission and is optional. One box for both would make
                          the acceptance record evidence of something it is not.
                        */}
                        <label className="flex items-start gap-2.5 mt-4 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={acceptTerms}
                                onChange={(event) => setAcceptTerms(event.target.checked)}
                                className="mt-0.5 w-4 h-4 shrink-0 accent-brand-600 cursor-pointer"
                                required
                            />
                            <span className="text-sm text-ink-600 leading-snug">
                                I agree to the{" "}
                                <Link to="/terms" target="_blank" className="text-brand-700 underline">
                                    Terms of Service
                                </Link>{" "}
                                and acknowledge the{" "}
                                <Link to="/privacy" target="_blank" className="text-brand-700 underline">
                                    Privacy Policy
                                </Link>
                                .
                            </span>
                        </label>

                        <label className="flex items-start gap-2.5 mt-3 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={marketingOptIn}
                                onChange={(event) => setMarketingOptIn(event.target.checked)}
                                className="mt-0.5 w-4 h-4 shrink-0 accent-brand-600 cursor-pointer"
                            />
                            <span className="text-sm text-ink-500 leading-snug">
                                Send me occasional product updates. Optional, and you can stop any time.
                            </span>
                        </label>

                        <Button
                            type="submit"
                            size="lg"
                            className="w-full mt-5"
                            loading={submitting}
                            disabled={!acceptTerms}
                        >
                            {submitting ? "Creating your workspace..." : "Create workspace"}
                        </Button>

                        <Link
                            to="/login"
                            className="mt-4 block text-center text-sm text-ink-500 hover:text-ink-700"
                        >
                            Already have an account? Sign in
                        </Link>
                    </form>
                </div>

                <p className="text-center text-xs text-ink-400 mt-6">
                    <Link to="/pricing" className="text-ink-400 hover:text-ink-600">Pricing</Link>
                    {" · "}
                    <Link to="/terms" className="text-ink-400 hover:text-ink-600">Terms</Link>
                    {" · "}
                    <Link to="/privacy" className="text-ink-400 hover:text-ink-600">Privacy</Link>
                </p>
            </div>
        </div>
    );
}

export default Signup;
