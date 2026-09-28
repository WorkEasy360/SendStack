import { useState } from "react";
import { ClipboardPaste, Copy, Eraser, ScanLine, Trash2, X } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../ui/Alert";
import Badge from "../ui/Badge";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import { Field, Input, Textarea } from "../ui/Field";

/**
 * Import contacts from a list somebody pasted in.
 *
 * The CSV importer already handles a list that exists as a file. This is for
 * the far more common case: a WhatsApp message, a spreadsheet column, or a
 * page of a notebook that somebody has in front of them.
 *
 * **Nothing is created until Import is pressed.** The review table is the
 * feature, not a loading state: a paste of a hundred numbers with four typos
 * in it should be corrected before it becomes a hundred contacts, and an
 * importer that saved as it parsed would leave those corrections to be made
 * afterwards against rows that already exist.
 *
 * The parse is the server's, not the browser's. That matters for the two
 * statuses the browser could not know — whether a number is already on the
 * account, and whether that contact has unsubscribed — and it means the
 * classification shown here is the one the import will act on.
 */

const STATUS_TONE = {
    NEW: "green",
    EXISTING: "gray",
    UNSUBSCRIBED: "amber",
    DUPLICATE: "amber",
    INVALID: "red",
    NO_COUNTRY_CODE: "red",
    EMPTY: "red",
};

const SAMPLE = `9876543210
9876543211

Aman,9876543212
Rahul,9876543213`;

