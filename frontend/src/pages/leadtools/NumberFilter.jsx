import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Download, FolderPlus, ScanSearch, Smartphone, XCircle } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { downloadFile } from "../../utils/download";
import FeatureGate from "../../components/billing/FeatureLock";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import Modal from "../../components/ui/Modal";
import PageHeader from "../../components/ui/PageHeader";
import Tabs from "../../components/ui/Tabs";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * The number filter: which numbers on a list are actually on WhatsApp.
 *
 * A check is a job, not a request. It runs on the worker in paced batches
 * -- a linked phone that fires thousands of lookups back to back looks like
 * the scraping WhatsApp bans numbers for -- so this screen creates one,
 * polls it while it runs, and then offers the three things people do with
 * the answer: read it, export it, and save the reachable numbers into a
 * contact group they can send to.
 *
 * Nothing here is consent. The save goes through the ordinary importer with
 * consent left unrecorded, exactly like a pasted list; the copy says so.
 */

const RESULT_LABEL = {
    ON_WHATSAPP: "On WhatsApp",
    NOT_ON_WHATSAPP: "Not on WhatsApp",
    ERROR: "Could not check",
    PENDING: "Waiting",
};
const RESULT_TONE = { ON_WHATSAPP: "green", NOT_ON_WHATSAPP: "red", ERROR: "amber", PENDING: "gray" };
const STATUS_LABEL = { PENDING: "Queued", RUNNING: "Checking", COMPLETED: "Done", FAILED: "Stopped" };
const STATUS_TONE = { PENDING: "gray", RUNNING: "blue", COMPLETED: "green", FAILED: "red" };
const SOURCE_LABEL = { PASTE: "Pasted list", CONTACTS: "Contacts" };
const LIVE = new Set(["PENDING", "RUNNING"]);
const POLL_MS = 2500;
const SAMPLE = "+919876543210\n9876543211\nAman, 9876543212";

function when(iso) {
    return iso ? new Date(iso).toLocaleString() : "";
}

function Stat({ label, value, tone = "text-ink-900" }) {
    return (
        <div className="rounded-lg border border-ink-200 bg-surface p-3">
            <p className="text-xs font-medium text-ink-500 m-0">{label}</p>
            <p className={`text-xl font-bold m-0 mt-1 tabular-nums ${tone}`}>{(value ?? 0).toLocaleString()}</p>
        </div>
    );
}

