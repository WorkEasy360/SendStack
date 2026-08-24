import { useLocation } from "react-router-dom";
import { Menu } from "lucide-react";
import Badge from "../ui/Badge";
import { LogoMark } from "./Logo";
import { NAV_GROUPS } from "../../config/navigation";

const ALL_ITEMS = NAV_GROUPS.flatMap((group) => group.items);

function currentSectionLabel(pathname) {
    if (pathname === "/") return "Dashboard";
    // Longest matching prefix wins, so /campaigns/12 still resolves to "Campaigns".
    const match = ALL_ITEMS.filter((item) => item.to !== "/" && pathname.startsWith(item.to)).sort(
        (a, b) => b.to.length - a.to.length
    )[0];
    return match?.label || "";
}

export default function Topbar({ user, onOpenMobileNav }) {
    const { pathname } = useLocation();

    return (
        <header className="h-16 shrink-0 z-30 bg-white border-b border-ink-200 flex items-center gap-3 px-4 sm:px-6">
            <button
                type="button"
                onClick={onOpenMobileNav}
                aria-label="Open navigation"
                className="lg:hidden shrink-0 w-9 h-9 inline-flex items-center justify-center rounded-lg text-ink-600 hover:bg-ink-100 cursor-pointer bg-transparent border-none transition-colors"
            >
                <Menu size={19} />
            </button>

            <span className="lg:hidden">
                <LogoMark size={28} />
            </span>

            <p className="hidden lg:block text-sm font-semibold text-ink-900 m-0 truncate">
                {currentSectionLabel(pathname)}
            </p>

            <div className="flex-1" />

            {user && (
                <div className="flex items-center gap-3">
                    <Badge tone="gray" className="hidden sm:inline-flex">
                        {user.role}
                    </Badge>
                    <div className="hidden sm:block text-right leading-tight">
                        <p className="text-sm font-semibold text-ink-900 m-0 truncate max-w-[180px]">
                            {user.username}
                        </p>
                        {user.tenant_name && (
                            <p className="text-xs text-ink-500 m-0 truncate max-w-[180px]">{user.tenant_name}</p>
                        )}
                    </div>
                    <span className="shrink-0 w-9 h-9 rounded-lg bg-ink-900 text-white text-sm font-bold flex items-center justify-center uppercase">
                        {(user.username || "?").charAt(0)}
                    </span>
                </div>
            )}
        </header>
    );
}
