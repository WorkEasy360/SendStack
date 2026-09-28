/**
 * Personalization variables on the client.
 *
 * Mirrors `templates_app/services.py` closely enough for a live preview to be
 * trustworthy, and deliberately no further. The **server is the authority**:
 * what a recipient actually receives is rendered there, at send time, against
 * that contact's real data. This exists so the composer can redraw on every
 * keystroke without a round trip per character.
 *
 * Both syntaxes are supported for the same reason the backend supports both:
 * every template ever saved here uses `[Name]`, and everybody arriving from
 * another WhatsApp tool types `{{name}}`.
 */

export const VARIABLE_RE = /\[(\w+)\]|\{\{\s*(\w+)\s*\}\}/g;

/** The variables used in a piece of text, in the order they first appear. */
export function detectVariables(...texts) {
    const found = [];
    for (const text of texts) {
        for (const match of String(text ?? "").matchAll(VARIABLE_RE)) {
            const name = match[1] || match[2];
            if (name && !found.includes(name)) found.push(name);
        }
    }
    return found;
}

/**
 * Sample data for a preview, when no real contact has been chosen.
 *
 * Recognisable rather than realistic: "Aman" and "ORD123" are obviously
 * placeholders, so nobody mistakes a preview for a message that has been sent.
 */
export const SAMPLE_CONTACT = {
    name: "Aman Gupta",
    first_name: "Aman",
    last_name: "Gupta",
    phone: "+919876543210",
    number: "+919876543210",
    email: "aman@example.com",
    company: "Gupta Traders",
    country: "IN",
    order_id: "ORD123",
    amount: "2,500",
    city: "Mumbai",
};

function computed() {
    const now = new Date();
    return {
        date: now.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }),
        time: now.toLocaleTimeString("en-GB", { hour: "numeric", minute: "2-digit", hour12: true }),
    };
}

/**
 * Substitute every variable in one string.
 *
 * Same precedence as the server: the contact's data, then the template's own
 * fallback for that variable, then empty. Rendering to empty rather than
 * leaving the placeholder matches what will actually be sent — a customer
 * receiving "Hello [Name]" is worse than one receiving "Hello", and seeing
 * that gap in the preview is how an author notices the variable has no data
 * behind it.
 */
export function renderText(text, context = {}, fallbacks = {}) {
    const merged = { ...computed(), ...context };
    const lowered = {};
    for (const [key, value] of Object.entries(merged)) lowered[String(key).toLowerCase()] = value;

    return String(text ?? "").replace(VARIABLE_RE, (_, bracketed, braced) => {
        const name = bracketed || braced || "";
        const key = name.toLowerCase();
        if (lowered[key] !== undefined && lowered[key] !== null && lowered[key] !== "") {
            return String(lowered[key]);
        }
        if (fallbacks?.[name] !== undefined) return String(fallbacks[name]);
        for (const [fallbackName, value] of Object.entries(fallbacks || {})) {
            if (String(fallbackName).toLowerCase() === key) return String(value);
        }
        return "";
    });
}

/** The same substitution across a list's header, rows and footer. */
export function renderList(content, context, fallbacks) {
    if (!content?.sections) return content;
    return {
        ...content,
        header: renderText(content.header, context, fallbacks),
        footer: renderText(content.footer, context, fallbacks),
        button: renderText(content.button, context, fallbacks),
        sections: content.sections.map((section) => ({
            ...section,
            title: renderText(section.title, context, fallbacks),
            rows: (section.rows || []).map((row) => ({
                ...row,
                title: renderText(row.title, context, fallbacks),
                description: renderText(row.description, context, fallbacks),
            })),
        })),
    };
}

/** The same substitution across a poll's question and options. */
export function renderPoll(content, context, fallbacks) {
    if (!content?.question && !content?.options) return content;
    return {
        ...content,
        question: renderText(content.question, context, fallbacks),
        options: (content.options || []).map((option) => renderText(option, context, fallbacks)),
    };
}

/** Button labels only — ids and urls are never personalized. */
export function renderButtons(buttons, context, fallbacks) {
    return (buttons || []).map((button) => ({
        ...button,
        title: renderText(button.title, context, fallbacks),
    }));
}

/** A contact record as a render context, matching `build_render_context`. */
export function contextFromContact(contact) {
    if (!contact) return SAMPLE_CONTACT;
    const parts = (contact.name || "").split(/\s+/).filter(Boolean);
    return {
        name: contact.name || "",
        first_name: parts[0] || "",
        last_name: parts.slice(1).join(" "),
        phone: contact.phone || "",
        number: contact.phone || "",
        email: contact.email || "",
        company: contact.company || "",
        country: contact.country || "",
        ...(contact.custom_fields || {}),
    };
}
