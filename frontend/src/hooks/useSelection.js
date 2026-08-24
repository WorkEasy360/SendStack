import { useCallback, useMemo, useRef, useState } from "react";

/**
 * Multi-selection state for one list, shared by every page that offers bulk
 * actions.
 *
 * Selection is always intersected with the rows currently on screen, so a
 * reload, a filter change or someone else deleting a row can never leave a
 * stale id in the payload sent to a bulk endpoint. That also means the
 * "select all" checkbox means *all visible rows* — the honest promise, given
 * the list is paginated and filtered server-side.
 *
 * `canSelect` marks rows that cannot take part at all (your own user account,
 * for instance). Those rows render no checkbox and are excluded from both the
 * select-all toggle and its checked/indeterminate state.
 */
export default function useSelection(rows, { getId = (row) => row.id, canSelect } = {}) {
    const [selected, setSelected] = useState(() => new Set());

    // Held in refs so passing inline arrow functions from a component body
    // does not re-create every callback on each render.
    const getIdRef = useRef(getId);
    getIdRef.current = getId;
    const canSelectRef = useRef(canSelect);
    canSelectRef.current = canSelect;

    const selectableRows = useMemo(
        () => (rows || []).filter((row) => (canSelectRef.current ? canSelectRef.current(row) : true)),
        [rows]
    );
    const selectableIds = useMemo(() => selectableRows.map((row) => getIdRef.current(row)), [selectableRows]);

    const selectedIds = useMemo(() => selectableIds.filter((id) => selected.has(id)), [selectableIds, selected]);
    const selectedRows = useMemo(
        () => selectableRows.filter((row) => selected.has(getIdRef.current(row))),
        [selectableRows, selected]
    );

    const isSelected = useCallback((id) => selected.has(id), [selected]);

    const toggle = useCallback((id) => {
        setSelected((previous) => {
            const next = new Set(previous);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const clear = useCallback(() => setSelected(new Set()), []);

    const allSelected = selectableIds.length > 0 && selectedIds.length === selectableIds.length;
    const someSelected = selectedIds.length > 0 && !allSelected;

    const toggleAll = useCallback(() => {
        // One control, two meanings: tick everything visible, or — once
        // everything visible is ticked — untick it again.
        setSelected((previous) => {
            const next = new Set(previous);
            const everything = selectableIds.every((id) => next.has(id));
            selectableIds.forEach((id) => (everything ? next.delete(id) : next.add(id)));
            return next;
        });
    }, [selectableIds]);

    const canSelectRow = useCallback((row) => (canSelectRef.current ? canSelectRef.current(row) : true), []);

    return {
        selectedIds,
        selectedRows,
        selectedCount: selectedIds.length,
        selectableCount: selectableIds.length,
        isSelected,
        toggle,
        toggleAll,
        clear,
        allSelected,
        someSelected,
        canSelect: canSelectRow,
        getId: useCallback((row) => getIdRef.current(row), []),
    };
}
