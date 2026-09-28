/**
 * The model providers an agent can be pointed at, and what each one expects.
 *
 * Shared by the AI agent and the calling agent because getting this wrong is
 * the same mistake on both pages: a key from one vendor pasted into a field
 * configured for the other, which comes back only as a 401 hours later.
 *
 * Model lists are suggestions, not a whitelist — the API accepts any name, so
 * OpenRouter gets a free-text box with a datalist rather than a `select` it
 * would be out of date within the month.
 */

export const AI_PROVIDERS = [
    { value: "ANTHROPIC", label: "Anthropic — billed direct" },
    { value: "OPENAI", label: "OpenAI — billed direct" },
    { value: "OPENROUTER", label: "OpenRouter — one key, many vendors" },
    { value: "CUSTOM", label: "Custom — your own endpoint" },
];

/** Providers with no fixed model vocabulary or key format — free text throughout. */
export const FREEFORM_PROVIDERS = ["OPENROUTER", "CUSTOM"];

export const KEY_PLACEHOLDER = {
    ANTHROPIC: "sk-ant-…",
    OPENAI: "sk-…",
    OPENROUTER: "sk-or-v1-…",
    CUSTOM: "Bearer token or API key…",
};

export const KEY_HINT = {
    ANTHROPIC: "Your own Anthropic key, from console.anthropic.com.",
    // The one provider the server can also supply from its own environment,
    // so leaving this blank is a valid, documented choice rather than an
    // unfinished form.
    OPENAI: "Your own OpenAI key, from platform.openai.com. Leave blank to use the server's OPENAI_API_KEY.",
    OPENROUTER: "Your own OpenRouter key, from openrouter.ai/settings/keys.",
    CUSTOM: "The API key your endpoint expects, sent as a Bearer token.",
};

/** Suggested models per provider, best first. */
export const MODEL_SUGGESTIONS = {
    ANTHROPIC: [
        { value: "claude-opus-5", label: "Claude Opus 5 — most capable" },
        { value: "claude-sonnet-5", label: "Claude Sonnet 5 — balanced" },
        { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5 — fastest and cheapest" },
    ],
    OPENAI: [
        { value: "gpt-4o-mini", label: "gpt-4o-mini — cheap and quick" },
        { value: "gpt-4o", label: "gpt-4o — balanced" },
        { value: "gpt-4.1-mini", label: "gpt-4.1-mini — cheap, longer context" },
        { value: "o4-mini", label: "o4-mini — reasoning, slower" },
    ],
    OPENROUTER: [
        { value: "anthropic/claude-sonnet-4.5", label: "anthropic/claude-sonnet-4.5 — balanced" },
        { value: "openai/gpt-4o-mini", label: "openai/gpt-4o-mini — cheap and quick" },
        { value: "google/gemini-2.0-flash-001", label: "google/gemini-2.0-flash-001 — cheap and quick" },
        { value: "meta-llama/llama-3.3-70b-instruct", label: "meta-llama/llama-3.3-70b-instruct — open weights" },
    ],
    // No suggestions: a custom endpoint's model names are whatever that
    // endpoint calls them, which this codebase has no way to guess at.
    CUSTOM: [],
};

/**
 * Providers whose model the server can supply from its own environment.
 *
 * Switching to one of these leaves the model box blank rather than stamping a
 * suggestion into it, because a stored model always beats the environment —
 * so pre-filling here would silently override the deployment's OPENAI_MODEL
 * for every tenant who so much as opened the provider dropdown.
 */
export const ENV_MODEL_PROVIDERS = ["OPENAI"];

/** The model to put in the box when someone switches to `provider`. */
export function modelForProvider(provider) {
    if (ENV_MODEL_PROVIDERS.includes(provider)) return "";
    return MODEL_SUGGESTIONS[provider]?.[0]?.value || "";
}

/**
 * The options to offer for a model, with whatever is already stored kept.
 *
 * Without that last part a tenant on a model this list has since dropped
 * would open the page, see a `select` snapped to something else, and save a
 * model change they never made.
 */
export function modelOptions(provider, current) {
    const suggestions = MODEL_SUGGESTIONS[provider] || [];
    const stored = (current || "").trim();
    if (!stored || suggestions.some((option) => option.value === stored)) return suggestions;
    return [...suggestions, { value: stored, label: stored }];
}

/**
 * Why this model name cannot work with this provider, or "" if it can.
 *
 * Mirrors `model_name_problem` on the server so the message lands under the
 * field as it is typed instead of on save. The server still checks — this is
 * the courtesy, not the guard.
 */
export function modelNameProblem(provider, model) {
    const name = (model || "").trim();
    if (!name) return "";
    if (provider === "OPENROUTER" && !name.includes("/")) {
        return "OpenRouter model names are written vendor/model, like anthropic/claude-sonnet-4.5.";
    }
    if (provider === "ANTHROPIC" && name.includes("/")) {
        return "Anthropic model names have no vendor prefix, like claude-sonnet-5. Switch to OpenRouter to route this one.";
    }
    if (provider === "OPENAI" && name.includes("/")) {
        return "OpenAI model names have no vendor prefix, like gpt-4o-mini. Switch to OpenRouter to route this one.";
    }
    return "";
}

export const BASE_URL_PLACEHOLDER = "https://api.groq.com/openai/v1";

/**
 * Why this base URL cannot work, or "" if it can.
 *
 * Mirrors `base_url_problem` on the server. Only Custom has one to check —
 * Anthropic and OpenRouter each have a fixed endpoint.
 */
export function baseUrlProblem(provider, baseUrl) {
    if (provider === "CUSTOM" && !(baseUrl || "").trim()) {
        return "A custom provider needs the OpenAI-compatible endpoint's base URL, like https://api.groq.com/openai/v1.";
    }
    return "";
}
