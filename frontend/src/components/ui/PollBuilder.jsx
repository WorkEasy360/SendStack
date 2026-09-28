import { CircleDot, Plus, X } from "lucide-react";
import Button from "./Button";
import { Field, Input, Toggle } from "./Field";
import {
    MAX_POLL_OPTIONS,
    MAX_POLL_OPTION_LENGTH,
    MAX_POLL_QUESTION_LENGTH,
    MIN_POLL_OPTIONS,
} from "../../utils/interactive";

/**
 * Editor for a poll — the `poll_content` payload the API takes.
 *
 * A poll has no body of its own: the question *is* the message, and WhatsApp
 * renders it as a bubble by itself. That is why a poll template is the one
 * kind that saves with no message text.
 *
 * Duplicate options are flagged here rather than only on save, because a poll
 * with two identical answers produces a result nobody can read and the mistake
 * is invisible in a long list.
 */
export default function PollBuilder({ value, onChange, disabled = false }) {
    const content = value || { question: "", options: ["", ""], allow_multiple: false };
    const options = content.options || [];

    const patch = (changes) => onChange({ ...content, ...changes });

    const updateOption = (index, text) => {
        patch({ options: options.map((option, position) => (position === index ? text : option)) });
    };

    const addOption = () => patch({ options: [...options, ""] });

    const removeOption = (index) => {
        patch({ options: options.filter((_, position) => position !== index) });
    };

    const folded = options.map((option) => option.trim().toLowerCase()).filter(Boolean);
    const duplicated = new Set(folded).size !== folded.length;

    return (
        <div className="mb-4 rounded-lg border border-ink-200 bg-ink-50/60 p-3">
            <Field label="Question" required hint="This is the message — a poll has no separate body text.">
                <Input
                    value={content.question || ""}
                    maxLength={MAX_POLL_QUESTION_LENGTH}
                    placeholder="Which service are you interested in?"
                    disabled={disabled}
                    onChange={(event) => patch({ question: event.target.value })}
                />
            </Field>

            <span className="mb-1.5 block text-sm font-semibold text-ink-700">Options</span>
            <ul className="m-0 list-none space-y-1.5 p-0">
                {options.map((option, index) => (
                    <li key={index} className="flex items-center gap-2">
                        <CircleDot size={14} aria-hidden="true" className="shrink-0 text-ink-400" />
                        <input
                            value={option}
                            maxLength={MAX_POLL_OPTION_LENGTH}
                            placeholder={`Option ${index + 1}`}
                            disabled={disabled}
                            onChange={(event) => updateOption(index, event.target.value)}
                            className="w-full rounded-md border border-ink-200 bg-surface px-2 py-1.5 text-xs text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/15"
                        />
                        <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            icon={X}
                            disabled={disabled || options.length <= MIN_POLL_OPTIONS}
                            className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                            onClick={() => removeOption(index)}
                            aria-label={`Remove option ${index + 1}`}
                        />
                    </li>
                ))}
            </ul>

            <div className="mt-2 flex flex-wrap items-center gap-2.5">
                <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    icon={Plus}
                    disabled={disabled || options.length >= MAX_POLL_OPTIONS}
                    onClick={addOption}
                >
                    Add option
                </Button>
                <span className="text-xs text-ink-500">
                    {options.length} of {MAX_POLL_OPTIONS}
                </span>
            </div>

            {duplicated && (
                <p className="mt-2 mb-0 text-xs font-medium text-red-600">
                    Two options are the same. Every answer has to be different, or the result cannot be read.
                </p>
            )}

            <div className="mt-3 border-t border-ink-200 pt-3">
                <Toggle
                    checked={Boolean(content.allow_multiple)}
                    disabled={disabled}
                    onChange={(event) => patch({ allow_multiple: event.target.checked })}
                    label="Allow several answers"
                    description="Let the recipient pick more than one option."
                />
            </div>
        </div>
    );
}
