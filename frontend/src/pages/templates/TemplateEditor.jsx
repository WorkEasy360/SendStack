import { useCallback, useEffect, useRef, useState } from "react";
import { BarChart3, List, MessageSquare, MousePointerClick } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { toButtons } from "../../utils/buttons";
import { primaryMessageType, toMediaItems } from "../../utils/media";
import {
    BUTTON,
    LIST,
    POLL,
    TEXT,
    contentKind,
    contentPayload,
    contentProblem,
    emptyList,
    emptyPoll,
    requiredCapability,
} from "../../utils/interactive";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import ButtonBuilder from "../../components/ui/ButtonBuilder";
import ListBuilder from "../../components/ui/ListBuilder";
import MediaPicker from "../../components/ui/MediaPicker";
import MessageComposer from "../../components/ui/MessageComposer";
import PollBuilder from "../../components/ui/PollBuilder";
import WhatsAppPreview from "../../components/ui/WhatsAppPreview";
import { Field, Input, Select } from "../../components/ui/Field";

/**
 * Compose one message: pick what kind it is, fill in only the fields that kind
 * needs, and watch it being built.
 *
 * **The type selector is derived, not stored.** A template is a BUTTON message
 * because it has buttons, a LIST because it has a menu, a POLL because it has
 * a poll — so a template saved before any of this existed opens as exactly
 * what it always was, with no migration and no column that could disagree with
 * the content beside it. Switching type clears the other kinds, because
 * WhatsApp has no envelope that carries two.
 *
 * Attachments are orthogonal: every type except a poll can carry them, which
 * is what makes "Text + Media", "Buttons + Media" and so on combinations
 * rather than eight separate types to choose between.
 *
 * The preview redraws locally on every keystroke. The one thing it asks the
 * server for is how the message will arrive on the *selected number* — that
 * answer is produced by the same functions the sender calls, so it cannot
 * drift from what a recipient receives.
 */

const TYPES = [
    { value: TEXT, label: "Text", icon: MessageSquare, capability: "" },
    { value: BUTTON, label: "Buttons", icon: MousePointerClick, capability: "buttons" },
    { value: LIST, label: "Menu", icon: List, capability: "lists" },
    { value: POLL, label: "Poll", icon: BarChart3, capability: "polls" },
];

export const EMPTY_TEMPLATE = {
    name: "",
    message_type: TEXT,
    body: "",
    media_items: [],
    buttons: [],
    list_content: {},
    poll_content: {},
    variables: {},
};

/** A saved template as the editor's form. */
export function templateToForm(template) {
    return {
        name: template.name,
        message_type: template.message_type,
        body: template.body || "",
        // Hydrated through toMediaItems so a template saved with the old
        // single media_url opens with that file already attached.
        media_items: toMediaItems(template),
        buttons: toButtons(template),
        list_content: template.list_content || {},
        poll_content: template.poll_content || {},
        variables: template.variables || {},
    };
}

/** What the API is sent, with the unused content kinds explicitly cleared. */
export function formToPayload(form) {
    return {
        name: form.name,
        message_type: form.message_type,
        body: form.body,
        media_items: form.media_items,
        variables: form.variables,
        ...contentPayload(form),
    };
}

