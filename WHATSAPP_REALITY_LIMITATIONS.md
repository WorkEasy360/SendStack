# WhatsApp Reality and Non-Negotiable Limitations

## Bottom line

The QR/session product is an **unofficial automation layer over WhatsApp Web's linked-device protocol**. It is not Meta's WhatsApp Business Platform, is not approved merely because the user scanned a QR code, and cannot truthfully promise protocol stability, delivery, account safety, template status, throughput, feature parity, or uninterrupted access.

This does not mean the software has no useful market. It means the commercial promise must match reality: a best-effort linked-device tool for customers who knowingly accept interruption and account-enforcement risk, with conservative use controls and a supported migration/export path. “Official,” “Meta partner,” “ban-proof,” “unlimited,” “guaranteed delivery,” “full WhatsApp API,” and “complete WhatsApp clone” are unsafe claims unless separately and factually earned.

## Three different products must not be conflated

| Product | Who operates it | Connection model | Intended use | What this repository does |
|---|---|---|---|---|
| WhatsApp Messenger | Meta mobile/desktop apps | Consumer account and linked devices | Personal communications | Connects indirectly through an unofficial Web protocol session |
| WhatsApp Business App | Meta mobile/desktop apps | Business app account and linked devices | Small-business conversations and app tools | Connects indirectly through the same class of linked-device session; app-only features are not automatically exposed |
| WhatsApp Business Platform / Cloud API | Meta's supported business API | Business portfolio/WABA, registered number, access tokens, webhooks | Approved business messaging under platform policies | Optional provider/benchmark code exists, but it is not the requested core architecture |

A QR-connected Business App number does not turn Baileys traffic into Cloud API traffic. A text labelled “template” in this product is ordinary linked-device content; it is not an approved Meta template and has no official template quality/category lifecycle.

