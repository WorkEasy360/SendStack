import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { CalendarClock, MessageSquareText, Smartphone, Users } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { toMediaItems, primaryMessageType } from "../../utils/media";
import { toButtons } from "../../utils/buttons";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Card from "../../components/ui/Card";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import MediaPicker from "../../components/ui/MediaPicker";
import ButtonBuilder, { ButtonPreview } from "../../components/ui/ButtonBuilder";
import { CheckboxField, Field, Input, Select, Textarea, Toggle } from "../../components/ui/Field";
import { ACCOUNT_TYPE_LABEL } from "../../constants/whatsapp";

const EMPTY_FORM = {
    name: "",
    account_assignment_mode: "MANUAL",
    whatsapp_account: "",
    min_delay_seconds: "",
    max_delay_seconds: "",
    template: "",
    message_type: "TEXT",
    message_body: "",
    media_items: [],
    buttons: [],
    append_opt_out: true,
    track_links: true,
    audience_type: "ALL",
    audience_group: "",
    audience_tag: "",
    audience_segment: "",
    audience_contact_ids: [],
    scheduled_at: "",
};

function Step({ number, icon: Icon, title, description, children }) {
    return (
        <Card className="mb-4">
            <div className="flex items-start gap-3 mb-5">
                <span className="shrink-0 w-9 h-9 rounded-lg bg-ink-900 text-white flex items-center justify-center">
                    <Icon size={17} aria-hidden="true" />
                </span>
                <div className="min-w-0">
                    <h2 className="text-sm font-bold text-ink-900 m-0">
                        <span className="text-ink-400 mr-1.5">{number}.</span>
                        {title}
                    </h2>
                    {description && <p className="text-xs text-ink-500 mt-0.5 mb-0">{description}</p>}
                </div>
            </div>
            {children}
        </Card>
    );
}

