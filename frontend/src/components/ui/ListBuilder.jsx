import { GripVertical, Plus, Rows3, X } from "lucide-react";
import Button from "./Button";
import { Field, Input } from "./Field";
import {
    MAX_LIST_BUTTON_LENGTH,
    MAX_LIST_HEADER_LENGTH,
    MAX_LIST_ROWS,
    MAX_LIST_SECTIONS,
    MAX_ROW_DESCRIPTION_LENGTH,
    MAX_ROW_TITLE_LENGTH,
    MAX_SECTION_TITLE_LENGTH,
    deriveRowId,
    listRows,
} from "../../utils/interactive";

/**
 * Editor for a tappable menu — the `list_content` payload the API takes.
 *
 * The limits shown are WhatsApp's, not this product's, and they are enforced
 * here as well as on save for one reason: ten rows is a *total* across every
 * section, which is not what anybody assumes, and discovering it after
 * building a four-section menu is a worse experience than being stopped at the
 * eleventh row.
 *
 * A row's `id` is what comes back when the recipient picks it, and what
 * automation rules match on. It is derived from the label once, on blur, and
 * never rewritten afterwards — relabelling a saved row must not unhook the
 * rules pointing at it. Same rule as ButtonBuilder's.
 */

function SmallInput({ className = "", ...props }) {
    // Deliberately not <Input>: appending `py-1.5 text-xs` to it would lose to
    // its own `py-2.5 text-sm` on Tailwind source order, so the row would
    // silently stay full height.
    return (
        <input
            className={`w-full rounded-md border border-ink-200 bg-surface px-2 py-1.5 text-xs text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/15 ${className}`}
            {...props}
        />
    );
}

