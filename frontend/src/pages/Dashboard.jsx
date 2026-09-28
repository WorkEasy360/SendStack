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
import PlanUsage from "../components/billing/PlanUsage";
import OnboardingChecklist from "../components/onboarding/OnboardingChecklist";
import TrialBanner from "../components/billing/TrialBanner";

// `phrase` completes "...your workspace <phrase>." -- "over the last today"
// was not a sentence. Lifetime sends no range at all: the reports API treats a
// missing range as all-time (the Reports page does the same).
const RANGES = [
    { id: "today", label: "Today", phrase: "today" },
    { id: "last_7_days", label: "7 days", phrase: "over the last 7 days" },
    { id: "last_30_days", label: "30 days", phrase: "over the last 30 days" },
    { id: "all_time", label: "Lifetime", phrase: "over its lifetime" },
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
    const [onboarding, setOnboarding] = useState(null);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);

        async function load() {
            try {
                const [dashboardRes, balanceRes, campaignsRes] = await Promise.all([
                    api.get("/reports/dashboard/", { params: range === "all_time" ? {} : { range } }),
                    api.get("/credits/balance/"),
                    api.get("/campaigns/campaigns/", { params: { ordering: "-created_at", page_size: 5 } }),
                ]);
                if (cancelled) return;
                setMetrics(dashboardRes.data);
                setBalance(balanceRes.data);
                setCampaigns(campaignsRes.data.results || []);

                // Fetched separately and deliberately not in the Promise.all
                // above: a workspace that has finished onboarding does not
                // need this, and a failure here must not blank the dashboard.
                try {
                    const onboardingRes = await api.get("/onboarding/");
                    if (!cancelled) setOnboarding(onboardingRes.data);
                } catch {
                    if (!cancelled) setOnboarding(null);
                }
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

    const rangePhrase = RANGES.find((item) => item.id === range)?.phrase;

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
                description={`Here's what's happening with your workspace ${rangePhrase}.`}
                actions={
                    <div className="inline-flex rounded-lg border border-ink-200 bg-surface p-0.5 shadow-card">
                        {RANGES.map((item) => (
                            <button
                                key={item.id}
                                type="button"
                                onClick={() => setRange(item.id)}
                                className={`px-3 py-1.5 rounded-md text-sm font-semibold cursor-pointer border-none transition-colors ${
                                    range === item.id
                                        ? "bg-ink-900 text-ink-50"
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

            {/*
              First-run setup, and the trial state that goes with it. Rendered
              from server state, and it disappears on its own once the
              workspace is set up -- there is nothing to dismiss for a customer
              who has been here a year, because they never see it.
            */}
            <TrialBanner trial={onboarding?.trial} />
            {onboarding && !onboarding.dismissed_at && (
                <OnboardingChecklist
                    state={onboarding}
                    className="mb-6"
                    onDismiss={async () => {
                        setOnboarding((current) => ({ ...current, dismissed_at: new Date().toISOString() }));
                        try {
                            await api.post("/onboarding/", { action: "dismiss" });
                        } catch {
                            // A hidden checklist that comes back on the next
                            // load is a small annoyance, not an error worth
                            // interrupting somebody for.
                        }
                    }}
                />
            )}

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
                    {/*
                      A brand-new workspace has four zeroes and a blank table,
                      which tells somebody nothing about what to do next. The
                      checklist above already says it, so this is only for a
                      workspace that dismissed it and still has nothing set up.
                    */}
                    {metrics && onboarding?.dismissed_at && !metrics.connected_whatsapp_accounts && (
                        <Card className="mb-4">
                            <CardHeader
                                title="Nothing is connected yet"
                                description="Link a WhatsApp number and this dashboard starts filling in."
                                action={
                                    <Link to="/whatsapp" className="no-underline">
                                        <Button size="sm" icon={Smartphone}>Connect WhatsApp</Button>
                                    </Link>
                                }
                                className="mb-0"
                            />
                        </Card>
                    )}

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

                    {/* Above the campaign list rather than below it: somebody
                        two hundred messages from the end of their month needs
                        to know that before they open the campaign builder. */}
                    <PlanUsage className="mb-6" />

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
                                    metrics && !metrics.connected_whatsapp_accounts ? (
                                        // Offering "New campaign" to somebody
                                        // with no connected number sends them
                                        // into a form they cannot submit.
                                        <EmptyState
                                            icon={Smartphone}
                                            title="Connect WhatsApp first"
                                            description="A campaign needs a connected number to send from. It takes about a minute."
                                            action={
                                                <Link to="/whatsapp" className="no-underline">
                                                    <Button icon={Smartphone}>Connect WhatsApp</Button>
                                                </Link>
                                            }
                                        />
                                    ) : metrics && !metrics.total_contacts ? (
                                        <EmptyState
                                            icon={Contact}
                                            title="Add some contacts"
                                            description="A campaign needs an audience. Add one contact by hand, or import a CSV."
                                            action={
                                                <Link to="/contacts" className="no-underline">
                                                    <Button icon={Contact}>Add contacts</Button>
                                                </Link>
                                            }
                                        />
                                    ) : (
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
                                    )
                                }
                            />
                        </div>
                    </Card>
                </>
            )}
        </div>
    );
}
