import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { CheckCircle2, Lock } from "lucide-react";
import axios from "axios";
import { useBranding } from "../../context/BrandingContext";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Field";
import { LogoMark } from "../../components/layout/Logo";

// Unauthenticated by design -- see the note in ForgotPassword.jsx.
const client = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || "/api/v1",
    headers: { "Content-Type": "application/json" },
});

//: Kept in step with PasswordResetConfirmSerializer.new_password on the server.
//: Checked here as well so somebody is told before they submit, never instead
//: of the server -- the server's validators are the authority.
const MIN_LENGTH = 12;

function ResetPassword() {
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const { branding, reloadBranding } = useBranding();

    const uid = params.get("uid") || "";
    const token = params.get("token") || "";

    const [password, setPassword] = useState("");
    const [confirmation, setConfirmation] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const [done, setDone] = useState(false);

    useEffect(() => {
        reloadBranding();
    }, [reloadBranding]);

    const localProblem = useMemo(() => {
        if (password && password.length < MIN_LENGTH) {
            return `Use at least ${MIN_LENGTH} characters.`;
        }
        if (confirmation && password !== confirmation) {
            return "The two passwords do not match.";
        }
        return "";
    }, [password, confirmation]);

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError("");
        if (localProblem) {
            setError(localProblem);
            return;
        }
        setLoading(true);
        try {
            await client.post("/auth/password-reset/confirm/", {
                uid,
                token,
                new_password: password,
                confirm_password: confirmation,
            });
            // Anything still in localStorage belongs to a session the server
            // has just revoked, so clearing it here only avoids the browser
            // presenting a token it already knows is dead.
            localStorage.removeItem("access_token");
            localStorage.removeItem("refresh_token");
            setDone(true);
        } catch (err) {
            const data = err?.response?.data;
            setError(
                data?.detail ||
                    data?.new_password?.[0] ||
                    data?.confirm_password?.[0] ||
                    "That reset link is invalid or has expired. Request a new one."
            );
        } finally {
            setLoading(false);
        }
    };

    // A link that arrived without its two halves cannot be acted on, so say so
    // immediately rather than after somebody has typed a password twice.
    if (!uid || !token) {
        return (
            <Shell branding={branding} subtitle="Reset your password">
                <Alert>This password reset link is incomplete.</Alert>
                <p className="text-sm text-ink-500 mt-3 mb-4">
                    Open the link from your email exactly as it was sent, or request a new one.
                </p>
                <Link to="/forgot-password">
                    <Button size="lg" className="w-full">
                        Request a new link
                    </Button>
                </Link>
            </Shell>
        );
    }

    if (done) {
        return (
            <Shell branding={branding} subtitle="Password updated">
                <div className="text-center">
                    <div className="mx-auto w-11 h-11 rounded-full bg-emerald-50 flex items-center justify-center mb-4">
                        <CheckCircle2 size={20} className="text-emerald-600" aria-hidden="true" />
                    </div>
                    <h2 className="text-base font-semibold text-ink-900 mb-2">Your password has been changed</h2>
                    <p className="text-sm text-ink-500 mb-5">
                        You have been signed out on every other device. Sign in with your new password.
                    </p>
                    <Button size="lg" className="w-full" onClick={() => navigate("/login", { replace: true })}>
                        Go to sign in
                    </Button>
                </div>
            </Shell>
        );
    }

    return (
        <Shell branding={branding} subtitle="Choose a new password">
            <form onSubmit={handleSubmit}>
                <Alert>{error}</Alert>

                <Field label="New password">
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
                            placeholder={`At least ${MIN_LENGTH} characters`}
                            autoComplete="new-password"
                            className="pl-9"
                            required
                        />
                    </div>
                </Field>

                <Field label="Confirm new password">
                    <div className="relative">
                        <Lock
                            size={16}
                            aria-hidden="true"
                            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                        />
                        <Input
                            type="password"
                            value={confirmation}
                            onChange={(event) => setConfirmation(event.target.value)}
                            placeholder="Type it again"
                            autoComplete="new-password"
                            className="pl-9"
                            required
                        />
                    </div>
                </Field>

                {localProblem ? <p className="text-xs text-amber-600 -mt-2 mb-3">{localProblem}</p> : null}

                <Button type="submit" size="lg" className="w-full mt-2" loading={loading} disabled={!!localProblem}>
                    {loading ? "Updating..." : "Update password"}
                </Button>

                <p className="text-center text-xs text-ink-400 mt-4 mb-0">
                    Signing you out everywhere else, so any device still signed in will need the new password.
                </p>
            </form>
        </Shell>
    );
}

function Shell({ branding, subtitle, children }) {
    return (
        <div className="min-h-screen flex items-center justify-center bg-ink-50 px-4 py-10">
            <div className="w-full max-w-sm">
                <div className="flex flex-col items-center text-center mb-8">
                    <LogoMark size={44} />
                    <h1 className="text-2xl font-bold text-ink-900 mt-4 mb-1">{branding.product_name}</h1>
                    <p className="text-sm text-ink-500 m-0">{subtitle}</p>
                </div>
                <div className="bg-surface border border-ink-200 rounded-card shadow-raised p-6 sm:p-7">{children}</div>
            </div>
        </div>
    );
}

export default ResetPassword;
