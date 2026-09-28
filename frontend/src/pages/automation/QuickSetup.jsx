import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Lightbulb, Sparkles, Wand2 } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import Spinner from "../../components/ui/Spinner";
import { CheckboxField, Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * Automation, described as a sentence instead of a diagram.
 *
 * "TRIGGER -> CONDITION -> DELAY -> ACTION" is how the engine works, not what
 * anybody wants. This tab asks what should happen ("follow up with people who
 * don't reply"), asks only the two or three questions that genuinely cannot be
 * guessed, and creates an ordinary rule the other tabs can then edit. Nothing
 * here is a second engine — see backend presets.py.
 *
 * Alongside it: what the system has noticed. Every suggestion is a counted
 * fact about this account's own data, and every one of them opens a form
 * rather than doing anything. Noticing an opportunity and acting on it are
 * different products; only one of them is safe to ship without asking.
 */

const TONE_CLASS = {
    red: "border-l-red-400",
    amber: "border-l-amber-400",
    green: "border-l-brand-500",
    blue: "border-l-blue-400",
    violet: "border-l-violet-400",
    gray: "border-l-ink-300",
};

function PresetCard({ preset, onChoose, disabled }) {
    return (
        <button
            type="button"
            disabled={disabled}
            onClick={() => onChoose(preset)}
            className="text-left bg-surface border border-ink-200 rounded-card p-4 shadow-card cursor-pointer transition-colors hover:border-brand-400 disabled:cursor-not-allowed disabled:opacity-60 w-full"
        >
            <div className="flex items-start gap-3">
                <span className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center shrink-0">
                    <Wand2 size={17} aria-hidden="true" />
                </span>
                <div className="min-w-0">
                    <p className="text-sm font-bold text-ink-900 m-0">{preset.name}</p>
                    <p className="text-sm text-ink-600 mt-1 mb-0">{preset.goal}</p>
                    <p className="text-xs text-ink-400 mt-2 mb-0 italic">{preset.why}</p>
                </div>
            </div>
        </button>
    );
}

export default function QuickSetup({ accounts, templates, onCreated }) {
    const toast = useToast();
    const [presets, setPresets] = useState([]);
    const [recommendations, setRecommendations] = useState([]);
    const [knowledgeBases, setKnowledgeBases] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [active, setActive] = useState(null);
    const [answers, setAnswers] = useState({});
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        Promise.all([
            api.get("/automation/presets/"),
            api.get("/automation/recommendations/").catch(() => ({ data: { results: [] } })),
            api.get("/ai/knowledge-bases/").catch(() => ({ data: { results: [] } })),
        ])
            .then(([presetsRes, recommendationsRes, basesRes]) => {
                setPresets(presetsRes.data.results || []);
                setRecommendations(recommendationsRes.data.results || []);
                setKnowledgeBases(basesRes.data.results || []);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the automation presets.")))
            .finally(() => setLoading(false));
    }, []);

    const connected = accounts.filter((account) => account.status === "CONNECTED");

    const openPreset = (preset) => {
        const seeded = {};
        preset.fields.forEach((field) => {
            seeded[field.key] = field.default ?? (field.type === "templates" ? [] : "");
        });
        // One connected number is the common case; pre-picking it removes a
        // question that has only one answer.
        if (!seeded.whatsapp_account) {
            seeded.whatsapp_account = (connected[0] || accounts[0])?.id || "";
        }
        if ("knowledge_base" in seeded && !seeded.knowledge_base) {
            const usable = knowledgeBases.filter((base) => base.document_count > 0);
            seeded.knowledge_base = (usable[0] || knowledgeBases[0])?.id || "";
        }
        setAnswers(seeded);
        setActive(preset);
    };

    const openByKey = (key) => {
        const preset = presets.find((item) => item.key === key);
        if (preset) openPreset(preset);
    };

    const activate = async (event) => {
        event.preventDefault();
        setSaving(true);
        try {
            const response = await api.post(`/automation/presets/${active.key}/activate/`, answers);
            toast.success(`"${active.name}" is on.`);
            setActive(null);
            onCreated?.(response.data.kind);
            // The counts behind the suggestions have just changed.
            api.get("/automation/recommendations/")
                .then((res) => setRecommendations(res.data.results || []))
                .catch(() => {});
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not set that up."));
        } finally {
            setSaving(false);
        }
    };

    const setAnswer = (key, value) => setAnswers((current) => ({ ...current, [key]: value }));

    const toggleTemplate = (key, templateId) => {
        setAnswers((current) => {
            const list = current[key] || [];
            return {
                ...current,
                [key]: list.includes(templateId)
                    ? list.filter((id) => id !== templateId)
                    : [...list, templateId],
            };
        });
    };

    if (loading) return <Spinner />;

    return (
        <div>
            <Alert>{error}</Alert>

            {recommendations.length > 0 && (
                <Card className="mb-6">
                    <CardHeader
                        title="What we noticed"
                        description="Found in your own data. Nothing here has been sent or changed — each one opens a form."
                        className="mb-4"
                    />
                    <ul className="list-none p-0 m-0 space-y-3">
                        {recommendations.map((item) => (
                            <li
                                key={item.key}
                                className={`border border-ink-200 border-l-4 rounded-card p-4 flex flex-col sm:flex-row sm:items-center gap-3 ${
                                    TONE_CLASS[item.tone] || TONE_CLASS.gray
                                }`}
                            >
                                <span className="w-8 h-8 rounded-lg bg-amber-50 text-amber-600 flex items-center justify-center shrink-0">
                                    <Lightbulb size={16} aria-hidden="true" />
                                </span>
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm font-bold text-ink-900 m-0">{item.title}</p>
                                    <p className="text-sm text-ink-600 mt-0.5 mb-0">{item.detail}</p>
                                </div>
                                {item.action === "preset" ? (
                                    <Button
                                        size="sm"
                                        icon={Sparkles}
                                        className="shrink-0"
                                        onClick={() => openByKey(item.preset)}
                                    >
                                        {item.action_label}
                                    </Button>
                                ) : (
                                    <Link to={item.link} className="no-underline shrink-0">
                                        <Button size="sm" variant="secondary">
                                            {item.action_label}
                                            <ArrowRight size={14} aria-hidden="true" />
                                        </Button>
                                    </Link>
                                )}
                            </li>
                        ))}
                    </ul>
                </Card>
            )}

            <div className="mb-4">
                <h2 className="text-base font-bold text-ink-900 m-0">Tell us what you want to happen</h2>
                <p className="text-sm text-ink-500 mt-1 mb-0">
                    Pick one and answer two or three questions. We will set up the rest, and you can change it later on
                    the other tabs.
                </p>
            </div>

            {presets.length === 0 ? (
                <EmptyState icon={Wand2} title="No presets available" description="Nothing to set up right now." />
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                    {presets.map((preset) => (
                        <PresetCard
                            key={preset.key}
                            preset={preset}
                            onChoose={openPreset}
                            disabled={accounts.length === 0 && preset.creates !== "KNOWLEDGE"}
                        />
                    ))}
                </div>
            )}

            {active && (
                <Modal
                    title={active.name}
                    description={active.goal}
                    onClose={() => setActive(null)}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setActive(null)}>
                                Cancel
                            </Button>
                            <Button type="submit" form="preset-form" loading={saving} icon={Sparkles}>
                                Turn it on
                            </Button>
                        </>
                    }
                >
                    <form id="preset-form" onSubmit={activate} className="space-y-4">
                        {active.fields.map((field) => {
                            if (field.type === "account") {
                                return (
                                    <Field key={field.key} label={field.label} required={field.required}>
                                        <Select
                                            required={field.required}
                                            value={answers[field.key] || ""}
                                            onChange={(event) => setAnswer(field.key, event.target.value)}
                                        >
                                            <option value="">Choose a number...</option>
                                            {accounts.map((account) => (
                                                <option key={account.id} value={account.id}>
                                                    {account.name}
                                                    {account.status !== "CONNECTED" ? " (not connected)" : ""}
                                                </option>
                                            ))}
                                        </Select>
                                    </Field>
                                );
                            }
                            if (field.type === "knowledge_base") {
                                return (
                                    <Field key={field.key} label={field.label} hint={field.help} required={field.required}>
                                        {knowledgeBases.length === 0 ? (
                                            <p className="text-sm text-ink-500 m-0">
                                                You have no knowledge bases yet.{" "}
                                                <Link to="/knowledge">Create one and upload a document</Link> first.
                                            </p>
                                        ) : (
                                            <Select
                                                required={field.required}
                                                value={answers[field.key] || ""}
                                                onChange={(event) => setAnswer(field.key, Number(event.target.value))}
                                            >
                                                <option value="">Choose a knowledge base...</option>
                                                {knowledgeBases.map((base) => (
                                                    <option key={base.id} value={base.id}>
                                                        {base.name}
                                                        {base.document_count
                                                            ? ` (${base.document_count} document${base.document_count === 1 ? "" : "s"})`
                                                            : " (empty)"}
                                                    </option>
                                                ))}
                                            </Select>
                                        )}
                                    </Field>
                                );
                            }
                            if (field.type === "choice") {
                                return (
                                    <Field key={field.key} label={field.label} hint={field.help}>
                                        <Select
                                            value={answers[field.key] ?? field.default}
                                            onChange={(event) => setAnswer(field.key, Number(event.target.value))}
                                        >
                                            {field.options.map((option) => (
                                                <option key={option.value} value={option.value}>
                                                    {option.label}
                                                </option>
                                            ))}
                                        </Select>
                                    </Field>
                                );
                            }
                            if (field.type === "templates") {
                                return (
                                    <Field key={field.key} label={field.label} hint={field.help}>
                                        {templates.length === 0 ? (
                                            <p className="text-sm text-ink-500 m-0">
                                                You have no templates yet.{" "}
                                                <Link to="/templates">Create one</Link> to scope this to a specific
                                                message.
                                            </p>
                                        ) : (
                                            <div className="max-h-40 overflow-y-auto border border-ink-200 rounded-lg p-2 space-y-1">
                                                {templates.map((template) => (
                                                    <CheckboxField
                                                        key={template.id}
                                                        label={template.name}
                                                        checked={(answers[field.key] || []).includes(template.id)}
                                                        onChange={() => toggleTemplate(field.key, template.id)}
                                                    />
                                                ))}
                                            </div>
                                        )}
                                    </Field>
                                );
                            }
                            if (field.type === "textarea") {
                                return (
                                    <Field key={field.key} label={field.label} hint={field.help} required={field.required}>
                                        <Textarea
                                            rows={4}
                                            required={field.required}
                                            value={answers[field.key] ?? ""}
                                            onChange={(event) => setAnswer(field.key, event.target.value)}
                                        />
                                    </Field>
                                );
                            }
                            return (
                                <Field key={field.key} label={field.label} hint={field.help} required={field.required}>
                                    <Input
                                        required={field.required}
                                        value={answers[field.key] ?? ""}
                                        onChange={(event) => setAnswer(field.key, event.target.value)}
                                    />
                                </Field>
                            );
                        })}

                        <div className="flex items-start gap-2 rounded-lg bg-ink-50 border border-ink-200 p-3">
                            <Badge tone="gray">Safe</Badge>
                            <p className="text-xs text-ink-600 m-0">
                                This obeys everything your normal sends obey: unsubscribed contacts are never
                                messaged, quiet hours are respected, per-number send limits still apply, and a
                                follow-up stops the moment somebody replies.
                            </p>
                        </div>
                    </form>
                </Modal>
            )}
        </div>
    );
}
