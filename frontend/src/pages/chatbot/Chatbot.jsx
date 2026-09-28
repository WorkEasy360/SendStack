import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BarChart3, Bot, GitBranch, LayoutDashboard, ListChecks, MessageSquare } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import PageHeader from "../../components/ui/PageHeader";
import Tabs from "../../components/ui/Tabs";
import Alert from "../../components/ui/Alert";
import Spinner from "../../components/ui/Spinner";
import ChatbotOverview from "./ChatbotOverview";
import ChatbotBots from "./ChatbotBots";
import ChatbotRules from "./ChatbotRules";
import ChatbotFlow from "./ChatbotFlow";
import ChatbotInbox from "./ChatbotInbox";
import ChatbotAnalytics from "./ChatbotAnalytics";

/**
 * The Chatbot section.
 *
 * Tabbed because the six screens are read at different times and by different
 * people: an agent lives in the Inbox all day, a manager visits Bots once to
 * set it up and Analytics on a Monday. Overview comes first because the
 * question people arrive with is "is it working, and how much is it doing?".
 *
 * The active tab and the selected bot both live in the URL, so a link to a
 * particular bot's rules is a link somebody can actually send to a colleague.
 */

const TABS = [
    { id: "overview", label: "Overview", icon: LayoutDashboard },
    { id: "bots", label: "Bots", icon: Bot },
    { id: "flows", label: "Flows", icon: GitBranch },
    { id: "rules", label: "Rules", icon: ListChecks },
    { id: "inbox", label: "Inbox", icon: MessageSquare },
    { id: "analytics", label: "Analytics", icon: BarChart3 },
];

export default function Chatbot() {
    const [params, setParams] = useSearchParams();
    const tab = TABS.some((entry) => entry.id === params.get("tab")) ? params.get("tab") : "overview";

    const [bots, setBots] = useState([]);
    const [accounts, setAccounts] = useState([]);
    const [permissions, setPermissions] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const selectedId = params.get("bot") ? Number(params.get("bot")) : null;
    const selected = bots.find((bot) => bot.id === selectedId) || bots[0] || null;

    const setTab = (next) => {
        const updated = new URLSearchParams(params);
        updated.set("tab", next);
        setParams(updated, { replace: true });
    };

    const selectBot = (id) => {
        const updated = new URLSearchParams(params);
        if (id) updated.set("bot", String(id));
        else updated.delete("bot");
        setParams(updated, { replace: true });
    };

    const loadBots = useCallback(async () => {
        try {
            const response = await api.get("/chatbot/bots/", { params: { page_size: 100 } });
            setBots(response.data.results ?? response.data);
            setError("");
        } catch (requestError) {
            setError(apiErrorMessage(requestError, "Could not load your chatbots."));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadBots();
        // Capabilities and permissions change rarely and are needed by three of
        // the tabs, so they are fetched once here rather than by each of them.
        api.get("/chatbot/capabilities/")
            .then((response) => {
                setAccounts(response.data.accounts || []);
                setPermissions(response.data.permissions || []);
            })
            .catch(() => {
                // Not fatal: the tabs below degrade to "no numbers listed"
                // rather than refusing to render.
            });
    }, [loadBots]);

    const can = (permission) => permissions.includes(permission);

    if (loading) {
        return (
            <div>
                <PageHeader title="Chatbot" description="Automated WhatsApp conversations, and the people who take over." />
                <div className="flex justify-center py-20">
                    <Spinner />
                </div>
            </div>
        );
    }

    return (
        <div>
            <PageHeader
                title="Chatbot"
                description="Automated WhatsApp conversations, and the people who take over."
            />

            {error && (
                <Alert tone="error" className="mb-4">
                    {error}
                </Alert>
            )}

            <Tabs tabs={TABS} active={tab} onChange={setTab} />

            {tab === "overview" && <ChatbotOverview onOpenTab={setTab} bots={bots} />}
            {tab === "bots" && (
                <ChatbotBots
                    bots={bots}
                    accounts={accounts}
                    onChanged={loadBots}
                    onOpenTab={setTab}
                    onSelectBot={selectBot}
                    can={can}
                />
            )}
            {tab === "flows" && (
                <ChatbotFlow bots={bots} selected={selected} onSelectBot={selectBot} onChanged={loadBots} can={can} />
            )}
            {tab === "rules" && (
                <ChatbotRules bots={bots} selected={selected} onSelectBot={selectBot} onChanged={loadBots} can={can} />
            )}
            {tab === "inbox" && <ChatbotInbox bots={bots} accounts={accounts} can={can} />}
            {tab === "analytics" && <ChatbotAnalytics bots={bots} selected={selected} onSelectBot={selectBot} />}
        </div>
    );
}
