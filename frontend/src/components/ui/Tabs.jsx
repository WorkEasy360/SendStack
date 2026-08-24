/** Underlined tab bar. `tabs` is an array of { id, label, icon?, count? }. */
export default function Tabs({ tabs, active, onChange, className = "" }) {
    return (
        <div className={`border-b border-ink-200 mb-6 ${className}`}>
            <div className="scroll-x flex gap-1 -mb-px" role="tablist">
                {tabs.map((tab) => {
                    const isActive = tab.id === active;
                    const Icon = tab.icon;
                    return (
                        <button
                            key={tab.id}
                            type="button"
                            role="tab"
                            aria-selected={isActive}
                            onClick={() => onChange(tab.id)}
                            className={`inline-flex items-center gap-2 px-4 py-2.5 text-sm font-semibold whitespace-nowrap cursor-pointer bg-transparent border-0 border-b-2 transition-colors ${
                                isActive
                                    ? "border-brand-600 text-brand-700"
                                    : "border-transparent text-ink-500 hover:text-ink-800 hover:border-ink-300"
                            }`}
                        >
                            {Icon && <Icon size={15} aria-hidden="true" />}
                            {tab.label}
                            {tab.count != null && (
                                <span
                                    className={`px-1.5 py-0.5 rounded text-xs font-bold ${
                                        isActive ? "bg-brand-50 text-brand-700" : "bg-ink-100 text-ink-500"
                                    }`}
                                >
                                    {tab.count}
                                </span>
                            )}
                        </button>
                    );
                })}
            </div>
        </div>
    );
}
