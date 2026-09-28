import { Checkbox } from "./Field";

/**
 * Table shell used by every list page.
 *
 * `columns` entries: { key, header, align?, className?, hideBelow?, render(row) }
 * `hideBelow` ("sm" | "md" | "lg" | "xl") drops lower-priority columns on
 * small screens; the table itself also scrolls horizontally so nothing is
 * ever unreachable on mobile.
 *
 * Pass `selection` (the object returned by `useSelection`) to prepend a
 * checkbox column with a select-all control in the header. Rows the selection
 * excludes render an empty cell, so the columns still line up.
 */
const ALIGN = { left: "text-left", right: "text-right", center: "text-center" };
const HIDE_BELOW = {
    sm: "hidden sm:table-cell",
    md: "hidden md:table-cell",
    lg: "hidden lg:table-cell",
    xl: "hidden xl:table-cell",
};

export default function DataTable({ columns, rows, rowKey = (row) => row.id, onRowClick, empty, footer, selection }) {
    if (!rows.length && empty) return empty;

    return (
        <div className="bg-surface border border-ink-200 rounded-card shadow-card overflow-hidden">
            <div className="scroll-x">
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-ink-50 border-b border-ink-200">
                            {selection && (
                                <th scope="col" className="w-10 px-4 py-3 text-left">
                                    <Checkbox
                                        checked={selection.allSelected}
                                        indeterminate={selection.someSelected}
                                        onChange={selection.toggleAll}
                                        disabled={selection.selectableCount === 0}
                                        aria-label={selection.allSelected ? "Deselect all rows" : "Select all rows"}
                                    />
                                </th>
                            )}
                            {columns.map((column) => (
                                <th
                                    key={column.key}
                                    scope="col"
                                    className={`px-4 py-3 text-xs font-semibold uppercase tracking-wide text-ink-500 whitespace-nowrap ${
                                        ALIGN[column.align || "left"]
                                    } ${column.hideBelow ? HIDE_BELOW[column.hideBelow] : ""} ${column.className || ""}`}
                                >
                                    {column.header}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => {
                            const id = rowKey(row);
                            const selectable = selection ? selection.canSelect(row) : false;
                            const isSelected = selectable && selection.isSelected(selection.getId(row));
                            return (
                                <tr
                                    key={id}
                                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                                    className={`border-b border-ink-100 last:border-0 transition-colors ${
                                        isSelected ? "bg-brand-50/60" : ""
                                    } ${onRowClick ? "cursor-pointer hover:bg-ink-50" : "hover:bg-ink-50/60"}`}
                                >
                                    {selection && (
                                        // The cell swallows the click so ticking a row never also
                                        // opens it on tables that navigate on row click.
                                        <td className="px-4 py-3.5 align-middle" onClick={(event) => event.stopPropagation()}>
                                            {selectable && (
                                                <Checkbox
                                                    checked={isSelected}
                                                    onChange={() => selection.toggle(selection.getId(row))}
                                                    aria-label={`Select row ${id}`}
                                                />
                                            )}
                                        </td>
                                    )}
                                    {columns.map((column) => (
                                        <td
                                            key={column.key}
                                            className={`px-4 py-3.5 text-ink-700 align-middle ${ALIGN[column.align || "left"]} ${
                                                column.hideBelow ? HIDE_BELOW[column.hideBelow] : ""
                                            } ${column.className || ""}`}
                                        >
                                            {column.render(row)}
                                        </td>
                                    ))}
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {footer && <div className="border-t border-ink-200 px-4 py-3 bg-ink-50">{footer}</div>}
        </div>
    );
}

/** Inline text action for a table row's action cell. */
export function RowAction({ tone = "default", className = "", ...props }) {
    const tones = {
        default: "text-ink-500 hover:text-ink-900",
        brand: "text-brand-700 hover:text-brand-800",
        danger: "text-red-600 hover:text-red-700",
    };
    return (
        <button
            type="button"
            className={`text-sm font-semibold cursor-pointer bg-transparent border-none p-0 transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${tones[tone]} ${className}`}
            {...props}
        />
    );
}