export default function TemplateEditor({ form, onChange, accounts = [], variables, nameField = true }) {
    const kind = contentKind(form);
    const [accountId, setAccountId] = useState(() => accounts[0]?.id ?? "");
    const [delivery, setDelivery] = useState(null);
    const requestId = useRef(0);

    const patch = useCallback((changes) => onChange({ ...form, ...changes }), [form, onChange]);

    const setKind = (next) => {
        if (next === kind) return;
        // Switching replaces rather than converts: a menu row has a
        // description and an id, a poll option has neither, and there is
        // nothing meaningful to carry between them.
        patch({
            buttons: next === BUTTON ? [{ type: "REPLY", id: "", title: "", url: "", phone: "" }] : [],
            list_content: next === LIST ? emptyList() : {},
            poll_content: next === POLL ? emptyPoll() : {},
        });
    };

    const setMedia = (media_items) => {
        // The API derives message_type from the first attachment, so the form
        // shows the same thing rather than a type the save would overwrite.
        patch({
            media_items,
            message_type: primaryMessageType(media_items, media_items.length ? form.message_type : TEXT),
        });
    };

    const capability = requiredCapability(form);

    // Debounced, and only when there is something a connection could refuse.
    // A plain text message needs no capability, so it needs no round trip.
    useEffect(() => {
        if (!capability || !accountId) {
            setDelivery(null);
            return undefined;
        }

        const id = ++requestId.current;
        const timer = setTimeout(async () => {
            try {
                const response = await api.post("/templates/templates/draft-preview/", {
                    body: form.body,
                    buttons: form.buttons,
                    list_content: form.list_content,
                    poll_content: form.poll_content,
                    variables: form.variables,
                    whatsapp_account_id: accountId,
                });
                // Stale responses are dropped rather than rendered: a slow
                // reply for content two edits ago would show a fallback for a
                // message that no longer exists.
                if (id === requestId.current) setDelivery(response.data.delivery);
            } catch {
                // A preview that cannot be fetched shows the composed message
                // rather than an error -- the content is still valid, and the
                // send path is what actually decides.
                if (id === requestId.current) setDelivery(null);
            }
        }, 400);

        return () => clearTimeout(timer);
    }, [capability, accountId, form.body, form.buttons, form.list_content, form.poll_content, form.variables]);

    const problem = contentProblem(form);
    const selected = accounts.find((account) => account.id === accountId);

    return (
        /*
         * The @container is a *wrapper*, not the grid itself: an element that
         * declares `container-type` is a container for its descendants and not
         * for itself, so `@3xl:` on the same div would query the nearest
         * ancestor container instead and silently never match.
         *
         * Container queries rather than `lg:` because this editor is used both
         * in a modal and inside the campaign wizard — what the layout depends
         * on is how much room it has, not how wide the window is.
         */
        <div className="@container">
            <div className="grid gap-5 @3xl:grid-cols-[minmax(0,1fr)_300px]">
                <div className="min-w-0">
                {/* Absent inside the campaign wizard, which already asked for
                    a name in its first step. */}
                {nameField && (
                    <Field label="Name" required>
                        <Input
                            value={form.name}
                            onChange={(event) => patch({ name: event.target.value })}
                            placeholder="Order ready"
                            autoFocus
                            required
                        />
                    </Field>
                )}

                <span className="mb-1.5 block text-sm font-semibold text-ink-700">Message type</span>
                <div className="mb-4 inline-flex flex-wrap rounded-lg border border-ink-200 bg-ink-50 p-0.5">
                    {TYPES.map(({ value, label, icon: Icon, capability: needs }) => {
                        const unsupported =
                            needs && selected && selected.capabilities && !selected.capabilities[needs];
                        return (
                            <button
                                key={value}
                                type="button"
                                onClick={() => setKind(value)}
                                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
                                    kind === value
                                        ? "bg-surface text-ink-900 shadow-card"
                                        : "text-ink-500 hover:text-ink-800"
                                }`}
                            >
                                <Icon size={13} aria-hidden="true" />
                                {label}
                                {/* Not disabled: the message still sends, as a
                                    numbered list. Marking it is honest;
                                    forbidding it would be wrong. */}
                                {unsupported && (
                                    <span
                                        className="h-1.5 w-1.5 rounded-full bg-amber-500"
                                        title={`Not shown natively on ${selected.name}`}
                                        aria-label={`Not shown natively on ${selected.name}`}
                                    />
                                )}
                            </button>
                        );
                    })}
                </div>

                {kind !== POLL && (
                    <MessageComposer
                        value={form.body}
                        onChange={(body) => patch({ body })}
                        variables={variables}
                        required={kind === LIST}
                        placeholder="Hello {{name}}, your order {{order_id}} is ready."
                        hint={
                            kind === LIST
                                ? "The menu opens from a button under this text, so it is required."
                                : "Use {{name}} or [Name] for personalization. Formatting is WhatsApp's own."
                        }
                    />
                )}

                {kind === BUTTON && (
                    <ButtonBuilder
                        value={form.buttons}
                        onChange={(buttons) => patch({ buttons })}
                        provider={selected?.provider}
                    />
                )}
                {kind === LIST && (
                    <ListBuilder value={form.list_content} onChange={(list_content) => patch({ list_content })} />
                )}
                {kind === POLL && (
                    <PollBuilder value={form.poll_content} onChange={(poll_content) => patch({ poll_content })} />
                )}

                {kind !== POLL && <MediaPicker value={form.media_items} onChange={setMedia} />}

                {problem && (
                    <Alert tone="warning" className="mb-4">
                        {problem}
                    </Alert>
                )}
            </div>

                <div className="@3xl:sticky @3xl:top-4 @3xl:self-start">
                {accounts.length > 1 && (
                    <Field label="Preview for" className="mb-3">
                        <Select value={accountId} onChange={(event) => setAccountId(Number(event.target.value))}>
                            {accounts.map((account) => (
                                <option key={account.id} value={account.id}>
                                    {account.name}
                                </option>
                            ))}
                        </Select>
                    </Field>
                )}
                    <WhatsAppPreview form={form} delivery={delivery} />
                </div>
            </div>
        </div>
    );
}
