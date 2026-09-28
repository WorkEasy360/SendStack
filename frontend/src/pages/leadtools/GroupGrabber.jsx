import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Contact as ContactIcon, RefreshCw, Smartphone, UsersRound } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import useSelection from "../../hooks/useSelection";
import FeatureGate from "../../components/billing/FeatureLock";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import DataTable from "../../components/ui/DataTable";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import SearchInput from "../../components/ui/SearchInput";
import { TableSkeleton } from "../../components/ui/Skeleton";
import { Field, Input, Select } from "../../components/ui/Field";
import { useToast } from "../../components/ui/Toast";

/**
 * The group grabber: the members of WhatsApp groups a linked number is in,
 * pulled into a contact group.
 *
 * Two steps, both through the phone: list the groups, then read the members
 * of the ones ticked. The members go through the ordinary contact importer,
 * so the plan's contact ceiling, the never-overwrite-a-corrected-name rule
 * and consent all apply -- and consent is the part this screen has to be
 * loud about. Being in a group is not agreeing to be marketed to, and every
 * contact created here says so by having none recorded.
 */

const IMPORT_LIVE = new Set(["PENDING", "PROCESSING"]);
const POLL_MS = 2500;

function when(iso) {
    return iso ? new Date(iso).toLocaleString() : "";
}

