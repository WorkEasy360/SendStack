import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { BellOff, BellRing, Contact as ContactIcon, Download, Plus, Trash2, Upload } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { runBulk, describeBulkFailures } from "../../services/bulk";
import useSelection from "../../hooks/useSelection";
import { downloadFile } from "../../utils/download";
import Button from "../../components/ui/Button";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Modal from "../../components/ui/Modal";
import EmptyState from "../../components/ui/EmptyState";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import PageHeader from "../../components/ui/PageHeader";
import SearchInput from "../../components/ui/SearchInput";
import DataTable, { RowAction } from "../../components/ui/DataTable";
import BulkActionBar from "../../components/ui/BulkActionBar";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { ChipToggle, Field, FileInput, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";
import {
    LIFECYCLE_OPTIONS,
    LifecycleBadge,
    ScoreBadge,
    timeAgo,
} from "../../components/crm/CrmBits";

const EMPTY_CONTACT = { name: "", phone: "", email: "", company: "", country: "", groups: [], tags: [] };

export default function Contacts() {
    const toast = useToast();
    const [searchParams, setSearchParams] = useSearchParams();
    const [contacts, setContacts] = useState([]);
    const [groups, setGroups] = useState([]);
    const [tags, setTags] = useState([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState("");
    const [subscribedFilter, setSubscribedFilter] = useState("");
    const [groupFilter, setGroupFilter] = useState("");
    const [smartSegment, setSmartSegment] = useState("");
    const [ownerFilter, setOwnerFilter] = useState("");
    const [segments, setSegments] = useState([]);
    const [team, setTeam] = useState([]);
    const [understood, setUnderstood] = useState([]);
    const [bulkOwner, setBulkOwner] = useState("");
    const [bulkStatus, setBulkStatus] = useState("");
    const [error, setError] = useState("");

    const [showContactModal, setShowContactModal] = useState(false);
    const [editing, setEditing] = useState(null);
    const [form, setForm] = useState(EMPTY_CONTACT);
    const [formError, setFormError] = useState("");
    const [saving, setSaving] = useState(false);

    const [pendingDelete, setPendingDelete] = useState(null);
    const [pendingBulk, setPendingBulk] = useState(null);
    const [bulkBusy, setBulkBusy] = useState(false);
    const [bulkGroup, setBulkGroup] = useState("");
    const [bulkTag, setBulkTag] = useState("");
    const selection = useSelection(contacts);

    const [showImport, setShowImport] = useState(false);
    const [importFile, setImportFile] = useState(null);
    const [importPreview, setImportPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);
    const [importing, setImporting] = useState(false);
    const [importError, setImportError] = useState("");
    const [exporting, setExporting] = useState(false);

    // Search and filters are applied server-side; the backend exposes
    // `search`, `subscribed` and `groups` on the contact list endpoint, plus
    // the three CRM ones: `q` (the natural-language box), `smart` (a
    // behavioural segment) and `owner`.
    //
    // `q` and `search` are deliberately different parameters. `search` is the
    // plain substring match this page always had; `q` is parsed for phrases
    // like "hot leads who haven't replied in 3 days" and reports back what it
    // understood. Sending the sentence as `search` would just find nobody.
    const buildParams = (overrides = {}) => {
        const {
            searchTerm = search,
            subscribed = subscribedFilter,
            group = groupFilter,
            smart = smartSegment,
            owner = ownerFilter,
        } = overrides;
        const params = {};
        if (searchTerm) params.q = searchTerm;
        if (subscribed !== "") params.subscribed = subscribed;
        if (group) params.groups = group;
        if (smart) params.smart = smart;
        if (owner) params.owner = owner;
        return params;
    };

    const loadAll = async (overrides = {}) => {
        setLoading(true);
        try {
            const [contactsRes, groupsRes, tagsRes] = await Promise.all([
                api.get("/contacts/contacts/", { params: buildParams(overrides) }),
                api.get("/contacts/groups/"),
                api.get("/contacts/tags/"),
            ]);
            setContacts(contactsRes.data.results || []);
            setUnderstood(contactsRes.data.understood || []);
            setGroups(groupsRes.data.results || []);
            setTags(tagsRes.data.results || []);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to load contacts."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadAll();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // The smart-segment chips and the owner picker. Both are the same for
    // every load, so they are fetched once and a failure costs a control
    // rather than the page.
    useEffect(() => {
        Promise.all([
            api.get("/crm/segments/").catch(() => ({ data: { segments: [] } })),
            api.get("/crm/team/").catch(() => ({ data: { results: [] } })),
        ]).then(([segmentsRes, teamRes]) => {
            setSegments(segmentsRes.data.segments || []);
            setTeam(teamRes.data.results || []);
        });
    }, []);

    // A smart segment arriving in the URL (?smart=hot_leads) is how the
    // dashboard's Today tiles and the automation recommendations hand off to
    // this page: notice the problem, land on exactly the list it describes.
    useEffect(() => {
        const requested = searchParams.get("smart");
        if (requested && requested !== smartSegment) {
            setSmartSegment(requested);
            loadAll({ smart: requested });
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [searchParams]);

    const applySmart = (key) => {
        const next = key === smartSegment ? "" : key;
        setSmartSegment(next);
        setSearchParams(next ? { smart: next } : {}, { replace: true });
        loadAll({ smart: next });
    };

    const handleSearchSubmit = (event) => {
        event.preventDefault();
        loadAll();
    };

    const applyFilter = (key, value) => {
        if (key === "subscribed") setSubscribedFilter(value);
        if (key === "group") setGroupFilter(value);
        if (key === "owner") setOwnerFilter(value);
        loadAll({ [key]: value });
    };

    const openCreate = () => {
        setEditing(null);
        setForm(EMPTY_CONTACT);
        setFormError("");
        setShowContactModal(true);
    };

    const openEdit = (contact) => {
        setEditing(contact);
        setForm({
            name: contact.name || "",
            phone: contact.phone,
            email: contact.email || "",
            company: contact.company || "",
            country: contact.country || "",
            groups: contact.groups || [],
            tags: contact.tags || [],
        });
        setFormError("");
        setShowContactModal(true);
    };

    const closeModal = () => setShowContactModal(false);

    const saveContact = async (event) => {
        event.preventDefault();
        setFormError("");
        setSaving(true);
        try {
            if (editing) {
                await api.patch(`/contacts/contacts/${editing.id}/`, form);
            } else {
                await api.post("/contacts/contacts/", form);
            }
            closeModal();
            toast.success(editing ? "Contact updated." : "Contact added.");
            await loadAll();
        } catch (err) {
            setFormError(apiErrorMessage(err, "Unable to save this contact."));
        } finally {
            setSaving(false);
        }
    };

    const toggleMulti = (field, id) => {
        setForm((prev) => {
            const list = prev[field].includes(id) ? prev[field].filter((x) => x !== id) : [...prev[field], id];
            return { ...prev, [field]: list };
        });
    };

    const unsubscribe = async (contact) => {
        const action = contact.subscribed ? "unsubscribe" : "resubscribe";
        try {
            await api.post(`/contacts/contacts/${contact.id}/${action}/`);
            toast.success(contact.subscribed ? "Contact unsubscribed." : "Contact resubscribed.");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to update this contact."));
        }
        await loadAll();
    };

    const removeContact = async (contact) => {
        try {
            await api.delete(`/contacts/contacts/${contact.id}/`);
            toast.success(`${contact.name || contact.phone} deleted.`);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to delete this contact."));
        } finally {
            setPendingDelete(null);
            await loadAll();
        }
    };

    // Every bulk operation goes through the one /contacts/contacts/bulk/
    // endpoint, which already backed the tag/group/subscription actions --
    // `extra` carries the tag_id / group_id those two need.
    const runBulkAction = async (action, extra = {}) => {
        setBulkBusy(true);
        setError("");
        const result = await runBulk("/contacts/contacts/bulk/", { action, ids: selection.selectedIds, ...extra });
        if (result.succeeded > 0) toast.success(result.message);
        if (result.failed > 0 || !result.ok) {
            setError(describeBulkFailures(result, (id) => {
                const contact = contacts.find((entry) => entry.id === id);
                return contact ? contact.name || contact.phone : null;
            }));
        }
        selection.clear();
        setPendingBulk(null);
        setBulkBusy(false);
        await loadAll();
    };

    // Exports exactly the ticked rows rather than the whole filtered list.
    const exportSelected = async () => {
        setBulkBusy(true);
        setError("");
        try {
            await downloadFile(
                `/contacts/contacts/export/?ids=${selection.selectedIds.join(",")}`,
                "contacts-selected.csv"
            );
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to export the selected contacts."));
        } finally {
            setBulkBusy(false);
        }
    };

    // Picking a file reads it and says what is in it, rather than presenting a
    // column-mapping screen. The headers are matched server-side (see
    // apps/contacts/mapping.py), so the only decision left is whether to go
    // ahead — which is the only decision a user ever actually wanted to make.
    const previewImport = async (file) => {
        setImportFile(file);
        setImportPreview(null);
        setImportError("");
        if (!file) return;
        setPreviewing(true);
        try {
            const data = new FormData();
            data.append("file", file);
            const response = await api.post("/contacts/imports/preview/", data, {
                headers: { "Content-Type": "multipart/form-data" },
            });
            setImportPreview(response.data);
        } catch (err) {
            setImportError(apiErrorMessage(err, "Could not read that file."));
        } finally {
            setPreviewing(false);
        }
    };

    const closeImport = () => {
        setShowImport(false);
        setImportFile(null);
        setImportPreview(null);
        setImportError("");
    };

    const runImport = async (event) => {
        event.preventDefault();
        if (!importFile) return;
        setImporting(true);
        setImportError("");
        try {
            const data = new FormData();
            data.append("file", importFile);
            await api.post("/contacts/imports/", data, { headers: { "Content-Type": "multipart/form-data" } });
            closeImport();
            toast.success("Import started. Contacts appear as the file is processed.");
            await loadAll();
        } catch (err) {
            setImportError(apiErrorMessage(err, "Unable to import this file."));
        } finally {
            setImporting(false);
        }
    };

    // Exports whatever the current search/filters select, matching the list
    // above rather than always dumping the full contact book.
    const exportContacts = async () => {
        setExporting(true);
        setError("");
        try {
            const query = new URLSearchParams(buildParams()).toString();
            await downloadFile(`/contacts/contacts/export/${query ? `?${query}` : ""}`, "contacts.csv");
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to export contacts."));
        } finally {
            setExporting(false);
        }
    };

    const hasFilters = Boolean(search || subscribedFilter !== "" || groupFilter || smartSegment || ownerFilter);

    const columns = [
        {
            key: "name",
            header: "Name",
            render: (contact) => (
                <div className="min-w-0">
                    {/* The name is the way in to the customer's own screen --
                        everything else on this page is a shortcut for people
                        who already know what they want to do. */}
                    <Link
                        to={`/contacts/${contact.id}`}
                        className="font-semibold text-ink-900 truncate no-underline hover:text-brand-700 block"
                    >
                        {contact.name || contact.phone}
                    </Link>
                    {contact.company && (
                        <p className="text-xs text-ink-500 truncate m-0">{contact.company}</p>
                    )}
                    <p className="text-xs text-ink-500 truncate m-0 sm:hidden tabular-nums">{contact.phone}</p>
                </div>
            ),
        },
        {
            key: "phone",
            header: "Phone",
            hideBelow: "sm",
            render: (contact) => <span className="tabular-nums text-ink-600">{contact.phone}</span>,
        },
        {
            key: "lead",
            header: "Lead",
            render: (contact) => (
                <div className="flex items-center gap-1.5 flex-wrap">
                    <ScoreBadge band={contact.score_band} />
                    <LifecycleBadge status={contact.lifecycle_status} />
                </div>
            ),
        },
        {
            key: "last_inbound_at",
            header: "Last reply",
            hideBelow: "lg",
            render: (contact) => (
                <span className={contact.last_inbound_at ? "text-ink-600" : "text-ink-400"}>
                    {contact.last_inbound_at ? timeAgo(contact.last_inbound_at) : "Never"}
                </span>
            ),
        },
        {
            key: "owner",
            header: "Owner",
            hideBelow: "lg",
            render: (contact) => (
                <span className={contact.owner_username ? "text-ink-600" : "text-ink-400"}>
                    {contact.owner_username || "Unassigned"}
                </span>
            ),
        },
        {
            key: "tags",
            header: "Tags",
            hideBelow: "xl",
            render: (contact) => (
                <div className="flex gap-1 flex-wrap">
                    {contact.tags?.length ? (
                        contact.tags.map((tagId) => {
                            const tag = tags.find((t) => t.id === tagId);
                            return tag ? (
                                <Badge key={tagId} tone="blue">
                                    {tag.name}
                                </Badge>
                            ) : null;
                        })
                    ) : (
                        <span className="text-ink-400">—</span>
                    )}
                </div>
            ),
        },
        {
            key: "subscribed",
            header: "Status",
            render: (contact) => (
                <Badge tone={contact.subscribed ? "green" : "gray"}>
                    {contact.subscribed ? "Subscribed" : "Unsubscribed"}
                </Badge>
            ),
        },
        {
            key: "actions",
            header: "",
            align: "right",
            render: (contact) => (
                <div className="flex justify-end gap-3 whitespace-nowrap">
                    <RowAction tone="brand" onClick={() => openEdit(contact)}>
                        Edit
                    </RowAction>
                    <RowAction onClick={() => unsubscribe(contact)}>
                        {contact.subscribed ? "Unsubscribe" : "Resubscribe"}
                    </RowAction>
                    <RowAction tone="danger" onClick={() => setPendingDelete(contact)}>
                        Delete
                    </RowAction>
                </div>
            ),
        },
    ];

    return (
        <div>
            <PageHeader
                title="Contacts"
                description={loading ? "Manage your audience." : `${contacts.length} contact${contacts.length === 1 ? "" : "s"} shown`}
                actions={
                    <>
                        <Button variant="secondary" icon={Download} onClick={exportContacts} loading={exporting}>
                            Export
                        </Button>
                        <Button variant="secondary" icon={Upload} onClick={() => setShowImport(true)}>
                            Import CSV
                        </Button>
                        <Button icon={Plus} onClick={openCreate}>
                            Add Contact
                        </Button>
                    </>
                }
            />

            <Alert>{error}</Alert>

            {/* The behavioural lists everybody wants and nobody should have to
                build a filter for. Only segments with somebody in them are
                offered — an empty chip is a dead end. */}
            {segments.some((segment) => segment.count > 0) && (
                <div className="scroll-x flex gap-2 mb-4 pb-1">
                    {segments
                        .filter((segment) => segment.count > 0 || segment.key === smartSegment)
                        .map((segment) => (
                            <button
                                key={segment.key}
                                type="button"
                                title={segment.description}
                                onClick={() => applySmart(segment.key)}
                                className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-semibold whitespace-nowrap border cursor-pointer transition-colors ${
                                    smartSegment === segment.key
                                        ? "bg-ink-900 text-white border-ink-900"
                                        : "bg-white text-ink-600 border-ink-200 hover:border-ink-400 hover:text-ink-900"
                                }`}
                            >
                                {segment.label}
                                <span
                                    className={`tabular-nums text-xs ${
                                        smartSegment === segment.key ? "text-white/70" : "text-ink-400"
                                    }`}
                                >
                                    {segment.count}
                                </span>
                            </button>
                        ))}
                </div>
            )}

            <form onSubmit={handleSearchSubmit} className="flex flex-col sm:flex-row gap-2 mb-2">
                <SearchInput
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    onClear={() => {
                        setSearch("");
                        setUnderstood([]);
                        loadAll({ searchTerm: "" });
                    }}
                    placeholder="Try: hot leads who haven't replied in 3 days"
                    className="sm:max-w-sm flex-1"
                />
                <Select
                    value={ownerFilter}
                    onChange={(event) => applyFilter("owner", event.target.value)}
                    aria-label="Filter by owner"
                    className="sm:w-40"
                >
                    <option value="">Any owner</option>
                    {team.map((member) => (
                        <option key={member.id} value={member.id}>
                            {member.username}
                        </option>
                    ))}
                </Select>
                <Select
                    value={subscribedFilter}
                    onChange={(event) => applyFilter("subscribed", event.target.value)}
                    aria-label="Filter by subscription status"
                    className="sm:w-44"
                >
                    <option value="">All statuses</option>
                    <option value="true">Subscribed</option>
                    <option value="false">Unsubscribed</option>
                </Select>
                <Select
                    value={groupFilter}
                    onChange={(event) => applyFilter("group", event.target.value)}
                    aria-label="Filter by group"
                    className="sm:w-44"
                >
                    <option value="">All groups</option>
                    {groups.map((group) => (
                        <option key={group.id} value={group.id}>
                            {group.name}
                        </option>
                    ))}
                </Select>
                <Button type="submit" variant="secondary">
                    Search
                </Button>
            </form>

            {/* What the search box actually understood. Showing its working is
                what makes a sentence box trustworthy: a filter applied
                silently is indistinguishable from a bug. */}
            {understood.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap mb-4 text-sm">
                    <span className="text-ink-500">Showing:</span>
                    {understood.map((label) => (
                        <Badge key={label} tone="violet">
                            {label}
                        </Badge>
                    ))}
                </div>
            )}
            {understood.length === 0 && <div className="mb-4" />}

            <BulkActionBar count={selection.selectedCount} label="contact" onClear={selection.clear}>
                <Button size="sm" variant="secondary" icon={Download} disabled={bulkBusy} onClick={exportSelected}>
                    Export
                </Button>
                <Button
                    size="sm"
                    variant="secondary"
                    icon={BellOff}
                    disabled={bulkBusy}
                    onClick={() => runBulkAction("unsubscribe")}
                >
                    Unsubscribe
                </Button>
                <Button
                    size="sm"
                    variant="secondary"
                    icon={BellRing}
                    disabled={bulkBusy}
                    onClick={() => runBulkAction("resubscribe")}
                >
                    Resubscribe
                </Button>
                {team.length > 0 && (
                    <Select
                        value={bulkOwner}
                        disabled={bulkBusy}
                        aria-label="Assign the selected contacts to somebody"
                        className="w-44"
                        onChange={(event) => {
                            const ownerId = event.target.value;
                            setBulkOwner("");
                            if (ownerId) {
                                runBulkAction("assign_owner", {
                                    owner_id: ownerId === "none" ? null : Number(ownerId),
                                });
                            }
                        }}
                    >
                        <option value="">Assign owner...</option>
                        <option value="none">Nobody</option>
                        {team.map((member) => (
                            <option key={member.id} value={member.id}>
                                {member.username}
                            </option>
                        ))}
                    </Select>
                )}
                <Select
                    value={bulkStatus}
                    disabled={bulkBusy}
                    aria-label="Set the status of the selected contacts"
                    className="w-40"
                    onChange={(event) => {
                        const status = event.target.value;
                        setBulkStatus("");
                        if (status) runBulkAction("set_status", { lifecycle_status: status });
                    }}
                >
                    <option value="">Set status...</option>
                    {LIFECYCLE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                            {option.label}
                        </option>
                    ))}
                </Select>
                {groups.length > 0 && (
                    <Select
                        value={bulkGroup}
                        disabled={bulkBusy}
                        aria-label="Add the selected contacts to a group"
                        className="w-44"
                        onChange={(event) => {
                            const groupId = event.target.value;
                            setBulkGroup("");
                            if (groupId) runBulkAction("add_group", { group_id: Number(groupId) });
                        }}
                    >
                        <option value="">Add to group...</option>
                        {groups.map((group) => (
                            <option key={group.id} value={group.id}>
                                {group.name}
                            </option>
                        ))}
                    </Select>
                )}
                {tags.length > 0 && (
                    <Select
                        value={bulkTag}
                        disabled={bulkBusy}
                        aria-label="Tag the selected contacts"
                        className="w-40"
                        onChange={(event) => {
                            const tagId = event.target.value;
                            setBulkTag("");
                            if (tagId) runBulkAction("add_tag", { tag_id: Number(tagId) });
                        }}
                    >
                        <option value="">Add tag...</option>
                        {tags.map((tag) => (
                            <option key={tag.id} value={tag.id}>
                                {tag.name}
                            </option>
                        ))}
                    </Select>
                )}
                <Button
                    size="sm"
                    variant="danger"
                    icon={Trash2}
                    disabled={bulkBusy}
                    onClick={() => setPendingBulk("delete")}
                >
                    Delete
                </Button>
            </BulkActionBar>

            {loading ? (
                <TableSkeleton rows={6} columns={5} />
            ) : (
                <DataTable
                    columns={columns}
                    rows={contacts}
                    selection={selection}
                    empty={
                        hasFilters ? (
                            <EmptyState
                                icon={ContactIcon}
                                title="No matching contacts"
                                description="No contact matches your current search and filters."
                                action={
                                    <Button
                                        variant="secondary"
                                        onClick={() => {
                                            setSearch("");
                                            setSubscribedFilter("");
                                            setGroupFilter("");
                                            loadAll({ searchTerm: "", subscribed: "", group: "" });
                                        }}
                                    >
                                        Clear filters
                                    </Button>
                                }
                            />
                        ) : (
                            <EmptyState
                                icon={ContactIcon}
                                title="No contacts yet"
                                description="Add a contact manually or import a CSV file to get started."
                                action={
                                    <div className="flex flex-wrap gap-2 justify-center">
                                        <Button icon={Plus} onClick={openCreate}>
                                            Add Contact
                                        </Button>
                                        <Button variant="secondary" icon={Upload} onClick={() => setShowImport(true)}>
                                            Import CSV
                                        </Button>
                                    </div>
                                }
                            />
                        )
                    }
                />
            )}

            {showContactModal && (
                <Modal title={editing ? "Edit Contact" : "Add Contact"} onClose={closeModal}>
                    <form onSubmit={saveContact} id="contact-form">
                        <Alert>{formError}</Alert>
                        <Field label="Name">
                            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus />
                        </Field>
                        <Field label="Phone" hint="E.164 format, e.g. +15551234567" required>
                            <Input value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} required />
                        </Field>
                        <Field label="Email">
                            <Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                        </Field>
                        <Field label="Company">
                            <Input
                                value={form.company}
                                onChange={(e) => setForm({ ...form, company: e.target.value })}
                                maxLength={150}
                            />
                        </Field>
                        <Field label="Country" hint="Two-letter code, e.g. IN, US">
                            <Input
                                value={form.country}
                                onChange={(e) => setForm({ ...form, country: e.target.value.toUpperCase() })}
                                maxLength={2}
                            />
                        </Field>
                        {groups.length > 0 && (
                            <Field label="Groups">
                                <div className="flex flex-wrap gap-2">
                                    {groups.map((group) => (
                                        <ChipToggle
                                            key={group.id}
                                            checked={form.groups.includes(group.id)}
                                            onChange={() => toggleMulti("groups", group.id)}
                                        >
                                            {group.name}
                                        </ChipToggle>
                                    ))}
                                </div>
                            </Field>
                        )}
                        {tags.length > 0 && (
                            <Field label="Tags">
                                <div className="flex flex-wrap gap-2">
                                    {tags.map((tag) => (
                                        <ChipToggle
                                            key={tag.id}
                                            checked={form.tags.includes(tag.id)}
                                            onChange={() => toggleMulti("tags", tag.id)}
                                        >
                                            {tag.name}
                                        </ChipToggle>
                                    ))}
                                </div>
                            </Field>
                        )}
                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={closeModal}>
                                Cancel
                            </Button>
                            <Button type="submit" loading={saving}>
                                {editing ? "Save Changes" : "Add Contact"}
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}

            {pendingBulk === "delete" && (
                <ConfirmDialog
                    title={`Delete ${selection.selectedCount} contact${selection.selectedCount === 1 ? "" : "s"}`}
                    message={`Delete the ${selection.selectedCount} selected contact${
                        selection.selectedCount === 1 ? "" : "s"
                    }? Their inbox conversations and message history go with them. Campaign sends stay in your reports. This cannot be undone.`}
                    confirmLabel={`Delete ${selection.selectedCount}`}
                    onConfirm={() => runBulkAction("delete")}
                    onClose={() => setPendingBulk(null)}
                />
            )}

            {pendingDelete && (
                <ConfirmDialog
                    title="Delete contact"
                    message={`Delete ${pendingDelete.name || pendingDelete.phone}? Their inbox conversations and message history go with them. Campaign sends stay in your reports.`}
                    confirmLabel="Delete contact"
                    onConfirm={() => removeContact(pendingDelete)}
                    onClose={() => setPendingDelete(null)}
                />
            )}

            {showImport && (
                <Modal
                    title="Import contacts"
                    description="Pick a CSV. We work out which column is which — you just check it looks right."
                    onClose={closeImport}
                    width="lg"
                >
                    <form onSubmit={runImport}>
                        <Alert>{importError}</Alert>
                        <Field
                            label="CSV file"
                            hint="Any column names. Whatever we cannot place is kept as a custom field."
                            required
                        >
                            <FileInput accept=".csv" onChange={(e) => previewImport(e.target.files[0])} required />
                        </Field>

                        {previewing && (
                            <p className="text-sm text-ink-500 my-4">Reading the file…</p>
                        )}

                        {importPreview && (
                            <div className="my-4 space-y-4">
                                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                                    {[
                                        { label: "Found", value: importPreview.valid, tone: "text-ink-900" },
                                        { label: "New", value: importPreview.new, tone: "text-brand-700" },
                                        { label: "Already here", value: importPreview.existing, tone: "text-ink-600" },
                                        {
                                            label: "Unusable",
                                            value: importPreview.invalid + importPreview.duplicates_in_file,
                                            tone: importPreview.invalid ? "text-red-600" : "text-ink-400",
                                        },
                                    ].map((stat) => (
                                        <div key={stat.label} className="border border-ink-200 rounded-card p-3">
                                            <p className={`text-xl font-bold m-0 tabular-nums ${stat.tone}`}>
                                                {stat.value.toLocaleString()}
                                            </p>
                                            <p className="text-xs text-ink-500 mt-0.5 mb-0">{stat.label}</p>
                                        </div>
                                    ))}
                                </div>

                                {importPreview.truncated && (
                                    <p className="text-xs text-ink-500 m-0">
                                        These numbers cover the first {importPreview.rows_scanned.toLocaleString()}{" "}
                                        rows. The whole file is imported.
                                    </p>
                                )}

                                <div>
                                    <p className="text-xs font-bold uppercase tracking-wider text-ink-400 m-0 mb-2">
                                        Columns we recognised
                                    </p>
                                    <div className="flex flex-wrap gap-1.5">
                                        {Object.entries(importPreview.mapping).map(([field, header]) => (
                                            <Badge key={field} tone="green">
                                                {header} → {field}
                                            </Badge>
                                        ))}
                                        {importPreview.unmapped_headers?.map((header) => (
                                            <Badge key={header} tone="gray">
                                                {header} → custom field
                                            </Badge>
                                        ))}
                                    </div>
                                </div>

                                {importPreview.sample?.length > 0 && (
                                    <div>
                                        <p className="text-xs font-bold uppercase tracking-wider text-ink-400 m-0 mb-2">
                                            First few rows
                                        </p>
                                        <ul className="list-none p-0 m-0 space-y-1">
                                            {importPreview.sample.map((row) => (
                                                <li key={row.phone} className="text-sm text-ink-700">
                                                    <span className="tabular-nums">{row.phone}</span>
                                                    {row.name && <span className="text-ink-500"> · {row.name}</span>}
                                                    {row.company && (
                                                        <span className="text-ink-400"> · {row.company}</span>
                                                    )}
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                            </div>
                        )}

                        <div className="flex gap-2 justify-end">
                            <Button type="button" variant="secondary" onClick={closeImport}>
                                Cancel
                            </Button>
                            <Button
                                type="submit"
                                icon={Upload}
                                loading={importing}
                                disabled={!importFile || previewing}
                            >
                                {importPreview
                                    ? `Import ${importPreview.new.toLocaleString()} new contact${
                                          importPreview.new === 1 ? "" : "s"
                                      }`
                                    : "Import"}
                            </Button>
                        </div>
                    </form>
                </Modal>
            )}
        </div>
    );
}
