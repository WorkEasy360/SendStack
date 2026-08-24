import api, { apiErrorMessage } from "./api";

/**
 * Call one bulk endpoint and flatten every outcome into one shape.
 *
 * The API answers 200 when everything worked, 207 when some of the selection
 * was refused, and 400 when none of it was — and 207/400 both carry the same
 * per-item breakdown. Axios only rejects the 400, so this normalises all three
 * into `{ ok, succeeded, failed, errors, message }` and callers never have to
 * care which one they got.
 */
export async function runBulk(path, { action, ids, ...extra }, fallback = "This bulk action failed.") {
    try {
        const { data } = await api.post(path, { action, ids, ...extra });
        return {
            ok: true,
            succeeded: data.succeeded ?? ids.length,
            failed: data.failed ?? 0,
            errors: data.errors || [],
            message: data.detail || "",
        };
    } catch (error) {
        const data = error?.response?.data;
        if (data && typeof data === "object" && "succeeded" in data) {
            return {
                ok: false,
                succeeded: data.succeeded || 0,
                failed: data.failed || 0,
                errors: data.errors || [],
                message: data.detail || apiErrorMessage(error, fallback),
            };
        }
        return { ok: false, succeeded: 0, failed: ids.length, errors: [], message: apiErrorMessage(error, fallback) };
    }
}

/**
 * A readable line naming what was refused and why, for the page's error Alert.
 *
 * `nameFor(id)` turns an id back into something the user recognises; without
 * it the reasons still show, just without names.
 */
export function describeBulkFailures(result, nameFor) {
    if (!result.errors?.length) return result.ok ? "" : result.message;

    const listed = result.errors.slice(0, 3).map(({ id, detail }) => {
        const name = nameFor ? nameFor(id) : null;
        return name ? `${name}: ${detail}` : detail;
    });
    const remaining = result.errors.length - listed.length;
    return [listed.join(" "), remaining > 0 ? `(+${remaining} more)` : ""].filter(Boolean).join(" ");
}
