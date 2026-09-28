/**
 * One brand colour -> the ten-step accent ramp the design system expects.
 *
 * The app paints itself from `--color-brand-50` … `--color-brand-900`, but a
 * reseller will only ever give us one colour. Rather than ask for ten, each
 * step is that colour mixed toward white or black by a fixed amount, with the
 * chosen colour landing on 600 — the step used for primary buttons and active
 * nav, so what they picked is what they mostly see.
 *
 * The mix ratios are read off the default emerald ramp, so a custom brand
 * keeps the same light-to-dark rhythm the components were designed against
 * (a 50 pale enough for a badge background, a 700 dark enough to read on it).
 */

// step -> [mix toward white, mix toward black]
const STEPS = {
    50: [0.95, 0],
    100: [0.88, 0],
    200: [0.76, 0],
    300: [0.58, 0],
    400: [0.36, 0],
    500: [0.16, 0],
    600: [0, 0],
    700: [0, 0.12],
    800: [0, 0.26],
    900: [0, 0.38],
};

// The steps used as solid fills (buttons, progress bars, active nav accents),
// which read the same on a light or a dark page.
const FILL_STEPS = [300, 400, 500, 600];

function parseHex(hex) {
    const value = String(hex || "").trim().replace("#", "");
    const full =
        value.length === 3
            ? value
                  .split("")
                  .map((character) => character + character)
                  .join("")
            : value;
    if (!/^[0-9a-fA-F]{6}$/.test(full)) return null;
    return [
        parseInt(full.slice(0, 2), 16),
        parseInt(full.slice(2, 4), 16),
        parseInt(full.slice(4, 6), 16),
    ];
}

const toHex = (channel) => Math.round(channel).toString(16).padStart(2, "0");

function mix([r, g, b], towardWhite, towardBlack) {
    const target = towardWhite > 0 ? 255 : 0;
    const amount = towardWhite > 0 ? towardWhite : towardBlack;
    return `#${[r, g, b].map((channel) => toHex(channel + (target - channel) * amount)).join("")}`;
}

/** `{ "--color-brand-50": "#…", … }`, or null if the colour is unusable. */
export function brandRamp(hex) {
    const rgb = parseHex(hex);
    if (!rgb) return null;

    const ramp = {};
    for (const [step, [white, black]] of Object.entries(STEPS)) {
        ramp[`--color-brand-${step}`] = mix(rgb, white, black);
    }
    return ramp;
}

/**
 * Apply a ramp to the document, or clear it back to the stylesheet's own.
 *
 * Written onto the root element rather than into a stylesheet so that
 * removing the properties restores the built-in emerald with no reload — a
 * customer who clears their colour should see the default immediately.
 */
export function applyBrandColor(hex, dark = false) {
    const root = document.documentElement;
    const ramp = brandRamp(hex);

    for (const step of Object.keys(STEPS)) {
        const property = `--color-brand-${step}`;
        // In dark mode only the fill steps come from the reseller's colour.
        // The tints (50-200) and the text steps (700-900) are derived from
        // 500 by the stylesheet's dark block -- and an inline property would
        // beat that block, leaving pale badges on a dark page.
        const followsColour = !dark || FILL_STEPS.includes(Number(step));
        if (ramp && followsColour) root.style.setProperty(property, ramp[property]);
        else root.style.removeProperty(property);
    }
}
