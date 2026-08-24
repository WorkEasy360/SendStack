import { useState } from "react";
import { TriangleAlert } from "lucide-react";
import Modal from "./Modal";
import Button from "./Button";
import { Field, Input } from "./Field";

/**
 * Replacement for window.confirm / window.prompt.
 *
 * Set `promptLabel` to collect a required free-text reason (used by the
 * "Login as" flow, which must record why an impersonation happened).
 * `onConfirm` may return a promise — the button stays in its loading state
 * until it settles.
 */
export default function ConfirmDialog({
    title,
    message,
    confirmLabel = "Confirm",
    cancelLabel = "Cancel",
    tone = "danger",
    promptLabel,
    promptHint,
    promptDefault = "",
    onConfirm,
    onClose,
}) {
    const [value, setValue] = useState(promptDefault);
    const [busy, setBusy] = useState(false);

    const confirm = async (event) => {
        event.preventDefault();
        if (promptLabel && !value.trim()) return;
        setBusy(true);
        try {
            await onConfirm(promptLabel ? value.trim() : undefined);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal title={title} onClose={busy ? undefined : onClose} width="sm">
            <form onSubmit={confirm}>
                <div className="flex items-start gap-3 mb-5">
                    {tone === "danger" && (
                        <span className="shrink-0 w-9 h-9 rounded-lg bg-red-50 text-red-600 flex items-center justify-center">
                            <TriangleAlert size={18} aria-hidden="true" />
                        </span>
                    )}
                    <p className="text-sm text-ink-600 m-0">{message}</p>
                </div>

                {promptLabel && (
                    <Field label={promptLabel} hint={promptHint} required>
                        <Input value={value} onChange={(event) => setValue(event.target.value)} autoFocus required />
                    </Field>
                )}

                <div className="flex gap-2 justify-end">
                    <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
                        {cancelLabel}
                    </Button>
                    <Button type="submit" variant={tone === "danger" ? "danger" : "primary"} loading={busy}>
                        {confirmLabel}
                    </Button>
                </div>
            </form>
        </Modal>
    );
}
