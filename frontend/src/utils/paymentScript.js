/**
 * Loading a payment provider's checkout script, on demand.
 *
 * Deliberately **not** a `<script>` tag in index.html. That would put a
 * third-party script on every page load of every deployment -- including the
 * many that never take a payment -- which is a privacy cost, a performance
 * cost and one more thing that can break the login page when somebody else's
 * CDN has a bad day. It is loaded when a customer actually presses "choose
 * plan", and not before.
 *
 * The URLs are pinned per provider rather than taken from the API: a script
 * source that the server could set would be an arbitrary-code-execution
 * primitive handed to anyone who could write to that field.
 */

const SCRIPTS = {
    razorpay: "https://checkout.razorpay.com/v1/checkout.js",
};

//: Provider -> the global its script defines, so a second call can tell that
//: the script is already there without depending on our own bookkeeping
//: surviving a hot reload.
const GLOBALS = {
    razorpay: "Razorpay",
};

const inFlight = {};

/**
 * Ensure the given provider's checkout script is loaded.
 *
 * Resolves `true` when the provider's global is available and `false` when it
 * is not -- never rejects. The caller has to handle "the script did not load"
 * anyway (an ad blocker, an offline moment, a CDN outage), and a rejected
 * promise would just be a second way to write the same branch.
 */
export function loadPaymentScript(provider) {
    const src = SCRIPTS[provider];
    const globalName = GLOBALS[provider];
    if (!src || !globalName) return Promise.resolve(false);

    if (typeof window[globalName] !== "undefined") return Promise.resolve(true);
    if (inFlight[provider]) return inFlight[provider];

    inFlight[provider] = new Promise((resolve) => {
        const existing = document.querySelector(`script[src="${src}"]`);
        const script = existing || document.createElement("script");

        const done = () => resolve(typeof window[globalName] !== "undefined");
        script.addEventListener("load", done, { once: true });
        script.addEventListener("error", () => resolve(false), { once: true });

        if (!existing) {
            script.src = src;
            script.async = true;
            document.head.appendChild(script);
        }
    }).finally(() => {
        // Cleared either way, so a failed load can be retried when the
        // customer presses the button again.
        delete inFlight[provider];
    });

    return inFlight[provider];
}
