import { NavLink } from "react-router-dom";
import { LogOut, PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import Logo from "./Logo";
import { visibleGroups } from "../../config/navigation";

function NavItem({ item, collapsed, onNavigate }) {
    const Icon = item.icon;
    return (
        <NavLink
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            title={collapsed ? item.label : undefined}
            className={({ isActive }) =>
                `group relative flex items-center gap-3 rounded-lg text-sm font-medium no-underline transition-colors duration-150 ${
                    collapsed ? "justify-center px-0 py-2.5" : "px-3 py-2.5"
                } ${
                    isActive
                        ? "bg-brand-50 text-brand-700 font-semibold"
                        : "text-ink-600 hover:bg-ink-100 hover:text-ink-900"
                }`
            }
        >
            {({ isActive }) => (
                <>
                    {/* Active indicator rail — reads at a glance even when collapsed. */}
                    <span
                        className={`absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-r-full bg-brand-600 transition-opacity ${
                            isActive ? "opacity-100" : "opacity-0"
                        }`}
                    />
                    <Icon size={18} strokeWidth={isActive ? 2.25 : 2} className="shrink-0" aria-hidden="true" />
                    {!collapsed && <span className="truncate">{item.label}</span>}
                </>
            )}
        </NavLink>
    );
}

export default function Sidebar({ user, collapsed, onToggleCollapse, onLogout, onNavigate, onCloseMobile, isMobile }) {
    const groups = visibleGroups(user?.role);

    return (
        <div className="h-full flex flex-col bg-white border-r border-ink-200">
            <div
                className={`h-16 shrink-0 flex items-center border-b border-ink-200 ${
                    collapsed ? "justify-center px-2" : "justify-between px-4"
                }`}
            >
                <Logo collapsed={collapsed} />
                {isMobile ? (
                    <button
                        type="button"
                        onClick={onCloseMobile}
                        aria-label="Close navigation"
                        className="w-8 h-8 inline-flex items-center justify-center rounded-lg text-ink-500 hover:bg-ink-100 hover:text-ink-900 cursor-pointer bg-transparent border-none transition-colors"
                    >
                        <X size={18} />
                    </button>
                ) : (
                    !collapsed && (
                        <button
                            type="button"
                            onClick={onToggleCollapse}
                            aria-label="Collapse sidebar"
                            title="Collapse sidebar"
                            className="w-8 h-8 inline-flex items-center justify-center rounded-lg text-ink-400 hover:bg-ink-100 hover:text-ink-800 cursor-pointer bg-transparent border-none transition-colors"
                        >
                            <PanelLeftClose size={17} />
                        </button>
                    )
                )}
            </div>

            <nav className="scroll-y flex-1 py-4 px-3" aria-label="Main navigation">
                {groups.map((group, index) => (
                    <div key={group.label} className={index > 0 ? "mt-5" : ""}>
                        {!collapsed && (
                            <p className="px-3 mb-1.5 text-[11px] font-bold uppercase tracking-wider text-ink-400">
                                {group.label}
                            </p>
                        )}
                        {collapsed && index > 0 && <div className="mx-2 mb-3 border-t border-ink-200" />}
                        <div className="space-y-0.5">
                            {group.items.map((item) => (
                                <NavItem key={item.to} item={item} collapsed={collapsed} onNavigate={onNavigate} />
                            ))}
                        </div>
                    </div>
                ))}
            </nav>

            {!isMobile && collapsed && (
                <button
                    type="button"
                    onClick={onToggleCollapse}
                    aria-label="Expand sidebar"
                    title="Expand sidebar"
                    className="mx-auto mb-2 w-9 h-9 inline-flex items-center justify-center rounded-lg text-ink-400 hover:bg-ink-100 hover:text-ink-800 cursor-pointer bg-transparent border-none transition-colors"
                >
                    <PanelLeftOpen size={17} />
                </button>
            )}

            <div className={`shrink-0 border-t border-ink-200 ${collapsed ? "p-2" : "p-3"}`}>
                {collapsed ? (
                    <button
                        type="button"
                        onClick={onLogout}
                        title="Sign out"
                        aria-label="Sign out"
                        className="w-full h-9 inline-flex items-center justify-center rounded-lg text-ink-500 hover:bg-red-50 hover:text-red-600 cursor-pointer bg-transparent border-none transition-colors"
                    >
                        <LogOut size={17} />
                    </button>
                ) : (
                    <div className="flex items-center gap-2.5">
                        <span className="shrink-0 w-9 h-9 rounded-lg bg-ink-900 text-white text-sm font-bold flex items-center justify-center uppercase">
                            {(user?.username || "?").charAt(0)}
                        </span>
                        <div className="min-w-0 flex-1">
                            <p className="text-sm font-semibold text-ink-900 truncate m-0">{user?.username}</p>
                            <p className="text-xs text-ink-500 truncate m-0">{user?.tenant_name || user?.role}</p>
                        </div>
                        <button
                            type="button"
                            onClick={onLogout}
                            title="Sign out"
                            aria-label="Sign out"
                            className="shrink-0 w-8 h-8 inline-flex items-center justify-center rounded-lg text-ink-400 hover:bg-red-50 hover:text-red-600 cursor-pointer bg-transparent border-none transition-colors"
                        >
                            <LogOut size={16} />
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
