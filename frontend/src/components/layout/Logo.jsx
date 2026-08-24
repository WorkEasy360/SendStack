import { useBranding } from "../../context/BrandingContext";

/**
 * The product mark: a reseller's uploaded logo when there is one, otherwise
 * two stacked send-arrows suggesting one message fanning out to many
 * recipients.
 *
 * The fallback is drawn in `currentColor` against the ink square, so it
 * re-tints with the brand ramp without anything here knowing the colour.
 */
export function LogoMark({ size = 32, className = "" }) {
    const { branding } = useBranding();

    if (branding.logo_url) {
        return (
            <img
                src={branding.logo_url}
                alt=""
                className={`rounded-lg object-contain shrink-0 ${className}`}
                style={{ width: size, height: size }}
                // A logo that 404s should leave the layout alone rather than
                // showing a broken-image glyph in the sidebar.
                onError={(event) => {
                    event.currentTarget.style.visibility = "hidden";
                }}
            />
        );
    }

    return (
        <span
            className={`inline-flex items-center justify-center rounded-lg bg-ink-900 text-brand-400 shrink-0 ${className}`}
            style={{ width: size, height: size }}
            aria-hidden="true"
        >
            <svg width={size * 0.6} height={size * 0.6} viewBox="0 0 24 24" fill="none">
                <path d="M2 5.5 21 12 2 18.5l3.2-6.5L2 5.5Z" fill="currentColor" />
                <path
                    d="M5.2 12h6.4"
                    stroke="#0f172a"
                    strokeWidth="1.75"
                    strokeLinecap="round"
                />
            </svg>
        </span>
    );
}

export default function Logo({ collapsed = false }) {
    const { branding } = useBranding();

    return (
        <span className="inline-flex items-center gap-2.5 min-w-0">
            <LogoMark />
            {!collapsed && (
                <span className="font-bold text-[15px] text-ink-900 tracking-tight truncate">
                    {branding.product_name}
                </span>
            )}
        </span>
    );
}