Meta's official Cloud API documentation is the correct benchmark for supported message objects, webhooks and status semantics, not proof that the QR bridge supports them. See [Meta's WhatsApp Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api), [message requests](https://www.postman.com/meta/whatsapp-business-platform/folder/o48mro7/messages), [inbound message objects](https://www.postman.com/meta/whatsapp-business-platform/folder/1dtuocp/messages-object), and [status notifications](https://www.postman.com/meta/whatsapp-business-platform/request/rgtfq23/message-status-update-notifications).

## Policy and account-enforcement reality

WhatsApp's current terms prohibit or restrict, among other conduct, impermissible bulk/automated messaging, unauthorized non-personal use, reverse engineering, and services that emulate/resell WhatsApp in unauthorized ways. WhatsApp's messaging guidelines describe automated and human review and possible temporary or permanent suspension. Relevant primary sources:

- [WhatsApp Terms of Service](https://www.whatsapp.com/legal/terms-of-service)
- [WhatsApp Messaging Guidelines](https://www.whatsapp.com/legal/messaging-guidelines)
- [WhatsApp guidance on unauthorized automated or bulk messaging](https://faq.whatsapp.com/5957850900902049)
- [WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/)

Baileys itself says it is unofficial, is not affiliated with WhatsApp, and discourages spam/bulk or automated misuse. See the [Baileys repository notice](https://github.com/WhiskeySockets/Baileys).

Consequences that product, sales and support must plan for:

- Meta/WhatsApp can change protocol behavior without coordinating with this product.
- A linked session can be logged out, replaced, rate-limited, challenged or banned.
- A mobile app update, multi-device key change or Baileys incompatibility can impair decryption while the socket remains open.
- Consent, delays and rate limits reduce abuse but do not create authorization under WhatsApp's terms.
- The customer owning the number does not eliminate platform-contract or recipient-consent obligations.
- No engineering technique can guarantee that an account will not be restricted.

Do not build or advertise evasion, fingerprint spoofing, ban circumvention, CAPTCHA bypass, stealth automation, or techniques intended to defeat WhatsApp controls. The safe response to enforcement is pause, disclose, preserve evidence, follow official remediation, and offer an official-provider path where appropriate.

## Linked-device/session limitations

### Session lifecycle

- QR pairing requires a person with the phone and can expire before scan.
- The phone/account may remove the linked device; another connection or user action can replace/log it out.
- Credentials are high-value cryptographic material. Theft can grant messaging access; corruption/loss forces re-pairing.
- “Connected” at the WebSocket level does not prove messages decrypt, callbacks reach Django, or a recipient receives a send. The audit observed exactly this distinction.
- The current bridge uses one local multi-file auth directory and one process. It has no safe active/active ownership, so horizontal scaling or automatic failover is not available today.
- Reconnect loops must distinguish transient disconnect, logged-out, replaced, bad session, auth-write failure and protocol incompatibility. Blind reconnecting cannot heal every state.

Baileys' own session documentation says its multi-file helper is not the production storage pattern; credentials should be stored safely and transactionally. See [Baileys session management](https://github.com/WhiskeySockets/docs/blob/main/authentication/session-management.mdx) and [Baileys security](https://github.com/WhiskeySockets/Baileys/security).

### Message history and synchronization

The repository sets `syncFullHistory: false` and deliberately ignores history-set events. It processes new direct-message upserts. Therefore:

- This is not a WhatsApp backup, migration, archive or legal-hold system.
- Existing phone/desktop history will not reliably appear after linking.
- Messages received during outages may not be backfilled.
- Edits/deletes/reactions and cross-device reconciliation are not implemented end-to-end.
- Local database history may disagree with what the phone shows.

Product copy should say “messages observed by this connection while healthy,” not “your complete WhatsApp history.”

### Direct chats versus groups and broadcast constructs

The bridge deliberately drops group and broadcast JIDs from the inbox. That is safer than misattributing a group participant to a private chat, but it means:

- No group inbox, group sending, participant/admin management, join links, mentions, quoted group replies, communities or group events.
- No WhatsApp broadcast-list parity. Campaigns are the SaaS's own repeated direct sends, with different behavior and risk.
- Channels/status/stories are not supported.

### Message-type reality

Reliable in the current design is narrower than “Baileys has a type definition for it.” A feature is commercially supportable only when it is mapped through UI/API/model/task/bridge, rendered on current Android and iOS clients for Messenger and Business App, survives reconnect/retry, records provider IDs/status, appears correctly inbound, and has monitoring/tests.

- Plain direct text is the closest to complete, but current decrypt/callback failures still prevent a reliability claim.
- Common outbound image/video/document paths exist, subject to URL security and provider limits. Inbound content is not actually retained for agents.
- Poll payloads exist, but cross-client rendering/vote lifecycle is not fully proven.
- QR buttons and lists are disabled because observed native-flow rendering is unreliable. Offline payload tests do not prove handset behavior.
- Audio/sticker support in a low-level parser is not end-to-end voice note/sticker support.
- Reactions, edits, deletes, forwards, quoted replies, locations, contacts/vCards, live location, presence/typing/last seen, calls, ephemeral/view-once, product/catalog/cart/order/payment, status/stories, channels and communities are missing or unsupported.
- Cloud API templates, authentication/OTP templates, flows and official quality/tier controls do not exist in QR mode.

## Delivery, receipt, and exactly-once limitations

All network messaging has an ambiguity window: the remote service may accept a message while the local caller times out or dies before recording the acknowledgement. Exactly-once external delivery cannot be promised without provider-supported idempotency/reconciliation, and even then the claim must be carefully bounded.

The present repository makes this worse:

- Attempt-scoped operation IDs change on retry.
- Ambiguous results are requeued rather than quarantined for reconciliation.
- Multipart QR sends use suffixed operation IDs while recovery queries only the base ID.
- Bridge dedup lookup/send/record is not atomic and its persistence can lag the send.
- Callback delivery is at-most-once and can be discarded.

After repair, customer-facing statuses should remain explicit:

| Status | Honest meaning |
|---|---|
| Queued | Persisted locally; not yet attempted |
| Sending | Claimed locally; outcome may become ambiguous if the worker dies |
| Accepted | Provider/bridge returned a message ID; not proof of handset delivery |
| Delivered | A matching provider receipt was observed |
| Read | A matching read receipt was observed, where the recipient/client exposes it |
| Failed | A terminal failure is known |
| Ambiguous / reconciliation required | A send may have occurred; automatic retry is unsafe |

Do not relabel `accepted` as delivered. Do not show 100% success from an HTTP 200 alone. Receipts can be delayed, missing, reordered or unsupported by recipient privacy/client behavior.

## Normal WhatsApp versus Business App expectations

Features visible in the Business App—profile, hours, greeting/away messages, labels, catalog, carts, quick replies, short links and business discovery—are app-managed features. A Web protocol library may expose some underlying messages or metadata, but this repository does not implement authoritative CRUD/synchronization for most of them. The SaaS's own tags, canned replies, chatbot, schedule and catalog-like content should be described as SendStack features, not as synchronized WhatsApp Business App objects.

Similarly, consumer features such as calls, status, disappearing messages, live location and communities are not reasonable campaign-SaaS parity requirements. They should be explicitly marked “not supported / not in product scope,” not left ambiguous in sales material.

## Cloud API benchmark—not a forced redesign

The audit does **not** recommend replacing the user's chosen QR core as the only way forward. It uses Cloud API to establish the reliability/support contrast:

| Dimension | QR/Baileys core | Cloud API benchmark |
|---|---|---|
| Vendor support | Unofficial/reverse-engineered | Official Meta interface |
| Account onboarding | User scans linked-device QR | Business onboarding/number registration/token setup |
| Proactive business messaging | App-like sends technically possible; policy risk remains | Template/policy/conversation rules apply explicitly |
| Webhooks | Product must build durable bridge delivery | Meta supplies supported webhook contract; product still must ingest durably |
| Idempotency/ambiguity | Product owns operation ledger and reconciliation | Product still needs idempotency/reconciliation, with documented provider semantics |
| Scaling | Stateful sockets and session placement | Stateless HTTP calls are easier to distribute |
| Feature access | Web-client surface can change | Published supported message objects/endpoints |
| Ban/breakage risk | Material and not eliminable | Policy enforcement still exists, but integration itself is supported |

Offering both modes can be commercially sensible, but product entitlements, UI labels, claims, limits, status semantics and support policies must clearly identify which mode a number uses.

## Capacity and reliability promises that cannot be made yet

There is no measured basis for:

- 100+ simultaneously connected QR sessions on the current single bridge;
- any particular sustained messages/minute while respecting recipient/account safety;
- active/active bridge availability or zero-rescan failover;
- complete inbound capture during deploy/restart/network loss;
- 1,000, 10,000 or 100,000 tenants;
- exact delivery/read rates;
- full history synchronization;
- all Android/iOS/Desktop client feature parity.

After P0/P1 fixes, promise only the capacity demonstrated by a dated test matrix, on the same topology and limits used in production. Publish conservative account/session caps and a degradation policy.

## Required customer disclosures

Before any QR beta, terms and onboarding should plainly disclose:

1. The integration uses an unofficial linked-device protocol and is not endorsed by Meta/WhatsApp.
2. WhatsApp can disconnect, restrict or ban accounts, and the service cannot prevent that.
3. Customers must have recipient consent and comply with applicable law and WhatsApp terms.
4. Bulk/automated behavior may violate WhatsApp rules even when technically possible.
5. Feature availability can change after WhatsApp client/protocol updates.
6. “Connected” does not guarantee delivery; status meanings and ambiguous outcomes are defined.
7. Which message/media/group/history features are not supported.
8. Session loss may require a new QR scan; support and data-recovery boundaries are explicit.
9. Data processed by the platform, retention, AI subprocessors, and deletion/suppression handling.
10. A suspension/refund/termination policy and an official-provider migration/export route.

Disclosure does not cure unsafe engineering or prohibited conduct. It is an additional minimum after the technical release gates pass.

## Commercially safe product boundary

The defensible QR beta is direct, consent-based, low-volume customer communication with conservative rate/frequency limits; clear operator review; no group/broadcast/status automation; no evasion features; visible ambiguous/degraded states; durable suppression; and rapid pause/logout controls. High-volume cold outreach, scraped contacts, guaranteed scale, hidden automation or ban-circumvention should not be offered.

The complete feature-by-feature comparison is in [WHATSAPP_FEATURE_MATRIX.md](WHATSAPP_FEATURE_MATRIX.md).

