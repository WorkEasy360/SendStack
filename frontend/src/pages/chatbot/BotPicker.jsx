import { Select } from "../../components/ui/Field";
import { StatusBadge } from "../../components/ui/Badge";

/**
 * "Which bot am I looking at?", shared by the Rules, Flows and Analytics tabs.
 *
 * A select rather than a sidebar because most workspaces have one or two bots,
 * and a whole column of chrome for a two-item list would take space from the
 * thing actually being edited.
 */
export default function BotPicker({ bots, selected, onSelect, label = "Chatbot" }) {
    if (bots.length <= 1) {
        return selected ? (
            <div className="flex items-center gap-2 min-w-0">
                <h2 className="text-base font-bold text-ink-900 m-0 truncate">{selected.name}</h2>
                <StatusBadge status={selected.status} />
                <span className="text-sm text-ink-500 truncate">{selected.account_name}</span>
            </div>
        ) : null;
    }

    return (
        <div className="flex items-center gap-3 min-w-0">
            <div className="w-64">
                <Select
                    value={selected?.id || ""}
                    onChange={(event) => onSelect(Number(event.target.value))}
                    aria-label={label}
                >
                    {bots.map((bot) => (
                        <option key={bot.id} value={bot.id}>
                            {bot.name} — {bot.account_name}
                        </option>
                    ))}
                </Select>
            </div>
            {selected && <StatusBadge status={selected.status} />}
        </div>
    );
}
