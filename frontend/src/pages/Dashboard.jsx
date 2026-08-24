import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
    ArrowRight,
    CheckCheck,
    Coins,
    Contact,
    Eye,
    Send,
    Smartphone,
    TriangleAlert,
} from "lucide-react";
import api, { apiErrorMessage } from "../services/api";
import { useAuth } from "../context/AuthContext";
import Alert from "../components/ui/Alert";
import Button from "../components/ui/Button";
import StatCard from "../components/ui/StatCard";
import Card, { CardHeader } from "../components/ui/Card";
import DataTable from "../components/ui/DataTable";
import { StatusBadge } from "../components/ui/Badge";
import EmptyState from "../components/ui/EmptyState";
import PageHeader from "../components/ui/PageHeader";
import { StatCardSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import TodayPanel from "../components/crm/TodayPanel";

const RANGES = [
    { id: "today", label: "Today" },
    { id: "last_7_days", label: "7 days" },
    { id: "last_30_days", label: "30 days" },
];

function formatDate(value) {
    if (!value) return "—";
    return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function Dashboard() {
    const { user } = useAuth();
    const [range, setRange] = useState("last_30_days");
    const [metrics, setMetrics] = useState(null);
    const [balance, setBalance] = useState(null);
    const [campaigns, setCampaigns] = useState([]);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);

        async function load() {
            try {
                const [dashboardRes, balanceRes, campaignsRes] = await Promise.all([
                    api.get("/reports/dashboard/", { params: { range } }),
                    api.get("/credits/balance/"),
                    api.get("/campaigns/campaigns/", { params: { ordering: "-created_at", page_size: 5 } }),
                ]);
                if (cancelled) return;
                setMetrics(dashboardRes.data);
                setBalance(balanceRes.data);
                setCampaigns(campaignsRes.data.results || []);
            } catch (err) {
                if (!cancelled) setError(apiErrorMessage(err, "Could not load your dashboard."));
            } finally {
                if (!cancelled) setLoading(false);
            }
        }

        load();
        return () => {
            cancelled = true;
        };
    }, [range]);

    const rangeLabel = RANGES.find((item) => item.id === range)?.label.toLowerCase();

    const columns = [
        {
            key: "name",
            header: "Campaign",
            render: (campaign) => (
                <Link
                    to={`/campaigns/${campaign.id}`}
                    className="font-semibold text-ink-900 hover:text-brand-700 no-underline"
                >
                    {campaign.name}
                </Link>
            ),
        },
        { key: "status", header: "Status", render: (campaign) => <StatusBadge status={campaign.status} /> },
        {
            key: "recipients",
            header: "Recipients",
            align: "right",
            hideBelow: "sm",
            render: (campaign) => <span className="tabular-nums">{campaign.total_recipients}</span>,
        },
        {
            key: "sent",
            header: "Sent",
            align: "right",
            render: (campaign) => <span className="tabular-nums">{campaign.sent_count}</span>,
        },
        {
            key: "created",
            header: "Created",
            align: "right",
            hideBelow: "md",
            render: (campaign) => <span className="text-ink-500">{formatDate(campaign.created_at)}</span>,
        },
    ];

    return (
        <div>
            <PageHeader
                title={`Welcome back, ${user?.username || "there"}`}
                description={`Here's what's happening with your workspace over the last ${rangeLabel}.`}
                actions={
                    <div className="inline-flex rounded-lg border border-ink-200 bg-white p-0.5 shadow-card">
                        {RANGES.map((item) => (
                            <button
                                key={item.id}
                                type="button"
                                onClick={() => setRange(item.id)}
                                className={`px-3 py-1.5 rounded-md text-sm font-semibold cursor-pointer border-none transition-colors ${
                                    range === item.id
                                        ? "bg-ink-900 text-white"
                                        : "bg-transparent text-ink-500 hover:text-ink-900"
                                }`}
                            >
                                {item.label}
                            </button>
                        ))}
                    </div>
                }
            />

            <Alert>{error}</Alert>

            {/* Before any chart: what is actually waiting on this person right
                now. The numbers below say how the month went; this says what to
                do about today, and it loads independently so a slow report
                never delays it. */}
            <TodayPanel />

            {loading ? (
                <div className="space-y-4">
                    <StatCardSkeleton count={4} />
                    <StatCardSkeleton count={4} />
                    <TableSkeleton rows={4} columns={5} />
                </div>
            ) : (
                <>
                    {metrics && (
                        <>
                            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                                <StatCard
                                    label="Devices"
                                    value={metrics.whatsapp_accounts}
                                    hint={`${metrics.connected_whatsapp_accounts} connected`}
                                    icon={Smartphone}
                                    tone="brand"
                                    to="/whatsapp"
                                />
                                <StatCard
                                    label="Contacts"
                                    value={metrics.total_contacts}
                                    icon={Contact}
                                    tone="blue"
                                    to="/contacts"
                                />
                                <StatCard
                                    label="Campaigns"
                                    value={metrics.total_campaigns}
                                    hint={`${metrics.active_campaigns} active`}
                                    icon={Send}
                                    tone="violet"
                                    to="/campaigns"
                                />
                                <StatCard
                                    label="Credits"
                                    value={balance ? balance.available_credits : "—"}
                                    hint={balance ? `${balance.reserved_credits} reserved` : ""}
                                    icon={Coins}
                                    tone="amber"
                                    to="/credits"
                                />
                            </div>

                            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                                <StatCard label="Messages sent" value={metrics.sent} icon={Send} tone="brand" />
                                <StatCard
                                    label="Delivered"
                                    value={metrics.delivered}
                                    hint={`${metrics.delivery_rate}% delivery rate`}
                                    icon={CheckCheck}
                                    tone="brand"
                                />
                                <StatCard
                                    label="Read"
                                    value={metrics.read}
                                    hint={`${metrics.read_rate}% read rate`}
                                    icon={Eye}
                                    tone="blue"
                                />
                                <StatCard
                                    label="Failed"
                                    value={metrics.failed}
                                    hint="Needs attention"
                                    icon={TriangleAlert}
                                    tone="red"
                                />
                            </div>
                        </>
                    )}

                    <Card padded={false}>
                        <div className="p-5 sm:p-6 pb-4">
                            <CardHeader
                                title="Recent campaigns"
                                description="Your five most recently created campaigns."
                                action={
                                    <Link to="/campaigns" className="no-underline">
                                        <Button variant="secondary" size="sm" icon={ArrowRight}>
                                            View all
                                        </Button>
                                    </Link>
                                }
                            />
                        </div>
                        <div className="px-5 sm:px-6 pb-5 sm:pb-6">
                            <DataTable
                                columns={columns}
                                rows={campaigns}
                                empty={
                                    <EmptyState
                                        icon={Send}
                                        title="No campaigns yet"
                                        description="Create your first campaign to start sending WhatsApp messages."
                                        action={
                                            <Link to="/campaigns/new" className="no-underline">
                                                <Button icon={Send}>New Campaign</Button>
                                            </Link>
                                        }
                                    />
                                }
                            />
                        </div>
                    </Card>
                </>
            )}
        </div>
    );
}