export default function ListBuilder({ value, onChange, disabled = false }) {
    const content = value || { header: "", footer: "", button: "", sections: [] };
    const sections = content.sections || [];
    const totalRows = listRows(content).length;
    const multipleSections = sections.length > 1;

    const patch = (changes) => onChange({ ...content, ...changes });

    const updateSection = (index, changes) => {
        patch({
            sections: sections.map((section, position) =>
                position === index ? { ...section, ...changes } : section,
            ),
        });
    };

    const updateRow = (sectionIndex, rowIndex, changes) => {
        updateSection(sectionIndex, {
            rows: sections[sectionIndex].rows.map((row, position) =>
                position === rowIndex ? { ...row, ...changes } : row,
            ),
        });
    };

    /**
     * Fill in a blank id once the label is finished, not while it is being
     * typed: deriving on every keystroke turns "Web development" into the id
     * "w", because the first character is already a complete-looking label.
     */
    const fillBlankId = (sectionIndex, rowIndex) => {
        const row = sections[sectionIndex].rows[rowIndex];
        if (row.id || !row.title.trim()) return;
        const taken = listRows(content)
            .filter((other) => other !== row)
            .map((other) => other.id)
            .filter(Boolean);
        updateRow(sectionIndex, rowIndex, { id: deriveRowId(row.title, taken) });
    };

    const addRow = (sectionIndex) => {
        updateSection(sectionIndex, {
            rows: [...sections[sectionIndex].rows, { id: "", title: "", description: "" }],
        });
    };

    const removeRow = (sectionIndex, rowIndex) => {
        const remaining = sections[sectionIndex].rows.filter((_, position) => position !== rowIndex);
        if (!remaining.length && sections.length > 1) {
            // A section with no rows cannot be sent, so removing the last row
            // removes the section it was in rather than leaving an empty one
            // the save would reject.
            return patch({ sections: sections.filter((_, position) => position !== sectionIndex) });
        }
        updateSection(sectionIndex, { rows: remaining });
    };

    const addSection = () => {
        patch({
            sections: [...sections, { title: "", rows: [{ id: "", title: "", description: "" }] }],
        });
    };

    const removeSection = (index) => {
        patch({ sections: sections.filter((_, position) => position !== index) });
    };

    return (
        <div className="@container mb-4 rounded-lg border border-ink-200 bg-ink-50/60 p-3">
            <div className="mb-3 grid gap-3 @md:grid-cols-2">
                <Field label="Header" hint="Optional. Shown in bold above the message." className="mb-0">
                    <Input
                        value={content.header || ""}
                        maxLength={MAX_LIST_HEADER_LENGTH}
                        placeholder="Choose a service"
                        disabled={disabled}
                        onChange={(event) => patch({ header: event.target.value })}
                    />
                </Field>
                <Field
                    label="Menu button"
                    required
                    hint="What the recipient taps to open the menu."
                    className="mb-0"
                >
                    <Input
                        value={content.button || ""}
                        maxLength={MAX_LIST_BUTTON_LENGTH}
                        placeholder="View services"
                        disabled={disabled}
                        onChange={(event) => patch({ button: event.target.value })}
                    />
                </Field>
            </div>

            <div className="space-y-2.5">
                {sections.map((section, sectionIndex) => (
                    <div key={sectionIndex} className="rounded-lg border border-ink-200 bg-surface p-2.5">
                        <div className="mb-2 flex items-center gap-2">
                            <Rows3 size={14} aria-hidden="true" className="shrink-0 text-ink-400" />
                            <SmallInput
                                value={section.title || ""}
                                maxLength={MAX_SECTION_TITLE_LENGTH}
                                placeholder={
                                    multipleSections ? "Section title (required)" : "Section title (optional)"
                                }
                                disabled={disabled}
                                onChange={(event) => updateSection(sectionIndex, { title: event.target.value })}
                            />
                            {sections.length > 1 && (
                                <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    icon={X}
                                    disabled={disabled}
                                    className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                                    onClick={() => removeSection(sectionIndex)}
                                    aria-label={`Remove section ${sectionIndex + 1}`}
                                />
                            )}
                        </div>

                        <ul className="m-0 list-none space-y-1.5 p-0">
                            {(section.rows || []).map((row, rowIndex) => (
                                <li key={rowIndex} className="flex items-start gap-2">
                                    <GripVertical
                                        size={14}
                                        aria-hidden="true"
                                        className="mt-2 shrink-0 text-ink-300"
                                    />
                                    <div className="grid min-w-0 flex-1 gap-1.5 @lg:grid-cols-3">
                                        <div>
                                            <SmallInput
                                                value={row.title}
                                                maxLength={MAX_ROW_TITLE_LENGTH}
                                                placeholder="Option label"
                                                disabled={disabled}
                                                onChange={(event) =>
                                                    updateRow(sectionIndex, rowIndex, { title: event.target.value })
                                                }
                                                onBlur={() => fillBlankId(sectionIndex, rowIndex)}
                                            />
                                            <span className="mt-1 block text-[11px] text-ink-400">
                                                {row.title.length}/{MAX_ROW_TITLE_LENGTH}
                                            </span>
                                        </div>
                                        <SmallInput
                                            value={row.description || ""}
                                            maxLength={MAX_ROW_DESCRIPTION_LENGTH}
                                            placeholder="Description (optional)"
                                            disabled={disabled}
                                            onChange={(event) =>
                                                updateRow(sectionIndex, rowIndex, {
                                                    description: event.target.value,
                                                })
                                            }
                                        />
                                        <div>
                                            <SmallInput
                                                value={row.id || ""}
                                                placeholder="row_id"
                                                disabled={disabled}
                                                className="font-mono"
                                                onChange={(event) =>
                                                    updateRow(sectionIndex, rowIndex, { id: event.target.value })
                                                }
                                            />
                                            <span className="mt-1 block text-[11px] text-ink-400">
                                                Automation matches this id
                                            </span>
                                        </div>
                                    </div>
                                    <Button
                                        type="button"
                                        variant="ghost"
                                        size="sm"
                                        icon={X}
                                        disabled={disabled || (sections.length === 1 && section.rows.length === 1)}
                                        className="shrink-0 text-red-600 hover:bg-red-50 hover:text-red-700"
                                        onClick={() => removeRow(sectionIndex, rowIndex)}
                                        aria-label={`Remove row ${rowIndex + 1}`}
                                    />
                                </li>
                            ))}
                        </ul>

                        <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            icon={Plus}
                            className="mt-2"
                            disabled={disabled || totalRows >= MAX_LIST_ROWS}
                            onClick={() => addRow(sectionIndex)}
                        >
                            Add row
                        </Button>
                    </div>
                ))}
            </div>

            <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
                <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    icon={Plus}
                    disabled={disabled || sections.length >= MAX_LIST_SECTIONS || totalRows >= MAX_LIST_ROWS}
                    onClick={addSection}
                >
                    Add section
                </Button>
                <span className={`text-xs ${totalRows > MAX_LIST_ROWS ? "text-red-600" : "text-ink-500"}`}>
                    {totalRows} of {MAX_LIST_ROWS} rows — WhatsApp counts the whole menu, not each section.
                </span>
            </div>

            <Field label="Footer" hint="Optional. Small print under the menu." className="mt-3 mb-0">
                <Input
                    value={content.footer || ""}
                    maxLength={MAX_LIST_HEADER_LENGTH}
                    placeholder="Reply anytime"
                    disabled={disabled}
                    onChange={(event) => patch({ footer: event.target.value })}
                />
            </Field>
        </div>
    );
}
