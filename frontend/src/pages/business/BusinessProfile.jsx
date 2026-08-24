import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Building2, Eye, MessageSquare, PhoneCall, Sparkles } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";
import { CardGridSkeleton } from "../../components/ui/Skeleton";
import { useToast } from "../../components/ui/Toast";

/**
 * What the business is, told to the platform once.
 *
 * This used to be two free-text boxes on two different pages -- one on the AI
 * Agent screen and one on the Calling Agent screen -- which meant a business
 * that changed its delivery area had to remember to change it twice, and
 * usually didn't. Everything here is read by both agents from one record.
 *
 * The preview panel is the honest part of the screen. Filling these boxes in
 * is really writing part of an AI prompt, and the only truthful way to show
 * somebody what they have written is to show them the text the model is
 * actually handed -- not a summary of it.
 */

const SECTIONS = [
    {
        title: "The basics",
        description: "Enough for an agent to introduce itself and say what you do.",
        fields: [
            {
                name: "trading_name",
                label: "Trading name",
                type: "text",
                hint: "Only if customers call you something different from your account name.",
            },
            {
                name: "description",
                label: "What does your business do?",
                type: "textarea",
                rows: 3,
                placeholder: "We sell and service bicycles for commuters and clubs.",
                hint: "A sentence or two. This is the first thing both agents are told.",
            },
        ],
    },
    {
        title: "What you offer",
        description: "So an agent can tell a customer whether you have what they want.",
        fields: [
            {
                name: "products",
                label: "Products",
                type: "textarea",
                rows: 3,
                placeholder: "Road bikes, city bikes, helmets, spare parts",
            },
            {
                name: "services",
                label: "Services",
                type: "textarea",
                rows: 2,
                placeholder: "Servicing, repairs, custom builds",
            },
            {
                name: "target_customers",
                label: "Who you sell to",
                type: "textarea",
                rows: 2,
                placeholder: "Daily commuters, cycling clubs, corporate fleets",
            },
        ],
    },
    {
        title: "Where and how to reach you",
        description: "The only contact details an agent is allowed to give out.",
        fields: [
            {
                name: "locations",
                label: "Locations",
                type: "textarea",
                rows: 2,
                placeholder: "Shop in Pune. We deliver across Maharashtra.",
            },
            {
                name: "contact_details",
                label: "Contact details",
                type: "textarea",
                rows: 2,
                placeholder: "+91 20 1234 5678 · hello@acmecycles.in",
                hint: "Opening hours come from your Team page, so you don't need to repeat them here.",
            },
        ],
    },
    {
        title: "How the agents should behave",
        description: "Standing rules that apply to every reply and every call.",
        fields: [
            {
                name: "policies",
                label: "Rules the agent must always follow",
                type: "textarea",
                rows: 3,
                placeholder: "Never quote a discount above 10%. Always confirm the delivery address.",
                hint: "Short rules only. Full policy documents belong in the knowledge base, where they are looked up per question.",
            },
            {
                name: "escalation_rules",
                label: "When to fetch a person",
                type: "textarea",
                rows: 2,
                placeholder: "Any order over 1 lakh. Anything about a warranty claim.",
                hint: "The agents already hand over when they are unsure or a customer asks. This adds your own cases.",
            },
            {
                name: "languages",
                label: "Languages you serve customers in",
                type: "text",
                placeholder: "English, Hindi, Marathi",
            },
            {
                name: "tone",
                label: "How the agents should sound",
                type: "select",
                options: [
                    { value: "FRIENDLY", label: "Friendly and warm" },
                    { value: "PROFESSIONAL", label: "Professional" },
                    { value: "CONCISE", label: "Short and direct" },
                    { value: "ENTHUSIASTIC", label: "Enthusiastic" },
                ],
            },
        ],
    },
];

const BLANK = {
    trading_name: "",
    description: "",
    products: "",
    services: "",
    locations: "",
    contact_details: "",
    policies: "",
    escalation_rules: "",
    languages: "",
    tone: "FRIENDLY",
};

