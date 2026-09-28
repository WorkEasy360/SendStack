import { Monitor, Moon, Sun } from "lucide-react";
import { useThemePreference } from "../../utils/theme";

const OPTIONS = [
    { id: "light", label: "Light", icon: Sun },
    { id: "system", label: "Match system", icon: Monitor },
    { id: "dark", label: "Dark", icon: Moon },
];

/** Three-way theme switch: light, follow the system, dark. */
export default function ThemeToggle({ className = "" }) {
    const [preference, setPreference] = useThemePreference();

    return (
        <div
            role="group"
            aria-label="Colour theme"
            className={`inline-flex items-center rounded-lg border border-ink-200 bg-surface p-0.5 ${className}`}
        >
            {OPTIONS.map(({ id, label, icon: Icon }) => {
                const active = preference === id;
                return (
                    <button
                        key={id}
                        type="button"
                        onClick={() => setPreference(id)}
                        aria-pressed={active}
                        aria-label={label}
                        title={label}
                        className={`w-7 h-7 inline-flex items-center justify-center rounded-md cursor-pointer border-none transition-colors ${
                            active
                                ? "bg-ink-900 text-ink-50"
                                : "bg-transparent text-ink-500 hover:text-ink-900 hover:bg-ink-100"
                        }`}
                    >
                        <Icon size={14} aria-hidden="true" />
                    </button>
                );
            })}
        </div>
    );
}
