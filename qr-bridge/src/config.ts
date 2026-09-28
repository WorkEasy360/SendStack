import { INTERACTIVE_FORMATS, type InteractiveFormat } from "./providers/base.js";

function required(name: string, value: string | undefined): string {
    if (!value) {
        throw new Error(`Missing required environment variable: ${name}`);
    }
    return value;
}

/** How long a generated QR stays scannable, and the range that is even meaningful. */
export const QR_TTL_DEFAULT_SECONDS = 120;
export const QR_TTL_MIN_SECONDS = 30;
export const QR_TTL_MAX_SECONDS = 3600;

/**
 * The QR session TTL, clamped to something a timer can actually hold.
 *
 * This value is multiplied by 1000 and used two ways: as a `setTimeout` delay,
 * and as the epoch-ms argument to `new Date(...)` for the `expires_at` the
 * backend is told. Both have hard ceilings, and the value this project shipped
 * as its documented default (99999999999999999999) blows through both:
 * setTimeout truncates anything over 2^31-1 ms to 1 and fires *immediately*
 * after printing a TimeoutOverflowWarning, while a Date past ±8.64e15 ms is
 * Invalid and serialising it throws `RangeError: Invalid time value`. So a
 * fresh deployment taking the documented default got a QR that either expired
 * at once or crashed the request that reported it.
 *
 * Anything unparseable or out of range is therefore corrected to the default
 * rather than honoured. That is deliberate: there is no deployment for which
 * "the QR is valid for three hundred billion years" is the intended setting,
 * and failing closed on a mistyped duration is worse than quietly using one
 * that works.
 */
export function resolveQrTtlSeconds(raw: string | undefined): number {
    const value = Number(raw);
    if (!Number.isFinite(value) || !Number.isInteger(value)) return QR_TTL_DEFAULT_SECONDS;
    if (value < QR_TTL_MIN_SECONDS || value > QR_TTL_MAX_SECONDS) return QR_TTL_DEFAULT_SECONDS;
    return value;
}

/**
 * The default wire shape for a button message.
 *
 * RE-MEASURED 2026-09-03 with a rig that reads the server's own ACK off the
 * websocket (`buttonLab.ts`), which is the instrument the earlier comparison
 * lacked. Two of its five conclusions were wrong, and the real reason is now
 * known rather than inferred.
 *
 * WHAT THE SERVER ACTUALLY DOES, from a QR-linked WhatsApp **Business**
 * account (creds platform `smba`, Baileys 6.7.24) to a real handset:
 *
 *   envelope                                    server ack   handset
 *   ------------------------------------------  -----------  ---------------------
 *   plain text                                  clean        arrives, readable
 *   interactiveMessage, undeclared (3 shapes)   clean        arrives, UNRENDERABLE
 *   interactiveMessage + biz native_flow        error 405    never arrives
 *     (name="quick_reply")
 *   interactiveMessage + biz native_flow        clean        arrives, UNRENDERABLE
 *     (name="mixed")
 *   listMessage, hand-relayed                   error 479    never arrives
 *
 * Two corrections to what this comment used to claim:
 *
 * 1. The interactive shapes are NOT "silently discarded in transit". They are
 *    delivered. WhatsApp Desktop shows them as "This message couldn't load.
 *    Open the message on your phone to view it", and the phone shows nothing
 *    at all -- so a test that only checked the phone read delivery-plus-no-
 *    renderer as non-arrival. The consumer client simply has no renderer for
 *    an `interactiveMessage` from a non-Cloud-API sender.
 *
 * 2. `buttons_legacy` and `template` never measured WhatsApp at all.
 *    Baileys 6.7.24 removed legacy button support: `generateWAMessageContent`
 *    branches on `'text' in message` and never reads `buttons`,
 *    `templateButtons` or `sections`, and unknown keys are ignored rather than
 *    rejected. Both formats therefore put a plain text message on the wire.
 *    "Arrived as plain text" was Baileys dropping the buttons locally, not
 *    WhatsApp flattening them.
 *
 * The limitation is server-side and account-type-bound, not our code and not a
 * Baileys bug: the payload survives protobuf round-trip intact, and the moment
 * the stanza honestly declares native-flow capability the server refuses it
 * (405). Every tappable envelope is now either refused or unrenderable.
 *
 * `buttons_legacy` remains the default, and is still the right one: because
 * Baileys drops the button structure before it ever reaches the server, the
 * body text always arrives readable. The three interactive shapes would
 * deliver a message the recipient cannot read, which is worse. Nothing on the
 * Django side sends interactive content here today --
 * `WhatsAppQRBridgeProvider.supports_interactive_buttons` is False, and that
 * flag is what keeps these shapes off the wire. Re-measure with
 * `qr-button-test.ts` before believing any claim that this has changed.
 */
export function resolveInteractiveFormat(raw: string | undefined): InteractiveFormat {
    return (INTERACTIVE_FORMATS as string[]).includes(raw ?? "")
        ? (raw as InteractiveFormat)
        : "buttons_legacy";
}

export const config = {
    port: Number(process.env.PORT || 4001),
    interactiveFormat: resolveInteractiveFormat(process.env.QR_BRIDGE_INTERACTIVE_FORMAT),
    // Must match Django's QR_PROVIDER_API_KEY / QR_PROVIDER_WEBHOOK_SECRET --
    // this bridge and the backend authenticate to each other with the same
    // two shared secrets (see backend/apps/whatsapp/providers/qr_bridge.py).
    apiKey: required("QR_PROVIDER_API_KEY", process.env.QR_PROVIDER_API_KEY),
    webhookSecret: required("QR_PROVIDER_WEBHOOK_SECRET", process.env.QR_PROVIDER_WEBHOOK_SECRET),
    dataDir: process.env.QR_BRIDGE_DATA_DIR || "./data",
    qrTtlSeconds: resolveQrTtlSeconds(process.env.WHATSAPP_QR_SESSION_TTL_SECONDS),
    logLevel: process.env.QR_BRIDGE_LOG_LEVEL || "info",
    // Kept separate from logLevel on purpose -- see the makeWASocket call in
    // providers/baileysProvider.ts. Below "debug" Baileys logs no credential
    // material; at or above it, it logs Signal keys.
    baileysLogLevel: process.env.QR_BRIDGE_BAILEYS_LOG_LEVEL || "warn",
};
