import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { UserCheck } from "lucide-react";
import Sidebar from "../components/layout/Sidebar";
import Topbar from "../components/layout/Topbar";
import { useAuth } from "../context/AuthContext";

const COLLAPSE_KEY = "sidebar_collapsed";

/**
 * App shell: the sidebar and topbar are fixed panes and only the main
 * region scrolls, so navigation stays reachable at any scroll depth. The
 * impersonation banner sits above the whole shell and shrinks it, rather
 * than overlapping it.
 */
export default function AppLayout() {
    const { user, logout, impersonation, exitImpersonation } = useAuth();
    const { pathname } = useLocation();

    const [mobileNavOpen, setMobileNavOpen] = useState(false);
    const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === "1");

    useEffect(() => {
        localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    }, [collapsed]);

    // Route changes close the mobile drawer; without this it stays open over
    // the page the user just navigated to.
    useEffect(() => {
        setMobileNavOpen(false);
    }, [pathname]);

    useEffect(() => {
        if (!mobileNavOpen) return undefined;
        const onKeyDown = (event) => {
            if (event.key === "Escape") setMobileNavOpen(false);
        };
        document.addEventListener("keydown", onKeyDown);
        return () => document.removeEventListener("keydown", onKeyDown);
    }, [mobileNavOpen]);

    const sidebarProps = {
        user,
        onLogout: logout,
        onToggleCollapse: () => setCollapsed((value) => !value),
    };

    return (
        <div className="h-screen flex flex-col overflow-hidden bg-ink-50">
            {impersonation && (
                <div className="shrink-0 bg-amber-500 text-white text-sm font-medium flex flex-wrap items-center justify-center gap-x-3 gap-y-1 px-4 py-2 text-center">
                    <span className="inline-flex items-center gap-1.5">
                        <UserCheck size={15} aria-hidden="true" />
                        {impersonation.originalUsername} is logged in as {impersonation.actingAsUsername}
                    </span>
                    <button
                        type="button"
                        onClick={exitImpersonation}
                        className="underline underline-offset-2 font-semibold cursor-pointer bg-transparent border-none text-white p-0"
                    >
                        Exit and return to my account
                    </button>
                </div>
            )}

            <div className="flex flex-1 min-h-0">
                <aside
                    className={`hidden lg:block shrink-0 transition-[width] duration-200 ${collapsed ? "w-17" : "w-64"}`}
                >
                    <Sidebar {...sidebarProps} collapsed={collapsed} isMobile={false} />
                </aside>

                {mobileNavOpen && (
                    <div className="lg:hidden fixed inset-0 z-50 flex">
                        <div
                            className="absolute inset-0 bg-ink-900/50 animate-fade-in"
                            onClick={() => setMobileNavOpen(false)}
                            role="presentation"
                        />
                        <div className="relative w-72 max-w-[85vw] h-full shadow-overlay animate-slide-in-right">
                            <Sidebar
                                {...sidebarProps}
                                collapsed={false}
                                isMobile
                                onCloseMobile={() => setMobileNavOpen(false)}
                                onNavigate={() => setMobileNavOpen(false)}
                            />
                        </div>
                    </div>
                )}

                <div className="flex-1 min-w-0 flex flex-col min-h-0">
                    <Topbar user={user} onOpenMobileNav={() => setMobileNavOpen(true)} />
                    <main className="scroll-y flex-1 min-h-0">
                        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
                            <Outlet />
                        </div>
                    </main>
                </div>
            </div>
        </div>
    );
}
