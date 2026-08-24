import {
    BarChart3,
    Bot,
    BrainCircuit,
    Coins,
    Contact,
    FileText,
    Handshake,
    ListChecks,
    Library,
    LayoutDashboard,
    MessageSquare,
    PhoneCall,
    Send,
    Settings,
    ShieldCheck,
    Building2,
    BriefcaseBusiness,
    Layers,
    Palette,
    Smartphone,
    UserCog,
    Users,
} from "lucide-react";

/**
 * Sidebar navigation.
 *
 * Every entry maps 1:1 onto a route already registered in App.jsx — the
 * grouping below is presentational only and does not change routing.
 * `roles: null` means visible to everyone; otherwise the item is shown only
 * to the listed roles (matching the backend's own permission model).
 */
export const NAV_GROUPS = [
    {
        label: "Overview",
        items: [
            { to: "/", label: "Dashboard", icon: LayoutDashboard, roles: null, end: true },
            { to: "/reports", label: "Reports", icon: BarChart3, roles: null },
        ],
    },
    {
        label: "Messaging",
        items: [
            { to: "/campaigns", label: "Campaigns", icon: Send, roles: null },
            { to: "/inbox", label: "Inbox", icon: MessageSquare, roles: null },
            { to: "/automation", label: "Automation", icon: Bot, roles: null },
            { to: "/ai", label: "AI Agent", icon: BrainCircuit, roles: null },
            { to: "/knowledge", label: "Knowledge base", icon: Library, roles: null },
            { to: "/voice", label: "Calling Agent", icon: PhoneCall, roles: null },
            { to: "/templates", label: "Templates", icon: FileText, roles: null },
        ],
    },
    {
        // Contacts, the deals hanging off them and the work owed on them are
        // one job, so they sit together. Named "Sales" rather than
        // "Customers" because the reseller group below already has a
        // Customers item meaning something entirely different (tenants).
        label: "Sales",
        items: [
            { to: "/contacts", label: "Contacts", icon: Contact, roles: null },
            { to: "/pipeline", label: "Pipeline", icon: Handshake, roles: null },
            { to: "/tasks", label: "Tasks", icon: ListChecks, roles: null },
        ],
    },
    {
        label: "Manage",
        items: [
            { to: "/whatsapp", label: "Devices", icon: Smartphone, roles: null },
            { to: "/business", label: "Business profile", icon: BriefcaseBusiness, roles: null },
            { to: "/team", label: "Team", icon: Users, roles: null },
            { to: "/compliance", label: "Compliance", icon: ShieldCheck, roles: null },
        ],
    },
    {
        label: "Reseller",
        items: [
            { to: "/customers", label: "Customers", icon: Building2, roles: ["SUPERADMIN", "RESELLER"] },
            { to: "/plans", label: "Plans", icon: Layers, roles: ["SUPERADMIN", "RESELLER"] },
            { to: "/users", label: "Users", icon: UserCog, roles: ["SUPERADMIN", "RESELLER"] },
            { to: "/branding", label: "Branding", icon: Palette, roles: ["SUPERADMIN", "RESELLER"] },
        ],
    },
    {
        label: "Account",
        items: [
            { to: "/credits", label: "Credits", icon: Coins, roles: null },
            { to: "/settings", label: "Settings", icon: Settings, roles: null },
        ],
    },
];

export function visibleGroups(role) {
    return NAV_GROUPS.map((group) => ({
        ...group,
        items: group.items.filter((item) => !item.roles || item.roles.includes(role)),
    })).filter((group) => group.items.length > 0);
}
