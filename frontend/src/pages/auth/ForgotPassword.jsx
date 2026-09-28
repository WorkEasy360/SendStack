import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Mail } from "lucide-react";
import axios from "axios";
import { useBranding } from "../../context/BrandingContext";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Field";
import { LogoMark } from "../../components/layout/Logo";

// A bare axios call rather than the shared `api` instance. That instance
// attaches an access token and, on a 401, tries to refresh and then redirects
// to /login -- all of which is exactly wrong on a page reached by somebody who
// cannot sign in. This request has no credentials by design.
const client = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
    headers: { "Content-Type": "application/json" },
});

function ForgotPassword() {
    const { branding, reloadBranding } = useBranding();

    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);
    const [sent, setSent] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        reloadBranding();
    }, [reloadBranding]);

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError("");
        setLoading(true);
        try {
            await client.post("/auth/password-reset/", { email });
            // Shown whether or not the address is registered, because the
            // server answers identically either way. Rendering "no such
            // account" here would undo the whole point of that -- the screen
            // would become the enumeration oracle the API refuses to be.
            setSent(true);
        } catch (err) {
            // The one case worth reporting: too many requests. Everything else
            // still shows the generic success, so a network hiccup does not
            // tell an attacker anything a success would not.
            if (err?.response?.status === 429) {
                setError("Too many requests. Wait a few minutes and try again.");
            } else {
                setSent(true);
            }
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-ink-50 px-4 py-10">
            <div className="w-full max-w-sm">
                <div className="flex flex-col items-center text-center mb-8">
                    <LogoMark size={44} />
                    <h1 className="text-2xl font-bold text-ink-900 mt-4 mb-1">{branding.product_name}</h1>
                    <p className="text-sm text-ink-500 m-0">Reset your password</p>
                </div>

                <div className="bg-surface border border-ink-200 rounded-card shadow-raised p-6 sm:p-7">
                    {sent ? (
                        <div className="text-center">
                            <div className="mx-auto w-11 h-11 rounded-full bg-emerald-50 flex items-center justify-center mb-4">
                                <Mail size={20} className="text-emerald-600" aria-hidden="true" />
                            </div>
                            <h2 className="text-base font-semibold text-ink-900 mb-2">Check your email</h2>
                            <p className="text-sm text-ink-500 mb-1">
                                If an account exists for <span className="font-medium text-ink-700">{email}</span>,
                                a password reset link has been sent.
                            </p>
                            <p className="text-xs text-ink-400 mb-5">
                                The link expires in one hour and can only be used once.
                            </p>
                            <Link to="/login" className="text-sm font-medium text-emerald-600 hover:text-emerald-700">
                                Back to sign in
                            </Link>
                        </div>
                    ) : (
                        <form onSubmit={handleSubmit}>
                            <Alert>{error}</Alert>

                            <p className="text-sm text-ink-500 mb-4 mt-0">
                                Enter the email address on your account and we&apos;ll send you a link to
                                choose a new password.
                            </p>

                            <Field label="Email address">
                                <div className="relative">
                                    <Mail
                                        size={16}
                                        aria-hidden="true"
                                        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                    />
                                    <Input
                                        type="email"
                                        value={email}
                                        onChange={(event) => setEmail(event.target.value)}
                                        placeholder="you@company.com"
                                        autoComplete="email"
                                        className="pl-9"
                                        required
                                    />
                                </div>
                            </Field>

                            <Button type="submit" size="lg" className="w-full mt-2" loading={loading}>
                                {loading ? "Sending..." : "Send reset link"}
                            </Button>

                            <Link
                                to="/login"
                                className="mt-4 flex items-center justify-center gap-1.5 text-sm text-ink-500 hover:text-ink-700"
                            >
                                <ArrowLeft size={14} aria-hidden="true" />
                                Back to sign in
                            </Link>
                        </form>
                    )}
                </div>
            </div>
        </div>
    );
}

export default ForgotPassword;
