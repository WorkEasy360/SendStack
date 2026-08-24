import { useEffect } from "react";
import { X } from "lucide-react";

const WIDTHS = {
    sm: "max-w-sm",
    md: "max-w-lg",
    lg: "max-w-xl",
    xl: "max-w-2xl",
};

/**
 * `width` accepts a size key (`sm`/`md`/`lg`/`xl`) or a raw Tailwind
 * max-width class, so existing callers passing `max-w-2xl` keep working.
 */
export default function Modal({ title, description, onClose, children, footer, width = "md" }) {
    useEffect(() => {
        const onKeyDown = (event) => {
            if (event.key === "Escape") onClose?.();
        };
        document.addEventListener("keydown", onKeyDown);
        // Stop the page behind the overlay from scrolling with the modal.
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => {
            document.removeEventListener("keydown", onKeyDown);
            document.body.style.overflow = previousOverflow;
        };
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-ink-900/50 backdrop-blur-[2px] p-0 sm:p-4 animate-fade-in"
            onClick={onClose}
            role="presentation"
        >
            <div
                role="dialog"
                aria-modal="true"
                aria-label={typeof title === "string" ? title : undefined}
                onClick={(event) => event.stopPropagation()}
                className={`w-full ${WIDTHS[width] || width} bg-white rounded-t-2xl sm:rounded-2xl shadow-overlay max-h-[92vh] sm:max-h-[88vh] flex flex-col animate-slide-up`}
            >
                <div className="flex items-start justify-between gap-4 px-5 sm:px-6 pt-5 pb-4 border-b border-ink-100">
                    <div className="min-w-0">
                        <h2 className="text-base font-bold text-ink-900 m-0">{title}</h2>
                        {description && <p className="text-sm text-ink-500 mt-1 mb-0">{description}</p>}
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close dialog"
                        className="shrink-0 -mr-1.5 -mt-1 w-8 h-8 inline-flex items-center justify-center rounded-lg text-ink-400 hover:bg-ink-100 hover:text-ink-700 cursor-pointer bg-transparent border-none transition-colors"
                    >
                        <X size={18} />
                    </button>
                </div>

                <div className="scroll-y flex-1 px-5 sm:px-6 py-5">{children}</div>

                {footer && (
                    <div className="px-5 sm:px-6 py-4 border-t border-ink-100 bg-ink-50 rounded-b-2xl">{footer}</div>
                )}
            </div>
        </div>
    );
}