export default function NumberFilter() {
    const toast = useToast();
    const [accounts, setAccounts] = useState([]);
    const [groups, setGroups] = useState([]);
    const [checks, setChecks] = useState([]);
    const [selected, setSelected] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [form, setForm] = useState({ account: "", source: "PASTE", text: "", countryCode: "91", group: "" });
    const [starting, setStarting] = useState(false);

    const [tab, setTab] = useState("ON_WHATSAPP");
    const [entries, setEntries] = useState({ results: [], count: 0, page: 1, next: null, previous: null });
    const [entriesLoading, setEntriesLoading] = useState(false);

    const [saveOpen, setSaveOpen] = useState(false);
    const [saveName, setSaveName] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        (async () => {
            try {
                const [accountsRes, groupsRes, checksRes] = await Promise.all([
                    api.get("/whatsapp/accounts/", { params: { page_size: 100 } }),
                    api.get("/contacts/groups/", { params: { page_size: 100 } }),
                    api.get("/leadtools/number-checks/"),
                ]);
                // Only a linked phone can ask WhatsApp who is registered; the
                // Cloud API has no such call, so those numbers are not offered.
                const usable = (accountsRes.data.results || []).filter(
                    (account) => account.connection_method === "QR" && account.status === "CONNECTED"
                );
                setAccounts(usable);
                setGroups(groupsRes.data.results || []);
                const list = checksRes.data.results || [];
                setChecks(list);
                setForm((current) => ({ ...current, account: usable[0]?.id || "" }));
                if (list.length) setSelected(list[0]);
            } catch (err) {
                setError(apiErrorMessage(err, "Unable to load the number filter."));
            } finally {
                setLoading(false);
            }
        })();
    }, []);

    // A running check is polled until it settles. Keyed on the id and the
    // status so a finished check stops costing a request every few seconds.
    const selectedId = selected?.id;
    const selectedStatus = selected?.status;
    useEffect(() => {
        if (!selectedId || !LIVE.has(selectedStatus)) return undefined;
        const timer = setInterval(async () => {
            try {
                const response = await api.get(`/leadtools/number-checks/${selectedId}/`);
                setSelected(response.data);
                setChecks((current) => current.map((check) => (check.id === response.data.id ? response.data : check)));
            } catch {
                // A missed poll is just a late update; the next one will land.
            }
        }, POLL_MS);
        return () => clearInterval(timer);
    }, [selectedId, selectedStatus]);

    const loadEntries = useCallback(async (checkId, result, page) => {
        setEntriesLoading(true);
        try {
            const response = await api.get(`/leadtools/number-checks/${checkId}/entries/`, { params: { result, page } });
            setEntries({
                results: response.data.results || [],
                count: response.data.count || 0,
                page,
                next: response.data.next,
                previous: response.data.previous,
            });
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load the results."));
        } finally {
            setEntriesLoading(false);
        }
    }, []);

    useEffect(() => {
        if (selectedId) loadEntries(selectedId, tab, 1);
    }, [selectedId, selectedStatus, tab, loadEntries]);

    const start = async (event) => {
        event.preventDefault();
        setStarting(true);
        setError("");
        try {
            const payload = { whatsapp_account: Number(form.account), source: form.source };
            if (form.source === "PASTE") {
                payload.text = form.text;
                payload.default_country_code = form.countryCode;
            } else if (form.group) {
                payload.contact_group = Number(form.group);
            }
            const response = await api.post("/leadtools/number-checks/", payload);
            setSelected(response.data);
            setChecks((current) => [response.data, ...current]);
            setTab("ON_WHATSAPP");
            toast.success(
                `Checking ${response.data.total.toLocaleString()} number${response.data.total === 1 ? "" : "s"} through ${
                    response.data.whatsapp_account_name
                }.`
            );
            if (form.source === "PASTE") setForm((current) => ({ ...current, text: "" }));
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to start the check."));
        } finally {
            setStarting(false);
        }
    };

    const exportResult = async (result) => {
        try {
            await downloadFile(
                `/leadtools/number-checks/${selected.id}/export/?result=${result}`,
                `number-check-${selected.id}-${result.toLowerCase()}.csv`
            );
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to export the results."));
        }
    };

    const openSave = () => {
        setSaveName(selected.source === "CONTACTS" && selected.contact_group_name
            ? `${selected.contact_group_name} – on WhatsApp`
            : "On WhatsApp");
        setSaveOpen(true);
    };

    const save = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        try {
            const response = await api.post(`/leadtools/number-checks/${selected.id}/save/`, {
                result: "ON_WHATSAPP",
                group_name: saveName,
            });
            toast.success(`Saving ${selected.on_whatsapp_count.toLocaleString()} numbers into "${response.data.contact_group.name}".`);
            setSaveOpen(false);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to save these numbers."));
        } finally {
            setSaving(false);
        }
    };

    const entryColumns = [
        { key: "phone", header: "Phone", render: (row) => <span className="tabular-nums text-ink-900">{row.phone}</span> },
        {
            key: "name",
            header: "Contact",
            render: (row) =>
                row.contact ? (
                    <Link to={`/contacts/${row.contact}`} className="text-ink-700 no-underline hover:text-brand-700">
                        {row.contact_name || "Unnamed contact"}
                    </Link>
                ) : (
                    <span className="text-ink-400">Not a contact yet</span>
                ),
        },
        {
            key: "result",
            header: "Result",
            render: (row) => <Badge tone={RESULT_TONE[row.result]}>{RESULT_LABEL[row.result]}</Badge>,
        },
        {
            key: "checked_at",
            header: "Checked",
            hideBelow: "md",
            render: (row) => <span className="text-ink-500">{when(row.checked_at) || "—"}</span>,
        },
    ];

    const checkColumns = [
        { key: "created_at", header: "When", render: (row) => <span className="text-ink-700">{when(row.created_at)}</span> },
        { key: "account", header: "Number", hideBelow: "md", render: (row) => row.whatsapp_account_name },
        {
            key: "source",
            header: "List",
            hideBelow: "lg",
            render: (row) => (row.source === "CONTACTS" ? row.contact_group_name || "All contacts" : "Pasted"),
        },
        { key: "total", header: "Checked", align: "right", render: (row) => <span className="tabular-nums">{row.checked.toLocaleString()} / {row.total.toLocaleString()}</span> },
        { key: "on", header: "On WhatsApp", align: "right", render: (row) => <span className="tabular-nums text-brand-700 font-semibold">{row.on_whatsapp_count.toLocaleString()}</span> },
        { key: "off", header: "Not on WhatsApp", align: "right", hideBelow: "sm", render: (row) => <span className="tabular-nums text-ink-600">{row.not_on_whatsapp_count.toLocaleString()}</span> },
        { key: "status", header: "Status", render: (row) => <Badge tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</Badge> },
    ];

    const tabs = selected
        ? [
              { id: "ON_WHATSAPP", label: "On WhatsApp", icon: CheckCircle2, count: selected.on_whatsapp_count },
              { id: "NOT_ON_WHATSAPP", label: "Not on WhatsApp", icon: XCircle, count: selected.not_on_whatsapp_count },
              ...(selected.error_count ? [{ id: "ERROR", label: "Could not check", count: selected.error_count }] : []),
          ]
        : [];

    return (
        <div>
            <PageHeader
                title="Number filter"
                description="Find out which numbers on a list are actually on WhatsApp before you send to them. Asked through one of your linked numbers, a few at a time, so the number stays healthy."
            />
            <FeatureGate
                feature="number_filter"
                title="Number filter"
                description="Check which numbers on a list are on WhatsApp before you send to them, and save the reachable ones into a group."
            >
                <Alert>{error}</Alert>

                {loading ? (
                    <TableSkeleton rows={4} columns={4} />
                ) : accounts.length === 0 ? (
                    <EmptyState
                        icon={Smartphone}
                        title="Connect a number by QR first"
                        description="The number filter asks WhatsApp through a linked phone. Cloud API numbers have no way to look a number up."
                        action={
                            <Link to="/whatsapp" className="no-underline">
                                <Button icon={Smartphone}>Go to Devices</Button>
                            </Link>
                        }
                    />
                ) : (
                    <>
                        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] mb-6">
                            <Card>
                                <CardHeader
                                    title="New check"
                                    description="Paste a list, or check the contacts you already have."
                                    className="mb-4"
                                />
                                <form onSubmit={start} className="space-y-4">
                                    <Field label="Check with" required>
                                        <Select
                                            value={form.account}
                                            onChange={(event) => setForm({ ...form, account: event.target.value })}
                                            required
                                        >
                                            {accounts.map((account) => (
                                                <option key={account.id} value={account.id}>
                                                    {account.name}
                                                    {account.phone_number ? ` (${account.phone_number})` : ""}
                                                </option>
                                            ))}
                                        </Select>
                                    </Field>
                                    <Field label="What to check">
                                        <Select
                                            value={form.source}
                                            onChange={(event) => setForm({ ...form, source: event.target.value })}
                                        >
                                            <option value="PASTE">A list I paste in</option>
                                            <option value="CONTACTS">My contacts</option>
                                        </Select>
                                    </Field>
                                    {form.source === "PASTE" ? (
                                        <>
                                            <Field
                                                label="Numbers"
                                                required
                                                hint="One per line. A name before the number is fine; it is ignored here."
                                            >
                                                <Textarea
                                                    rows={7}
                                                    value={form.text}
                                                    onChange={(event) => setForm({ ...form, text: event.target.value })}
                                                    placeholder={SAMPLE}
                                                    required
                                                    className="font-mono text-sm"
                                                />
                                            </Field>
                                            <Field
                                                label="Country code for numbers without one"
                                                hint="Digits only. Left blank, a number without a country code is skipped rather than guessed."
                                            >
                                                <Input
                                                    value={form.countryCode}
                                                    onChange={(event) => setForm({ ...form, countryCode: event.target.value.replace(/\D/g, "") })}
                                                    maxLength={4}
                                                    className="w-28"
                                                    placeholder="91"
                                                />
                                            </Field>
                                        </>
                                    ) : (
                                        <Field label="Which contacts">
                                            <Select
                                                value={form.group}
                                                onChange={(event) => setForm({ ...form, group: event.target.value })}
                                            >
                                                <option value="">All contacts</option>
                                                {groups.map((group) => (
                                                    <option key={group.id} value={group.id}>
                                                        {group.name}
                                                    </option>
                                                ))}
                                            </Select>
                                        </Field>
                                    )}
                                    <Button type="submit" icon={ScanSearch} loading={starting} disabled={!form.account}>
                                        Check numbers
                                    </Button>
                                </form>
                            </Card>

                            <Card>
                                {selected ? (
                                    <>
                                        <div className="flex items-start justify-between gap-3 flex-wrap">
                                            <div className="min-w-0">
                                                <p className="text-xs font-semibold uppercase tracking-wide text-ink-500 m-0">
                                                    Check #{selected.id}
                                                </p>
                                                <p className="text-sm text-ink-600 m-0 mt-1">
                                                    {SOURCE_LABEL[selected.source]}
                                                    {selected.source === "CONTACTS" && (
                                                        <> · {selected.contact_group_name || "All contacts"}</>
                                                    )}{" "}
                                                    · via {selected.whatsapp_account_name} · {when(selected.created_at)}
                                                </p>
                                            </div>
                                            <Badge tone={STATUS_TONE[selected.status]}>{STATUS_LABEL[selected.status]}</Badge>
                                        </div>

                                        {LIVE.has(selected.status) && (
                                            <div className="mt-4">
                                                <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
                                                    <div
                                                        className="h-full bg-brand-600 transition-all duration-500"
                                                        style={{ width: `${selected.progress}%` }}
                                                    />
                                                </div>
                                                <p className="text-xs text-ink-500 mt-1.5 mb-0">
                                                    {selected.checked.toLocaleString()} of {selected.total.toLocaleString()} checked.
                                                    Numbers are asked in small batches so the sender is not flagged.
                                                </p>
                                            </div>
                                        )}
                                        {selected.status === "FAILED" && (
                                            <Alert className="mt-4 mb-0" title="The check stopped early">
                                                {selected.error} Everything answered before that is kept below; start a new
                                                check for the rest.
                                            </Alert>
                                        )}

                                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
                                            <Stat label="Checked" value={selected.checked} />
                                            <Stat label="On WhatsApp" value={selected.on_whatsapp_count} tone="text-brand-700" />
                                            <Stat label="Not on WhatsApp" value={selected.not_on_whatsapp_count} tone="text-red-600" />
                                            <Stat label="Could not check" value={selected.error_count} tone={selected.error_count ? "text-amber-600" : "text-ink-400"} />
                                        </div>

                                        <div className="flex flex-wrap gap-2 mt-4">
                                            <Button
                                                size="sm"
                                                icon={FolderPlus}
                                                disabled={!selected.on_whatsapp_count || LIVE.has(selected.status)}
                                                onClick={openSave}
                                            >
                                                Save reachable numbers to a group
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="secondary"
                                                icon={Download}
                                                disabled={!selected.on_whatsapp_count}
                                                onClick={() => exportResult("ON_WHATSAPP")}
                                            >
                                                Export on WhatsApp
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="secondary"
                                                icon={Download}
                                                disabled={!selected.not_on_whatsapp_count}
                                                onClick={() => exportResult("NOT_ON_WHATSAPP")}
                                            >
                                                Export not on WhatsApp
                                            </Button>
                                        </div>
                                    </>
                                ) : (
                                    <EmptyState
                                        variant="plain"
                                        icon={ScanSearch}
                                        title="No checks yet"
                                        description="Run one and the answer appears here, number by number."
                                    />
                                )}
                            </Card>
                        </div>

                        {selected && (
                            <Card className="mb-6">
                                <Tabs tabs={tabs} active={tab} onChange={setTab} />
                                {entriesLoading && entries.results.length === 0 ? (
                                    <TableSkeleton rows={4} columns={4} />
                                ) : (
                                    <DataTable
                                        columns={entryColumns}
                                        rows={entries.results}
                                        empty={
                                            <EmptyState
                                                variant="plain"
                                                title={LIVE.has(selected.status) ? "Still checking" : "Nothing here"}
                                                description={
                                                    LIVE.has(selected.status)
                                                        ? "Results fill in as WhatsApp answers."
                                                        : `No numbers came back as “${RESULT_LABEL[tab]}”.`
                                                }
                                            />
                                        }
                                        footer={
                                            entries.count > entries.results.length ? (
                                                <div className="flex items-center justify-between gap-3 text-sm text-ink-500">
                                                    <span>
                                                        Page {entries.page} · {entries.count.toLocaleString()} numbers
                                                    </span>
                                                    <div className="flex gap-2">
                                                        <Button
                                                            size="xs"
                                                            variant="secondary"
                                                            disabled={!entries.previous || entriesLoading}
                                                            onClick={() => loadEntries(selected.id, tab, entries.page - 1)}
                                                        >
                                                            Previous
                                                        </Button>
                                                        <Button
                                                            size="xs"
                                                            variant="secondary"
                                                            disabled={!entries.next || entriesLoading}
                                                            onClick={() => loadEntries(selected.id, tab, entries.page + 1)}
                                                        >
                                                            Next
                                                        </Button>
                                                    </div>
                                                </div>
                                            ) : null
                                        }
                                    />
                                )}
                            </Card>
                        )}

                        <Card>
                            <CardHeader
                                title="Recent checks"
                                description="Pick one to see its numbers."
                                className="mb-4"
                            />
                            <DataTable
                                columns={checkColumns}
                                rows={checks}
                                onRowClick={(row) => {
                                    setSelected(row);
                                    setTab("ON_WHATSAPP");
                                }}
                                empty={
                                    <EmptyState
                                        variant="plain"
                                        title="No checks yet"
                                        description="Your first check will be listed here."
                                    />
                                }
                            />
                        </Card>
                    </>
                )}

                {saveOpen && selected && (
                    <Modal
                        title="Save reachable numbers"
                        description={`Put the ${selected.on_whatsapp_count.toLocaleString()} numbers that are on WhatsApp into a contact group. Numbers already on your account are added to the group; new ones become contacts.`}
                        onClose={() => setSaveOpen(false)}
                    >
                        <form onSubmit={save}>
                            <Field label="Contact group" required hint="An existing group of this name is reused.">
                                <Input
                                    value={saveName}
                                    onChange={(event) => setSaveName(event.target.value)}
                                    maxLength={100}
                                    required
                                    autoFocus
                                />
                            </Field>
                            <Alert tone="info" className="mt-4">
                                Being on WhatsApp is not consent. These contacts are saved with no consent recorded, so a
                                campaign that requires consent will skip them until you record where it came from.
                            </Alert>
                            <div className="flex justify-end gap-2 mt-2">
                                <Button type="button" variant="secondary" onClick={() => setSaveOpen(false)}>
                                    Cancel
                                </Button>
                                <Button type="submit" icon={FolderPlus} loading={saving}>
                                    Save to group
                                </Button>
                            </div>
                        </form>
                    </Modal>
                )}
            </FeatureGate>
        </div>
    );
}
