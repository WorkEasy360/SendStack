import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Lock, User } from "lucide-react";
import { useAuth } from "../../context/AuthContext";
import { useBranding } from "../../context/BrandingContext";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Field";
import { LogoMark } from "../../components/layout/Logo";

function Login() {
    const navigate = useNavigate();
    const { login } = useAuth();
    const { branding, reloadBranding } = useBranding();

    const [username, setUsername] = useState("");
    const [password, setPassword] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        reloadBranding();
    }, [reloadBranding]);

    const handleLogin = async (event) => {
        event.preventDefault();
        setError("");
        setLoading(true);

        const result = await login(username, password);
        setLoading(false);

        if (result.success) {
            reloadBranding();
            navigate("/", { replace: true });
        } else {
            setError(result.message);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-ink-50 px-4 py-10">
            <div className="w-full max-w-sm">
                <div className="flex flex-col items-center text-center mb-8">
                    <LogoMark size={44} />
                    <h1 className="text-2xl font-bold text-ink-900 mt-4 mb-1">{branding.product_name}</h1>
                    <p className="text-sm text-ink-500 m-0">Sign in to your workspace</p>
                </div>

                <div className="bg-white border border-ink-200 rounded-card shadow-raised p-6 sm:p-7">
                    <form onSubmit={handleLogin}>
                        <Alert>{error}</Alert>

                        <Field label="Username">
                            <div className="relative">
                                <User
                                    size={16}
                                    aria-hidden="true"
                                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
                                />
                                <Input
                                    type="text"
                                    value={username}
                                    onChange={(event) => setUsername(event.target.value)}
                                    placeholder="Enter username"
                                    autoComplete="username"
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        <Field label="Password">
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
                                    placeholder="Enter password"
                                    autoComplete="current-password"
                                    className="pl-9"
                                    required
                                />
                            </div>
                        </Field>

                        <Button type="submit" size="lg" className="w-full mt-2" loading={loading}>
                            {loading ? "Signing in..." : "Sign In"}
                        </Button>
                    </form>
                </div>

                <p className="text-center text-xs text-ink-400 mt-6">
                    WhatsApp campaigns, automation and inbox in one workspace.
                </p>
            </div>
        </div>
    );
}

export default Login;
