import { useEffect, useState } from "react";
import { Globe, Palette } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useBranding } from "../../context/BrandingContext";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import { brandRamp } from "../../utils/brandColor";

/**
 * How this workspace — and every customer beneath it — is branded.
 *
 * Each field left blank is inherited rather than empty, so the form shows
 * what a blank currently resolves to. Without that, clearing the product name
 * looks like it broke something instead of handing the field back upstream.
 */

const RAMP_STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];

export default function Branding() {
    const toast = useToast();
    const { reloadBranding } = useBranding();

    const [form, setForm] = useState(null);
    const [effective, setEffective] = useState(null);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        api.get("/tenants/branding/")
            .then((response) => {
                const { effective: resolved, updated_at: _updatedAt, ...own } = response.data;
                setForm(own);
                setEffective(resolved);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your branding.")));
    }, []);

    const update = (patch) => setForm((current) => ({ ...current, ...patch }));

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const response = await api.patch("/tenants/branding/", {
                product_name: form.product_name,
                logo_url: form.logo_url,
                primary_color: form.primary_color,
                support_email: form.support_email,
                custom_domain: form.custom_domain || "",
            });
            const { effective: resolved, updated_at: _updatedAt, ...own } = response.data;
            setForm(own);
            setEffective(resolved);
            // Repaint the app around them rather than waiting for a reload.
            reloadBranding();
            toast.success("Branding saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save your branding."));
        } finally {
            setSaving(false);
        }
    };

    if (!form) return error ? <Alert>{error}</Alert> : <Spinner />;

    // What the colour field is worth right now: what they typed, or whatever
    // a blank inherits.
    const previewColor = form.primary_color || effective?.primary_color;
    const ramp = brandRamp(previewColor) || {};

    return (
        <div className="max-w-3xl">
            <PageHeader
                title="Branding"
                description="What your workspace is called and how it looks. Customers you create inherit this unless they set their own."
            />

            <Alert>{error}</Alert>

            <Card className="mb-6">
                <CardHeader
                    title="Identity"
                    description="Leave a field blank to inherit it from the account above yours."
                    className="mb-4"
                />

                <Field
                    label="Product name"
                    hint={
                        form.product_name
                            ? "Shown in the sidebar, on the login page, and as the browser tab title."
                            : `Inherited — currently “${effective?.product_name}”.`
                    }
                >
                    <Input
                        value={form.product_name}
                        onChange={(event) => update({ product_name: event.target.value })}
                        placeholder={effective?.product_name || "MultiSender"}
                        maxLength={60}
                    />
                </Field>

                <Field label="Logo URL" hint="A square image works best. Blank falls back to the default mark.">
                    <Input
                        type="url"
                        value={form.logo_url}
                        onChange={(event) => update({ logo_url: event.target.value })}
                        placeholder="https://example.com/logo.png"
                    />
                </Field>

                <Field label="Support email" hint="Where your customers are told to write for help.">
                    <Input
                        type="email"
                        value={form.support_email}
                        onChange={(event) => update({ support_email: event.target.value })}
                        placeholder={effective?.support_email || "help@example.com"}
                    />
                </Field>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Colour"
                    description="One colour; the whole accent ramp is derived from it."
                    action={<Palette size={16} className="text-ink-400" aria-hidden="true" />}
                    className="mb-4"
                />

                <div className="flex flex-wrap items-end gap-3 mb-4">
                    <Field label="Brand colour" className="mb-0">
                        <div className="flex gap-2">
                            <input
                                type="color"
                                value={previewColor || "#059669"}
                                onChange={(event) => update({ primary_color: event.target.value })}
                                className="h-10 w-14 cursor-pointer rounded-lg border border-ink-200 bg-white p-1"
                                aria-label="Pick a brand colour"
                            />
                            <Input
                                value={form.primary_color}
                                onChange={(event) => update({ primary_color: event.target.value })}
                                placeholder={effective?.primary_color || "#059669"}
                                className="w-36 font-mono"
                            />
                        </div>
                    </Field>
                    {!form.primary_color && <Badge tone="gray">Inherited</Badge>}
                    {form.primary_color && (
                        <Button variant="ghost" size="sm" onClick={() => update({ primary_color: "" })}>
                            Reset to inherited
                        </Button>
                    )}
                </div>

                <p className="text-xs font-semibold text-ink-500 m-0 mb-1.5">Derived ramp</p>
                <div className="flex rounded-lg overflow-hidden border border-ink-200">
                    {RAMP_STEPS.map((step) => (
                        <div
                            key={step}
                            className="flex-1 h-10 flex items-end justify-center pb-1"
                            style={{ backgroundColor: ramp[`--color-brand-${step}`] }}
                            title={`brand-${step}: ${ramp[`--color-brand-${step}`]}`}
                        >
                            <span
                                className={`text-[9px] font-bold ${step >= 500 ? "text-white/80" : "text-ink-500"}`}
                            >
                                {step}
                            </span>
                        </div>
                    ))}
                </div>
                <p className="text-xs text-ink-400 mt-2 mb-0">
                    Your colour sits at 600 — the step used for primary buttons and active navigation.
                </p>
            </Card>

            <Card className="mb-6">
                <CardHeader
                    title="Custom domain"
                    description="The address your customers use. Point it at this server, then set it here so its login page wears your brand."
                    action={<Globe size={16} className="text-ink-400" aria-hidden="true" />}
                    className="mb-4"
                />
                <Field label="Domain" hint="A bare hostname, without https:// or a path.">
                    <Input
                        value={form.custom_domain || ""}
                        onChange={(event) => update({ custom_domain: event.target.value })}
                        placeholder="app.example.com"
                    />
                </Field>
                <p className="text-xs text-ink-400 m-0">
                    Setting this here does not create the DNS record or the certificate — it tells the app which brand
                    to serve when a request arrives on that host.
                </p>
            </Card>

            <div className="flex justify-end">
                <Button onClick={save} loading={saving}>
                    Save branding
                </Button>
            </div>
        </div>
    );
}