export default function PasteImport({
    onClose,
    onImported,
    groups = [],
    tags = [],
    // The three pieces of copy that change with what the paste is *for*. The
    // campaign wizard pastes a list in order to send to it, not in order to
    // have it, and "Import 40 contacts" is the wrong promise on that screen —
    // but every rule about how the list is read, reviewed and committed is
    // the same, so it is the words that vary here and nothing else.
    title = "Paste contacts",
    description = "One number per line, or a name and a number. Nothing is saved until you press Import.",
    submitLabel = (count) => `Import ${count.toLocaleString()} contact${count === 1 ? "" : "s"}`,
}) {
    const [text, setText] = useState("");
    const [countryCode, setCountryCode] = useState("91");
    const [rows, setRows] = useState(null);
    const [summary, setSummary] = useState(null);
    const [quotaWarning, setQuotaWarning] = useState("");
    const [groupIds, setGroupIds] = useState([]);
    const [tagIds, setTagIds] = useState([]);

    const [checking, setChecking] = useState(false);
    const [importing, setImporting] = useState(false);
    const [error, setError] = useState("");

    const check = async () => {
        setChecking(true);
        setError("");
        try {
            const response = await api.post("/contacts/imports/parse/", {
                text,
                default_country_code: countryCode,
            });
            setRows(response.data.rows);
            setSummary(response.data.summary);
            setQuotaWarning(response.data.quota_warning || "");
        } catch (err) {
            setError(apiErrorMessage(err, "Could not read that list."));
        } finally {
            setChecking(false);
        }
    };

    // Every "remove" acts on the review table only. The paste itself is left
    // alone so somebody can go back, fix a typo and check again without
    // losing the rows they already pruned.
    const removeRow = (line) => setRows(rows.filter((row) => row.line !== line));
    const removeWhere = (predicate) => setRows(rows.filter((row) => !predicate(row)));

    // Distinct by number, not merely "importable". A row still marked as a
    // repeat is harmless to send — the server imports each number once — but
    // counting it would put "Import 4 contacts" on a button that creates
    // three, and a count that does not match the outcome is the one thing a
    // confirmation button must not do.
    const keepable = [];
    const seen = new Set();
    for (const row of rows || []) {
        if (!row.importable || seen.has(row.phone)) continue;
        seen.add(row.phone);
        keepable.push(row);
    }

    const duplicates = (rows || []).filter((row) => row.status === "DUPLICATE").length;
    const unusable = (rows || []).filter((row) => !row.importable).length;

    const runImport = async (event) => {
        event.preventDefault();
        setImporting(true);
        setError("");
        try {
            const response = await api.post("/contacts/imports/paste/", {
                rows: keepable.map((row) => ({ name: row.name, phone: row.phone })),
                group_ids: groupIds,
                tag_ids: tagIds,
            });
            onImported(response.data);
        } catch (err) {
            setError(apiErrorMessage(err, "Those contacts could not be imported."));
        } finally {
            setImporting(false);
        }
    };

    return (
        <Modal title={title} description={description} width="xl" onClose={onClose}>
            <form onSubmit={runImport}>
                <Alert>{error}</Alert>

                {!rows && (
                    <>
                        <Field
                            label="Your list"
                            required
                            hint="Commas, tabs, semicolons and dashes all work, in either order."
                        >
                            <Textarea
                                value={text}
                                rows={9}
                                placeholder={SAMPLE}
                                className="font-mono text-xs"
                                onChange={(event) => setText(event.target.value)}
                                required
                            />
                        </Field>

                        <Field
                            label="Country code"
                            hint="Used for numbers written without one. A number starting with + keeps its own."
                        >
                            <div className="flex items-center gap-2">
                                <span className="text-sm font-semibold text-ink-500">+</span>
                                <Input
                                    value={countryCode}
                                    inputMode="numeric"
                                    maxLength={4}
                                    className="max-w-[100px]"
                                    onChange={(event) =>
                                        setCountryCode(event.target.value.replace(/\D/g, ""))
                                    }
                                />
                            </div>
                        </Field>

                        <div className="flex justify-end gap-2">
                            <Button type="button" variant="secondary" onClick={onClose}>
                                Cancel
                            </Button>
                            <Button
                                type="button"
                                icon={ScanLine}
                                loading={checking}
                                disabled={!text.trim()}
                                onClick={check}
                            >
                                Check list
                            </Button>
                        </div>
                    </>
                )}

                {rows && (
                    <>
                        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                            {[
                                { label: "New", value: summary.new, tone: "text-brand-700" },
                                { label: "Already here", value: summary.existing, tone: "text-ink-600" },
                                { label: "Repeated", value: summary.duplicates, tone: "text-amber-600" },
                                {
                                    label: "Unusable",
                                    value: summary.invalid + summary.no_country_code + summary.empty,
                                    tone:
                                        summary.invalid + summary.no_country_code + summary.empty
                                            ? "text-red-600"
                                            : "text-ink-400",
                                },
                            ].map((stat) => (
                                <div key={stat.label} className="rounded-card border border-ink-200 p-3">
                                    <p className={`m-0 text-xl font-bold tabular-nums ${stat.tone}`}>
                                        {stat.value.toLocaleString()}
                                    </p>
                                    <p className="mt-0.5 mb-0 text-xs text-ink-500">{stat.label}</p>
                                </div>
                            ))}
                        </div>

                        {quotaWarning && <Alert tone="warning">{quotaWarning}</Alert>}

                        <div className="mb-3 flex flex-wrap gap-2">
                            <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                icon={Copy}
                                disabled={!duplicates}
                                onClick={() => removeWhere((row) => row.status === "DUPLICATE")}
                            >
                                Remove duplicates
                            </Button>
                            <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                icon={Eraser}
                                disabled={!unusable}
                                onClick={() => removeWhere((row) => !row.importable)}
                            >
                                Remove unusable
                            </Button>
                            <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                icon={Trash2}
                                onClick={() => {
                                    setRows(null);
                                    setSummary(null);
                                }}
                            >
                                Clear all
                            </Button>
                        </div>

                        <div className="mb-4 max-h-72 overflow-auto rounded-lg border border-ink-200">
                            <table className="w-full border-collapse text-sm">
                                <thead className="sticky top-0 bg-ink-50">
                                    <tr className="text-left text-xs font-semibold uppercase tracking-wide text-ink-500">
                                        <th className="px-3 py-2">Sr</th>
                                        <th className="px-3 py-2">Name</th>
                                        <th className="px-3 py-2">Number</th>
                                        <th className="px-3 py-2">Status</th>
                                        <th className="px-3 py-2 text-right">Action</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {rows.map((row, index) => (
                                        <tr
                                            key={row.line}
                                            className={`border-t border-ink-100 ${
                                                row.importable ? "" : "bg-red-50/50"
                                            }`}
                                        >
                                            <td className="px-3 py-1.5 tabular-nums text-ink-400">{index + 1}</td>
                                            <td className="px-3 py-1.5 text-ink-800">
                                                {row.name || <span className="text-ink-300">—</span>}
                                            </td>
                                            <td className="px-3 py-1.5 font-mono text-xs tabular-nums text-ink-800">
                                                {/* The raw line when there is no
                                                    number to show: somebody
                                                    fixing a typo needs to see
                                                    what they actually typed. */}
                                                {row.phone || (
                                                    <span className="text-ink-400">{row.raw}</span>
                                                )}
                                            </td>
                                            <td className="px-3 py-1.5">
                                                <Badge tone={STATUS_TONE[row.status] || "gray"}>{row.detail}</Badge>
                                            </td>
                                            <td className="px-3 py-1.5 text-right">
                                                <Button
                                                    type="button"
                                                    variant="ghost"
                                                    size="sm"
                                                    icon={X}
                                                    className="text-red-600 hover:bg-red-50 hover:text-red-700"
                                                    onClick={() => removeRow(row.line)}
                                                    aria-label={`Remove row ${index + 1}`}
                                                />
                                            </td>
                                        </tr>
                                    ))}
                                    {!rows.length && (
                                        <tr>
                                            <td colSpan={5} className="px-3 py-6 text-center text-ink-500">
                                                Every row has been removed.
                                            </td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                        </div>

                        {(groups.length > 0 || tags.length > 0) && (
                            <div className="mb-4 grid gap-3 sm:grid-cols-2">
                                {groups.length > 0 && (
                                    <Field label="Add to groups" className="mb-0">
                                        <div className="flex flex-wrap gap-1.5">
                                            {groups.map((group) => (
                                                <button
                                                    key={group.id}
                                                    type="button"
                                                    onClick={() =>
                                                        setGroupIds((current) =>
                                                            current.includes(group.id)
                                                                ? current.filter((id) => id !== group.id)
                                                                : [...current, group.id],
                                                        )
                                                    }
                                                    className={`rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
                                                        groupIds.includes(group.id)
                                                            ? "border-brand-500 bg-brand-50 text-brand-700"
                                                            : "border-ink-200 text-ink-600 hover:border-ink-300"
                                                    }`}
                                                >
                                                    {group.name}
                                                </button>
                                            ))}
                                        </div>
                                    </Field>
                                )}
                                {tags.length > 0 && (
                                    <Field label="Add tags" className="mb-0">
                                        <div className="flex flex-wrap gap-1.5">
                                            {tags.map((tag) => (
                                                <button
                                                    key={tag.id}
                                                    type="button"
                                                    onClick={() =>
                                                        setTagIds((current) =>
                                                            current.includes(tag.id)
                                                                ? current.filter((id) => id !== tag.id)
                                                                : [...current, tag.id],
                                                        )
                                                    }
                                                    className={`rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
                                                        tagIds.includes(tag.id)
                                                            ? "border-brand-500 bg-brand-50 text-brand-700"
                                                            : "border-ink-200 text-ink-600 hover:border-ink-300"
                                                    }`}
                                                >
                                                    {tag.name}
                                                </button>
                                            ))}
                                        </div>
                                    </Field>
                                )}
                            </div>
                        )}

                        <div className="flex justify-end gap-2">
                            <Button type="button" variant="secondary" onClick={onClose}>
                                Cancel
                            </Button>
                            <Button
                                type="submit"
                                icon={ClipboardPaste}
                                loading={importing}
                                disabled={!keepable.length}
                            >
                                {submitLabel(keepable.length)}
                            </Button>
                        </div>
                    </>
                )}
            </form>
        </Modal>
    );
}