export default function GroupGrabber() {
    const toast = useToast();
    const [accounts, setAccounts] = useState([]);
    const [account, setAccount] = useState("");
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const [groups, setGroups] = useState(null);
    const [loadingGroups, setLoadingGroups] = useState(false);
    const [search, setSearch] = useState("");
    const [name, setName] = useState("");
    const [grabbing, setGrabbing] = useState(false);

    const [grab, setGrab] = useState(null);
    const [grabs, setGrabs] = useState([]);

    const visibleGroups = useMemo(() => {
        const needle = search.trim().toLowerCase();
        if (!groups) return [];
        return needle ? groups.filter((group) => group.subject.toLowerCase().includes(needle)) : groups;
    }, [groups, search]);
    const selection = useSelection(visibleGroups, { getId: (group) => group.id });

    useEffect(() => {
        (async () => {
            try {
                const [accountsRes, grabsRes] = await Promise.all([
                    api.get("/whatsapp/accounts/", { params: { page_size: 100 } }),
                    api.get("/leadtools/group-grabs/"),
                ]);
                // Groups are a phone-app feature: only a QR-linked number can
                // see them, and only while it is connected.
                const usable = (accountsRes.data.results || []).filter(
                    (row) => row.connection_method === "QR" && row.status === "CONNECTED"
                );
                setAccounts(usable);
                setAccount(usable[0]?.id || "");
                setGrabs(grabsRes.data.results || []);
            } catch (err) {
                setError(apiErrorMessage(err, "Unable to load the group grabber."));
            } finally {
                setLoading(false);
            }
        })();
    }, []);

    // The import behind the latest grab is polled until it settles, so the
    // "12 new, 3 already here" line is the real outcome and not a promise.
    const grabId = grab?.id;
    const importStatus = grab?.contact_import?.status;
    useEffect(() => {
        if (!grabId || !IMPORT_LIVE.has(importStatus)) return undefined;
        const timer = setInterval(async () => {
            try {
                const response = await api.get(`/leadtools/group-grabs/${grabId}/`);
                setGrab(response.data);
                setGrabs((current) => current.map((row) => (row.id === response.data.id ? response.data : row)));
            } catch {
                // A missed poll is a late update, not a failure.
            }
        }, POLL_MS);
        return () => clearInterval(timer);
    }, [grabId, importStatus]);

    const loadGroups = async () => {
        setLoadingGroups(true);
        setError("");
        selection.clear();
        try {
            const response = await api.get("/leadtools/groups/", { params: { account } });
            setGroups(response.data.groups || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to read the groups on this number."));
            setGroups(null);
        } finally {
            setLoadingGroups(false);
        }
    };

    const placeholderName =
        selection.selectedCount === 1 ? selection.selectedRows[0].subject : "WhatsApp groups";

    const startGrab = async (event) => {
        event.preventDefault();
        setGrabbing(true);
        setError("");
        try {
            const response = await api.post("/leadtools/group-grabs/", {
                whatsapp_account: Number(account),
                group_ids: selection.selectedIds,
                contact_group_name: name.trim(),
            });
            setGrab(response.data);
            setGrabs((current) => [response.data, ...current]);
            selection.clear();
            setName("");
            toast.success(
                `Importing ${response.data.members_found.toLocaleString()} members into "${response.data.contact_group_name}".`
            );
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to read those groups."));
        } finally {
            setGrabbing(false);
        }
    };

    const groupColumns = [
        {
            key: "subject",
            header: "Group",
            render: (group) => (
                <div className="min-w-0">
                    <p className="font-semibold text-ink-900 truncate m-0">{group.subject || "Unnamed group"}</p>
                    {group.created_at && (
                        <p className="text-xs text-ink-500 m-0">Created {new Date(group.created_at).toLocaleDateString()}</p>
                    )}
                </div>
            ),
        },
        {
            key: "members",
            header: "Members",
            align: "right",
            render: (group) => <span className="tabular-nums text-ink-700">{group.member_count.toLocaleString()}</span>,
        },
        {
            key: "kind",
            header: "",
            hideBelow: "sm",
            render: (group) => (
                <div className="flex gap-1 flex-wrap">
                    {group.announce && <Badge tone="amber">Admins post only</Badge>}
                    {group.is_community && <Badge tone="violet">Community</Badge>}
                </div>
            ),
        },
    ];

    const grabColumns = [
        { key: "created_at", header: "When", render: (row) => <span className="text-ink-700">{when(row.created_at)}</span> },
        {
            key: "groups",
            header: "Groups",
            render: (row) => (
                <span className="text-ink-700">
                    {row.groups.map((group) => group.subject || "Unnamed").join(", ")}
                </span>
            ),
        },
        { key: "account", header: "Number", hideBelow: "lg", render: (row) => row.whatsapp_account_name },
        {
            key: "contact_group",
            header: "Saved into",
            hideBelow: "md",
            render: (row) => row.contact_group_name || <span className="text-ink-400">Deleted group</span>,
        },
        {
            key: "members",
            header: "Members",
            align: "right",
            render: (row) => (
                <span className="tabular-nums">
                    {row.members_found.toLocaleString()}
                    {row.members_hidden ? <span className="text-ink-400"> (+{row.members_hidden} hidden)</span> : null}
                </span>
            ),
        },
        {
            key: "status",
            header: "Import",
            render: (row) => {
                const status = row.contact_import?.status;
                if (!status) return <Badge tone="gray">Unknown</Badge>;
                if (IMPORT_LIVE.has(status)) return <Badge tone="blue">Importing</Badge>;
                if (status === "FAILED") return <Badge tone="red">Failed</Badge>;
                return (
                    <Badge tone="green">
                        {row.contact_import.created_count.toLocaleString()} new
                    </Badge>
                );
            },
        },
    ];

    return (
        <div>
            <PageHeader
                title="Group grabber"
                description="Pull the members of WhatsApp groups your linked number belongs to into a contact group, ready to segment and send to."
            />
            <FeatureGate
                feature="group_grabber"
                title="Group grabber"
                description="Import the members of WhatsApp groups a linked number is in as contacts."
            >
                <Alert>{error}</Alert>

                {loading ? (
                    <TableSkeleton rows={4} columns={3} />
                ) : accounts.length === 0 ? (
                    <EmptyState
                        icon={Smartphone}
                        title="Connect a number by QR first"
                        description="Groups live on the phone, so only a linked number can read them. Cloud API numbers cannot."
                        action={
                            <Link to="/whatsapp" className="no-underline">
                                <Button icon={Smartphone}>Go to Devices</Button>
                            </Link>
                        }
                    />
                ) : (
                    <>
                        <Card className="mb-6">
                            <CardHeader
                                title="Read groups"
                                description="Every group the number is a member of, with how many people are in each."
                                className="mb-4"
                            />
                            <div className="flex flex-col sm:flex-row sm:items-end gap-3">
                                <Field label="Number" className="sm:w-72 mb-0">
                                    <Select value={account} onChange={(event) => setAccount(event.target.value)}>
                                        {accounts.map((row) => (
                                            <option key={row.id} value={row.id}>
                                                {row.name}
                                                {row.phone_number ? ` (${row.phone_number})` : ""}
                                            </option>
                                        ))}
                                    </Select>
                                </Field>
                                <Button icon={RefreshCw} loading={loadingGroups} onClick={loadGroups} disabled={!account}>
                                    {groups ? "Reload groups" : "Load groups"}
                                </Button>
                            </div>
                        </Card>

                        {groups && (
                            <Card className="mb-6">
                                <form onSubmit={startGrab}>
                                    <div className="flex flex-col md:flex-row md:items-end gap-3 mb-4">
                                        <SearchInput
                                            value={search}
                                            onChange={setSearch}
                                            onClear={() => setSearch("")}
                                            placeholder="Find a group"
                                            className="md:max-w-xs flex-1"
                                        />
                                        <Field label="Save members into" className="md:w-72 mb-0" hint="">
                                            <Input
                                                value={name}
                                                onChange={(event) => setName(event.target.value)}
                                                placeholder={placeholderName}
                                                maxLength={100}
                                            />
                                        </Field>
                                        <Button
                                            type="submit"
                                            icon={UsersRound}
                                            loading={grabbing}
                                            disabled={selection.selectedCount === 0}
                                        >
                                            {selection.selectedCount
                                                ? `Import members of ${selection.selectedCount} group${selection.selectedCount === 1 ? "" : "s"}`
                                                : "Tick groups to import"}
                                        </Button>
                                    </div>
                                    <DataTable
                                        columns={groupColumns}
                                        rows={visibleGroups}
                                        rowKey={(group) => group.id}
                                        selection={selection}
                                        empty={
                                            <EmptyState
                                                variant="plain"
                                                icon={UsersRound}
                                                title={groups.length ? "No group matches" : "This number is in no groups"}
                                                description={
                                                    groups.length
                                                        ? "Try a different search."
                                                        : "Join a group on the phone and reload."
                                                }
                                            />
                                        }
                                    />
                                </form>
                                <Alert tone="info" className="mt-4 mb-0">
                                    Members of a group have not agreed to hear from you. They are saved with no consent
                                    recorded, so a campaign that requires consent will skip them until you record where it
                                    came from. Numbers WhatsApp hides in a group are counted, never guessed.
                                </Alert>
                            </Card>
                        )}

                        {grab && (
                            <Card className="mb-6">
                                <div className="flex items-start justify-between gap-3 flex-wrap">
                                    <div className="min-w-0">
                                        <p className="text-xs font-semibold uppercase tracking-wide text-ink-500 m-0">
                                            Latest import
                                        </p>
                                        <p className="text-sm text-ink-600 m-0 mt-1">
                                            {grab.groups.map((group) => group.subject || "Unnamed").join(", ")} → “
                                            {grab.contact_group_name}”
                                        </p>
                                    </div>
                                    {IMPORT_LIVE.has(grab.contact_import?.status) ? (
                                        <Badge tone="blue">Importing</Badge>
                                    ) : grab.contact_import?.status === "FAILED" ? (
                                        <Badge tone="red">Failed</Badge>
                                    ) : (
                                        <Badge tone="green">Done</Badge>
                                    )}
                                </div>
                                <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 m-0">
                                    {[
                                        ["Members found", grab.members_found],
                                        ["New contacts", grab.contact_import?.created_count ?? 0],
                                        ["Already here", (grab.contact_import?.updated_count ?? 0) + (grab.contact_import?.skipped_count ?? 0)],
                                        ["Hidden by WhatsApp", grab.members_hidden],
                                    ].map(([label, value]) => (
                                        <div key={label} className="rounded-lg border border-ink-200 bg-surface p-3">
                                            <dt className="text-xs font-medium text-ink-500">{label}</dt>
                                            <dd className="text-xl font-bold text-ink-900 m-0 mt-1 tabular-nums">
                                                {value.toLocaleString()}
                                            </dd>
                                        </div>
                                    ))}
                                </dl>
                                {grab.contact_import?.status === "FAILED" && grab.contact_import.errors?.[0] && (
                                    <Alert className="mt-4 mb-0">{grab.contact_import.errors[0].error}</Alert>
                                )}
                                <div className="mt-4">
                                    <Link to="/contacts" className="no-underline">
                                        <Button size="sm" variant="secondary" icon={ContactIcon}>
                                            Open contacts
                                        </Button>
                                    </Link>
                                </div>
                            </Card>
                        )}

                        <Card>
                            <CardHeader title="Recent imports" className="mb-4" />
                            <DataTable
                                columns={grabColumns}
                                rows={grabs}
                                onRowClick={(row) => setGrab(row)}
                                empty={
                                    <EmptyState
                                        variant="plain"
                                        title="Nothing imported yet"
                                        description="Load a number's groups above, tick the ones you want, and import."
                                    />
                                }
                            />
                        </Card>
                    </>
                )}
            </FeatureGate>
        </div>
    );
}