export default function CampaignWizard() {
    const navigate = useNavigate();
    // A retargeting audience handed over from somewhere that already knows
    // who it means -- the campaign report's "everyone who clicked", today.
    const { state } = useLocation();
    const handedAudience = state?.contactIds?.length ? state : null;
    const [accounts, setAccounts] = useState([]);
    const [templates, setTemplates] = useState([]);
    const [groups, setGroups] = useState([]);
    const [tags, setTags] = useState([]);
    const [segments, setSegments] = useState([]);
    const [loading, setLoading] = useState(true);
    const [form, setForm] = useState(() =>
        handedAudience
            ? { ...EMPTY_FORM, audience_type: "CONTACT_IDS", audience_contact_ids: handedAudience.contactIds }
            : EMPTY_FORM
    );
    const [useTemplate, setUseTemplate] = useState(true);
    const [error, setError] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        Promise.all([
            api.get("/whatsapp/accounts/"),
            api.get("/templates/templates/", { params: { is_active: true } }),
            api.get("/contacts/groups/"),
            api.get("/contacts/tags/"),
            api.get("/contacts/segments/"),
        ])
            .then(([accountsRes, templatesRes, groupsRes, tagsRes, segmentsRes]) => {
                setAccounts(accountsRes.data.results || []);
                setTemplates(templatesRes.data.results || []);
                setGroups(groupsRes.data.results || []);
                setTags(tagsRes.data.results || []);
                setSegments(segmentsRes.data.results || []);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load campaign setup data.")))
            .finally(() => setLoading(false));
    }, []);

    const submit = async (event) => {
        event.preventDefault();
        setError("");
        setSaving(true);

        const payload = {
            name: form.name,
            account_assignment_mode: form.account_assignment_mode,
            audience_type: form.audience_type,
            scheduled_at: form.scheduled_at || null,
            // Blank means "use the deployment default" -- send null rather than
            // "" so the backend falls back instead of rejecting it.
            min_delay_seconds: form.min_delay_seconds === "" ? null : Number(form.min_delay_seconds),
            max_delay_seconds: form.max_delay_seconds === "" ? null : Number(form.max_delay_seconds),
            append_opt_out: form.append_opt_out,
            track_links: form.track_links,
        };
        // In automatic mode the backend chooses an account per recipient, so no
        // specific device is sent.
        if (form.account_assignment_mode === "MANUAL") {
            payload.whatsapp_account = form.whatsapp_account;
        }
        if (useTemplate) {
            payload.template = form.template || null;
        } else {
            payload.message_type = form.message_type;
            payload.message_body = form.message_body;
            // The API mirrors media_url/filename/caption from the first
            // attachment, so the list is all this form has to send.
            payload.media_items = form.media_items;
            payload.buttons = form.buttons;
        }
        if (form.audience_type === "GROUP") payload.audience_group = form.audience_group;
        if (form.audience_type === "TAG") payload.audience_tag = form.audience_tag;
        if (form.audience_type === "SEGMENT") payload.audience_segment = form.audience_segment;
        if (form.audience_type === "CONTACT_IDS") payload.audience_contact_ids = form.audience_contact_ids;

        try {
            const response = await api.post("/campaigns/campaigns/", payload);
            navigate(`/campaigns/${response.data.id}`);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to create this campaign."));
        } finally {
            setSaving(false);
        }
    };

    if (loading) return <Spinner />;

    const connectedAccounts = accounts.filter((a) => a.status === "CONNECTED");
    const selectedTemplate = templates.find((t) => String(t.id) === String(form.template));
    const isAutoMode = form.account_assignment_mode === "AUTO";
    const delayError =
        form.min_delay_seconds !== "" &&
        form.max_delay_seconds !== "" &&
        Number(form.max_delay_seconds) < Number(form.min_delay_seconds)
            ? "Maximum must be at least the minimum."
            : "";

    return (
        <div className="max-w-2xl">
            <PageHeader
                title="New Campaign"
                description="Set up your audience and message, then review before launching."
                backTo="/campaigns"
                backLabel="Back to campaigns"
            />

            <Alert>{error}</Alert>

            {connectedAccounts.length === 0 && (
                <Alert tone="warning" title="No connected device">
                    Connect a WhatsApp device from the Devices page before creating a campaign.
                </Alert>
            )}

            <form onSubmit={submit}>
                <Step number={1} icon={MessageSquareText} title="Basics" description="Name this campaign and pick the sending device.">
                    <Field label="Campaign Name" required>
                        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
                    </Field>

                    <Field label="WhatsApp Account" className={isAutoMode ? "mb-0" : "mb-4"}>
                        <div className="flex flex-wrap gap-4 pt-0.5">
                            <CheckboxField
                                type="radio"
                                name="account-mode"
                                label="Automatic / Random"
                                description="Spread recipients across every connected device"
                                checked={isAutoMode}
                                onChange={() => setForm({ ...form, account_assignment_mode: "AUTO" })}
                            />
                            <CheckboxField
                                type="radio"
                                name="account-mode"
                                label="Manual"
                                description="Send everything from one device"
                                checked={!isAutoMode}
                                onChange={() => setForm({ ...form, account_assignment_mode: "MANUAL" })}
                            />
                        </div>
                    </Field>

                    {isAutoMode ? (
                        <p className="text-xs text-ink-500 mt-3 mb-0">
                            Each recipient is assigned one of your {connectedAccounts.length} connected device
                            {connectedAccounts.length === 1 ? "" : "s"}. If a device disconnects mid-campaign, its
                            remaining recipients move to another connected device.
                        </p>
                    ) : (
                        <Field label="Send From" required className="mb-0">
                            <Select
                                value={form.whatsapp_account}
                                onChange={(e) => setForm({ ...form, whatsapp_account: e.target.value })}
                                required
                            >
                                <option value="">Select a WhatsApp device...</option>
                                {connectedAccounts.map((account) => (
                                    <option key={account.id} value={account.id}>
                                        {account.name} ({account.phone_number}) —{" "}
                                        {ACCOUNT_TYPE_LABEL[account.account_type] || account.account_type}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                </Step>

                <Step number={2} icon={Users} title="Audience" description="Choose who receives this campaign.">
                    {form.audience_type === "CONTACT_IDS" ? (
                        <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                                <p className="text-sm font-semibold text-ink-900 m-0">
                                    {form.audience_contact_ids.length} contact
                                    {form.audience_contact_ids.length === 1 ? "" : "s"}
                                </p>
                                <p className="text-xs text-ink-500 mt-0.5 mb-0">
                                    {handedAudience?.audienceLabel || "A fixed list of contacts."}
                                </p>
                            </div>
                            <Button
                                type="button"
                                size="xs"
                                variant="ghost"
                                className="shrink-0"
                                onClick={() =>
                                    setForm({ ...form, audience_type: "ALL", audience_contact_ids: [] })
                                }
                            >
                                Choose a different audience
                            </Button>
                        </div>
                    ) : (
                    <>
                    <Field label="Send to" className={form.audience_type === "ALL" ? "mb-0" : ""}>
                        <Select
                            value={form.audience_type}
                            onChange={(e) => setForm({ ...form, audience_type: e.target.value })}
                        >
                            <option value="ALL">All subscribed contacts</option>
                            <option value="GROUP">A group</option>
                            <option value="TAG">A tag</option>
                            <option value="SEGMENT">A saved segment</option>
                        </Select>
                    </Field>
                    {form.audience_type === "GROUP" && (
                        <Field label="Group" required className="mb-0">
                            <Select
                                value={form.audience_group}
                                onChange={(e) => setForm({ ...form, audience_group: e.target.value })}
                                required
                            >
                                <option value="">Select a group...</option>
                                {groups.map((g) => (
                                    <option key={g.id} value={g.id}>
                                        {g.name}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                    {form.audience_type === "TAG" && (
                        <Field label="Tag" required className="mb-0">
                            <Select
                                value={form.audience_tag}
                                onChange={(e) => setForm({ ...form, audience_tag: e.target.value })}
                                required
                            >
                                <option value="">Select a tag...</option>
                                {tags.map((t) => (
                                    <option key={t.id} value={t.id}>
                                        {t.name}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                    {form.audience_type === "SEGMENT" && (
                        <Field label="Segment" required className="mb-0">
                            <Select
                                value={form.audience_segment}
                                onChange={(e) => setForm({ ...form, audience_segment: e.target.value })}
                                required
                            >
                                <option value="">Select a segment...</option>
                                {segments.map((s) => (
                                    <option key={s.id} value={s.id}>
                                        {s.name}
                                    </option>
                                ))}
                            </Select>
                        </Field>
                    )}
                    </>
                    )}
                </Step>

                <Step number={3} icon={Smartphone} title="Message" description="Reuse a template or write a one-off message.">
                    <div className="flex flex-wrap gap-4 mb-4">
                        <CheckboxField
                            type="radio"
                            name="message-source"
                            label="Use a template"
                            checked={useTemplate}
                            onChange={() => setUseTemplate(true)}
                        />
                        <CheckboxField
                            type="radio"
                            name="message-source"
                            label="Write a custom message"
                            checked={!useTemplate}
                            onChange={() => setUseTemplate(false)}
                        />
                    </div>

                    {useTemplate ? (
                        <>
                            <Field label="Template" required className={selectedTemplate ? "" : "mb-0"}>
                                <Select
                                    value={form.template}
                                    onChange={(e) => setForm({ ...form, template: e.target.value })}
                                    required
                                >
                                    <option value="">Select a template...</option>
                                    {templates.map((t) => (
                                        <option key={t.id} value={t.id}>
                                            {t.name}
                                        </option>
                                    ))}
                                </Select>
                            </Field>
                            {selectedTemplate && (
                                <div className="rounded-xl bg-ink-100 p-4">
                                    <p className="text-xs font-semibold uppercase tracking-wide text-ink-400 m-0 mb-2">
                                        Template preview
                                    </p>
                                    <div className="max-w-[85%] ml-auto">
                                        <div className="bg-brand-600 text-white rounded-2xl rounded-br-sm px-3.5 py-2.5 text-sm whitespace-pre-wrap">
                                            {selectedTemplate.body}
                                        </div>
                                        <ButtonPreview buttons={toButtons(selectedTemplate)} className="mt-1" />
                                    </div>
                                    {toMediaItems(selectedTemplate).length > 0 && (
                                        <p className="text-xs text-ink-500 mt-2 mb-0">
                                            {toMediaItems(selectedTemplate).length} attachment
                                            {toMediaItems(selectedTemplate).length === 1 ? "" : "s"} sent with this
                                            template.
                                        </p>
                                    )}
                                </div>
                            )}
                        </>
                    ) : (
                        <>
                            <Field
                                label="Message Type"
                                hint={
                                    form.media_items.length > 0 ? "Set by the first attachment below." : undefined
                                }
                            >
                                <Select
                                    value={form.message_type}
                                    disabled={form.media_items.length > 0}
                                    onChange={(e) => setForm({ ...form, message_type: e.target.value })}
                                >
                                    <option value="TEXT">Text</option>
                                    <option value="IMAGE">Image</option>
                                    <option value="VIDEO">Video</option>
                                    <option value="DOCUMENT">Document</option>
                                </Select>
                            </Field>
                            <Field label="Message" hint="Use [Name] etc. for personalization." required>
                                <Textarea
                                    value={form.message_body}
                                    onChange={(e) => setForm({ ...form, message_body: e.target.value })}
                                    required
                                />
                            </Field>
                            <MediaPicker
                                value={form.media_items}
                                onChange={(media_items) =>
                                    setForm({
                                        ...form,
                                        media_items,
                                        message_type: primaryMessageType(
                                            media_items,
                                            media_items.length ? form.message_type : "TEXT"
                                        ),
                                    })
                                }
                            />
                            <ButtonBuilder
                                value={form.buttons}
                                onChange={(buttons) => setForm({ ...form, buttons })}
                                hint="Shown under the message. Answer a tap with a button-triggered rule on the Automation page."
                            />
                        </>
                    )}

                    {/* Applies to a template-based campaign too, which is why it
                        sits outside the inline-content branch above. */}
                    <div className="pt-4 border-t border-ink-100">
                        <Toggle
                            label="Include a way to unsubscribe"
                            description="Adds your opt-out line, or an unsubscribe button, to every message in this campaign. How it looks is set on the Compliance page."
                            checked={form.append_opt_out}
                            onChange={(e) => setForm({ ...form, append_opt_out: e.target.checked })}
                        />
                        {!form.append_opt_out && (
                            <p className="text-xs text-amber-700 bg-amber-50 rounded-md px-2 py-1.5 mt-2 mb-0">
                                Recipients with no visible way out tend to use the block button instead, which counts
                                against the number far more than an unsubscribe does.
                            </p>
                        )}
                    </div>

                    <div className="pt-4 border-t border-ink-100">
                        <Toggle
                            label="Track link clicks"
                            description="Shortens every link so you can see who clicked, and tags them for your own analytics."
                            checked={form.track_links}
                            onChange={(e) => setForm({ ...form, track_links: e.target.checked })}
                        />
                        {!form.track_links && (
                            <p className="text-xs text-ink-500 mt-2 mb-0">
                                Links go out exactly as written. This campaign will be able to report that it was
                                delivered and read, and nothing about whether it worked.
                            </p>
                        )}
                    </div>
                </Step>

                <Step number={4} icon={CalendarClock} title="Schedule" description="Send now or pick a future time.">
                    <Field label="Send at" hint="Leave blank to launch immediately after review.">
                        <Input
                            type="datetime-local"
                            value={form.scheduled_at}
                            onChange={(e) => setForm({ ...form, scheduled_at: e.target.value })}
                        />
                    </Field>

                    <div className="pt-4 border-t border-ink-100">
                        <p className="text-sm font-semibold text-ink-800 m-0">Message Delay</p>
                        <p className="text-xs text-ink-500 mt-0.5 mb-3">
                            A new random wait is generated before every single message, somewhere between these two
                            values. Leave blank to use the system default.
                        </p>
                        <div className="grid gap-4 sm:grid-cols-2">
                            <Field label="Minimum" hint="seconds" className="mb-0">
                                <Input
                                    type="number"
                                    min="0"
                                    step="0.1"
                                    placeholder="1"
                                    value={form.min_delay_seconds}
                                    onChange={(e) => setForm({ ...form, min_delay_seconds: e.target.value })}
                                />
                            </Field>
                            <Field label="Maximum" hint="seconds" className="mb-0" error={delayError}>
                                <Input
                                    type="number"
                                    min="0"
                                    step="0.1"
                                    placeholder="5"
                                    value={form.max_delay_seconds}
                                    onChange={(e) => setForm({ ...form, max_delay_seconds: e.target.value })}
                                    invalid={Boolean(delayError)}
                                />
                            </Field>
                        </div>
                    </div>
                </Step>

                <div className="flex flex-col-reverse sm:flex-row gap-2 sm:justify-end sticky bottom-0 bg-ink-50 py-4">
                    <Button type="button" variant="secondary" onClick={() => navigate("/campaigns")}>
                        Cancel
                    </Button>
                    <Button
                        type="submit"
                        size="lg"
                        loading={saving}
                        disabled={connectedAccounts.length === 0 || Boolean(delayError)}
                    >
                        Create Draft & Review
                    </Button>
                </div>
            </form>
        </div>
    );
}