export default function BusinessProfile() {
    const toast = useToast();
    const [form, setForm] = useState(BLANK);
    const [preview, setPreview] = useState("");
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const [dirty, setDirty] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await api.get("/tenants/business-profile/");
            setForm({ ...BLANK, ...data });
            setPreview(data.preview || "");
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Could not load your business profile."));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const update = (name, value) => {
        setForm((current) => ({ ...current, [name]: value }));
        setDirty(true);
    };

    const save = async () => {
        setSaving(true);
        setError("");
        try {
            const { data } = await api.patch("/tenants/business-profile/", form);
            setForm({ ...BLANK, ...data });
            setPreview(data.preview || "");
            setDirty(false);
            toast.success("Business profile saved.");
        } catch (err) {
            setError(apiErrorMessage(err, "Could not save your business profile."));
        } finally {
            setSaving(false);
        }
    };

    if (loading) return <CardGridSkeleton count={3} />;

    return (
        <div className="max-w-5xl">
            <PageHeader
                title="Business profile"
                description="Tell the platform who you are once. Both the WhatsApp assistant and the calling agent read it."
                actions={
                    <Button onClick={save} loading={saving} disabled={!dirty}>
                        {dirty ? "Save changes" : "Saved"}
                    </Button>
                }
            />

            <Alert>{error}</Alert>

            <Card className="mb-6">
                <div className="flex items-start gap-3">
                    <span className="shrink-0 w-9 h-9 rounded-lg bg-brand-50 text-brand-700 flex items-center justify-center">
                        <Sparkles size={17} aria-hidden="true" />
                    </span>
                    <div className="min-w-0">
                        <p className="text-sm font-semibold text-ink-900 m-0">Who reads this</p>
                        <p className="text-sm text-ink-500 mt-1 mb-0">
                            Everything below is given to your agents before they answer anything. Facts about
                            prices, stock and policies still come from your{" "}
                            <Link to="/knowledge" className="font-medium">
                                knowledge base
                            </Link>
                            , which is searched per question instead of being sent with every message.
                        </p>
                        <div className="flex flex-wrap gap-2 mt-3">
                            <Link
                                to="/ai"
                                className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-600 bg-ink-100 rounded-full px-3 py-1 no-underline hover:bg-ink-200"
                            >
                                <MessageSquare size={13} aria-hidden="true" /> WhatsApp assistant
                            </Link>
                            <Link
                                to="/voice"
                                className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-600 bg-ink-100 rounded-full px-3 py-1 no-underline hover:bg-ink-200"
                            >
                                <PhoneCall size={13} aria-hidden="true" /> Calling agent
                            </Link>
                        </div>
                    </div>
                </div>
            </Card>

            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px] items-start">
                <div className="grid gap-6">
                    {SECTIONS.map((section) => (
                        <Card key={section.title}>
                            <CardHeader
                                title={section.title}
                                description={section.description}
                                className="mb-4"
                            />
                            {section.fields.map((field) => (
                                <Field key={field.name} label={field.label} hint={field.hint}>
                                    {field.type === "textarea" && (
                                        <Textarea
                                            rows={field.rows || 3}
                                            value={form[field.name] || ""}
                                            placeholder={field.placeholder}
                                            onChange={(event) => update(field.name, event.target.value)}
                                        />
                                    )}
                                    {field.type === "text" && (
                                        <Input
                                            value={form[field.name] || ""}
                                            placeholder={field.placeholder}
                                            onChange={(event) => update(field.name, event.target.value)}
                                        />
                                    )}
                                    {field.type === "select" && (
                                        <Select
                                            value={form[field.name] || ""}
                                            onChange={(event) => update(field.name, event.target.value)}
                                        >
                                            {field.options.map((option) => (
                                                <option key={option.value} value={option.value}>
                                                    {option.label}
                                                </option>
                                            ))}
                                        </Select>
                                    )}
                                </Field>
                            ))}
                        </Card>
                    ))}
                </div>

                <Card className="lg:sticky lg:top-6">
                    <CardHeader
                        title="What your agents are told"
                        description="The exact text, saved as it stands."
                        className="mb-4"
                    />
                    {preview ? (
                        <pre className="text-xs leading-relaxed text-ink-700 bg-ink-50 border border-ink-200 rounded-lg p-3 m-0 whitespace-pre-wrap break-words max-h-[28rem] overflow-y-auto">
                            {preview}
                        </pre>
                    ) : (
                        <div className="text-center py-6">
                            <span className="inline-flex w-10 h-10 rounded-full bg-ink-100 text-ink-400 items-center justify-center mb-3">
                                <Eye size={18} aria-hidden="true" />
                            </span>
                            <p className="text-sm text-ink-500 m-0">
                                Nothing yet. Describe your business and save to see what the agents will read.
                            </p>
                        </div>
                    )}
                    {dirty && (
                        <p className="text-xs text-ink-500 mt-3 mb-0">
                            <Building2 size={12} className="inline mr-1" aria-hidden="true" />
                            Save to update this preview.
                        </p>
                    )}
                </Card>
            </div>
        </div>
    );
}
