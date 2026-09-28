# WhatsApp feature parity matrix

Audit date: 2026-09-06  
Primary product under review: normal WhatsApp and WhatsApp Business App accounts linked by QR/session  
Benchmark only: Meta's official WhatsApp Business Platform / Cloud API

## Bottom line

The repository has a substantial SaaS application around a narrow WhatsApp Web transport. Direct text, outbound image/video/document, QR pairing, polls, campaigns, an inbox, automations, AI, public APIs, reporting, billing, and reseller controls all have real implementations. It is not close to full WhatsApp or WhatsApp Business App parity. Groups, calls, status, communities, channels, most chat-management actions, history sync, quoted replies, reactions, locations, contacts, voice notes, stickers, catalog/commerce, and reliable inbound media are absent.

More importantly, “the bridge accepted a payload” is not treated as feature parity. Native buttons are disabled because handset tests did not show them reliably, inbound Signal decryption was failing heavily during the audit while the session still reported `CONNECTED`, bridge webhooks are best-effort, and outbound operation deduplication has race/crash gaps. Those defects make several nominally implemented rows operationally partial or broken.

The QR/session route is also not an approved substitute for the official business platform. WhatsApp's current Terms prohibit impermissible bulk/auto messaging, unauthorized non-personal use, reverse engineering, unauthorized resale, and unauthorized equivalent APIs. The Baileys project calls itself unofficial/not affiliated and discourages spam. Therefore the primary transport can be technically possible without being commercially safe. See [WhatsApp Terms](https://www.whatsapp.com/legal/terms-of-service), [WhatsApp Messaging Guidelines](https://www.whatsapp.com/legal/messaging-guidelines), [WhatsApp's unauthorized bulk/automation guidance](https://faq.whatsapp.com/5957850900902049), [WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/), and [Baileys](https://github.com/WhiskeySockets/Baileys).

## How to read the matrix

- **Normal app expectation** and **Business App expectation** describe what a person using the first-party mobile/desktop apps reasonably expects. They do not imply a right to automate those functions.
- **Cloud API benchmark** describes the official integration surface at a product level; Meta's [Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api) is the primary benchmark.
- **Repo status** is one of `Implemented`, `Partial`, `Missing`, `Broken`, or `Not applicable`. `Implemented` means a coherent code path exists, not that QR automation is approved by WhatsApp.
- **QR verdict** is three independent judgments: **T** = technically present/possible, **R** = reliable enough to promise, **C** = commercially safe to sell as an approved WhatsApp integration. `Y`, `P`, `N`, and `N/A` mean yes, partial/conditional, no, and not applicable.
- **Priority** is remediation urgency: `P0` blocks taking money, `P1` blocks a production promise, `P2` is a material parity gap, `P3` is an important enhancement, and `P4` is optional.

The commercial verdict assumes the product is sold as a multi-customer WhatsApp automation SaaS without separate written authorization from WhatsApp. If the operator has such authorization, it must be documented and the `C` judgments re-reviewed by counsel; consent controls and gradual sending do not create authorization by themselves.

## 1. Account linking, identity, and session lifecycle

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Link a normal WhatsApp number by QR | Linked-device QR | Linked-device QR | Not the normal Cloud onboarding model | Implemented | Y / P / N | Django owns pairing state and calls the bridge; bridge emits QR-ready/connected states (`backend/apps/whatsapp/connections/manager.py`, `qr-bridge/src/providers/baileysProvider.ts:142-242`). Technically works, but it is an unofficial automated client. | P0 policy |
| Link a WhatsApp Business App number by QR | N/A | Linked-device QR | Coexistence/onboarding is an official, separately governed flow | Implemented | Y / P / N | Account model distinguishes personal/business and QR/Cloud methods (`backend/apps/whatsapp/models.py:23-118`). A QR link does not become an approved Business Platform integration merely because the phone runs Business App. | P0 policy |
| QR refresh and expiry | Refreshable QR | Refreshable QR | N/A | Implemented | Y / P / N | Bridge creates, expires, and refreshes QR snapshots (`qr-bridge/src/providers/baileysProvider.ts:478-529`); expiry state is represented in Django. Real-world longevity remains client-controlled. | P1 |
| Pairing state machine | Linking, linked, logged out | Same | Credential registration/status | Implemented | Y / P / N | `CONNECTING`, `QR_READY`, `CONNECTED`, `ERROR`, `EXPIRED`, `DISCONNECTED` exist at both layers (`qr-bridge/src/providers/base.ts:1-24`; `backend/apps/whatsapp/models.py:239-270`). | P1 |
| Explicit disconnect/logout | Remove linked device | Remove linked device | Deregister/delete credentials as applicable | Implemented | Y / P / N | Django calls bridge `DELETE /sessions/{id}`; bridge handles logout and removes session state. | P1 |
| Automatic reconnect | First-party app recovers | First-party app recovers | Managed by Meta | Implemented | Y / P / N | Generation guards, backoff, socket replacement, logged-out and replaced-connection branches exist (`baileysProvider.ts:349-421`). It cannot cure protocol/key corruption. | P1 |
| Restore after bridge restart | Linked device normally remains linked | Same | Managed service | Partial | Y / N / N | Auth persists through `useMultiFileAuthState` (`qr-bridge/src/authState.ts:20-47`), but the upstream helper is explicitly not production-grade, the store is local JSON, and active socket state is memory-only. | P0 |
| Session credentials encrypted at rest | Device-managed secure storage | Device-managed secure storage | Managed by Meta/operator secrets | Missing | Y / N / N | QR auth JSON resides on a Docker volume with directory permissions but no application-layer encryption or KMS/envelope design. Treat it like a private key. | P0 |
| Durable/HA session ownership | First-party multi-device service | Same | Managed/scalable API | Missing | P / N / N | One bridge process owns in-memory sockets and a local session volume; no lease/placement protocol or shared production auth store supports horizontal replicas. | P1 |
| Multiple WhatsApp numbers per workspace | Multiple accounts in app, separately | Multiple business numbers, separately | Multiple phone-number resources | Implemented | Y / P / N | Tenant can create multiple `WhatsAppAccount` rows and campaigns can select/rotate them. Single-node bridge capacity/failure domain remains unproven. | P1 |
| One active socket per session | First-party invariant | First-party invariant | Managed | Implemented | Y / P / N | Bridge has generation checks and removes superseded listeners (`baileysProvider.ts:376-421`). No cross-process owner lock exists. | P1 |
| Phone/device conflict handling | User sees linked-device changes | Same | N/A | Partial | Y / P / N | `connectionReplaced` and `loggedOut` paths exist, but best-effort webhooks can lose the state transition. | P1 |
| Live connection truth | Accurate online/link health | Accurate online/link health | API/webhook health and errors | Broken | Y / N / N | Audit runtime showed heavy `Bad MAC`/decrypt failures while bridge and Django reported `CONNECTED`; Django may short-circuit on stored connected state. The exposed inbound counters do not count errors that occur before `messages.upsert`. | P0 |
| Inbound health telemetry | App visibly receives messages | Same | Webhook monitoring | Broken | P / N / N | Bridge exposes forwarded/dropped/undecryptable counters (`baileysProvider.ts:655-745`), but Django's provider result does not surface them and pre-upsert crypto failures bypass them. | P0 |
| Session history synchronization | Full recent chat history | Full recent business history | Webhooks are forward-going; official history scope is limited/product-specific | Missing | N / N / N | Bridge sets `syncFullHistory: false` and deliberately ignores `messaging-history.set` (`baileysProvider.ts:321,376-388`). | P2 |
| Linked-device limitation disclosure | User can inspect first-party UI | Same | Official docs/policies | Partial | Y / N / N | Some UI/provider capability disclosure exists, but the overall README still describes an official-API-only product while a Baileys bridge is shipped. Customers need an unavoidable risk/limitations acknowledgement. | P0 |

## 2. One-to-one outbound messaging

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Plain text | Native | Native | Supported | Implemented | Y / P / N | End-to-end API, message queue, provider, and bridge paths exist (`base.py:249-263`; `baileysProvider.ts:770-850`). Reliability is downgraded by P0 duplicate/recovery gaps. | P0 reliability |
| Unicode, emoji, and multiline text | Native | Native | Supported | Implemented | Y / P / N | Strings pass through without an artificial ASCII transform; no full handset matrix was recorded. | P2 test |
| WhatsApp text formatting syntax | Native | Native | Supported in message body | Partial | Y / P / N | Formatting markers can pass through, but there is no first-class composer/preview validation for client rendering. | P3 |
| Link rendering | Native | Native | Supported | Implemented | Y / P / N | URLs are sent as body text; campaign tracking can rewrite links. | P2 |
| Link preview control | Native previews and manual control vary | Native | Supported subject to message/API rules | Missing | P / N / N | No provider-level preview flag or metadata control is exposed. | P3 |
| Image send | Native | Native | Supported | Implemented | Y / P / N | IMAGE is represented in templates/campaigns/messages and maps to Baileys `{image: {url}}` (`baileysProvider.ts:1109-1114`). URL fetch security is broken; see SSRF row. | P0 security |
| Video send | Native | Native | Supported | Implemented | Y / P / N | VIDEO maps to a URL-backed Baileys video message (`baileysProvider.ts:1115-1116`). Codec/size/handset coverage is not proven. | P1 |
| Document send | Native | Native | Supported | Implemented | Y / P / N | DOCUMENT and filename/caption flow through (`baileysProvider.ts:1117-1122`). | P1 |
| Audio file send | Native | Native | Supported | Missing | P / N / N | Lower bridge type vocabulary mentions audio, but Django message/template/campaign enums expose only TEXT/IMAGE/VIDEO/DOCUMENT. | P2 |
| Voice-note/PTT send | Native | Native | Supported as audio with voice semantics where applicable | Missing | P / N / N | No PTT/voice-note type, recorder, waveform, or provider method. | P2 |
| Sticker send | Native | Native | Supported within official format constraints | Missing | P / N / N | Bridge can construct a sticker only at its low layer, but no product model/API/composer exposes it. | P2 |
| Animated GIF | Native | Native | Usually video/GIF-compatible media constraints | Missing | P / N / N | No explicit GIF type, validation, conversion, or rendering test. | P3 |
| Caption on media | Native | Native | Supported for applicable types | Implemented | Y / P / N | Caption fields flow through models and media builder. Multi-attachment semantics differ from an app album. | P2 |
| Multiple attachments | Album/multi-select experience | Same | Normally separate media messages or product-specific constraints | Partial | Y / P / N | `media_items` exists and the sender emits several messages sequentially. It is not a native app album and partial failure can split the set. | P2 |
| Send a contact card | Native | Native | Contact messages supported | Missing | P / N / N | No CONTACT message model or provider operation. | P2 |
| Send current/static location | Native | Native | Location messages supported | Missing | P / N / N | Inbox can describe official inbound locations, but no outbound location composer/provider method exists. | P2 |
| Live location | Native | Native | Not equivalent to normal app live sharing | Missing | N / N / N | No live-location session/state machinery. | P3 |
| Reply quoting a message | Native | Native | Reply context supported | Missing | P / N / N | No quoted message/context ID in the canonical provider send contract. Inbox “reply” means sending into a conversation, not quoting a bubble. | P2 |
| Forward message | Native | Native | Can resend content, not first-party forward semantics | Missing | P / N / N | No forwarded flag/context or UI action. | P3 |
| React to message | Native | Native | Reaction messages supported | Missing | P / N / N | Inbound Cloud payload can be described, but there is no outbound reaction method. | P2 |
| Edit sent message | Native for eligible recent messages | Native | Official support is product/version dependent | Missing | P / N / N | No edit operation, original-key retention contract, or UI action. | P3 |
| Delete/revoke for everyone | Native within app rules | Native | Official support is product/version dependent | Missing | P / N / N | Campaign cancellation stops queued rows; it does not revoke messages already sent. | P2 |
| Delete locally | Native | Native | Not a transport API concern | Missing | Y / N / N | No per-user local-message deletion/tombstone UI. Retention jobs are not the same feature. | P4 |
| View-once media | Native | Native | Supported only under applicable platform rules | Missing | P / N / N | No view-once flag or policy. | P3 |
| Disappearing-message timer | Native | Native | Not normal Cloud API parity | Missing | P / N / N | No chat ephemeral configuration or expiry semantics. | P3 |
| Native quick-reply buttons | Not a normal personal-chat composer feature | Business surfaces/templates can have buttons | Supported in official interactive/template forms | Broken | Y / N / N | Multiple QR wire formats exist, but real-handset work concluded buttons were not dependable; QR provider correctly sets `supports_interactive_buttons = False` (`qr_bridge.py:295-305`; `buttonLab.ts:44-74`). | P1 |
| URL button | Not general personal-app parity | Template/business interaction | Supported in applicable official forms | Broken | Y / N / N | QR provider advertises false because a URL row did not reliably appear on recipients' phones. Plain URLs still work. | P1 |
| Call button | Manual app has calls | Business/profile/template contexts vary | Template-only/applicable official forms | Missing | N / N / N | Provider base explicitly declares call buttons false (`base.py:190-201`). | P2 |
| Native list/menu | Not ordinary personal composer parity | Business interaction | Supported | Missing | P / N / N | QR provider advertises `supports_list_messages = False`; no bridge list send route is used. | P2 |
| Numbered menu fallback | Manual text | Business quick-reply workaround | Not needed when interactive supported | Partial | Y / P / N | App can degrade options to numbered text and resolve replies (`automation/tests_numbered_options.py`). It is readable but not native, and inbound crypto health can prevent resolution. | P1 |
| Native poll | Native | Native | Repo's Cloud provider marks unsupported | Partial | Y / P / N | QR bridge sends Baileys' first-class poll payload (`baileysProvider.ts:814-825`), but audit self-test was offline; no recorded cross-device vote E2E proves reliability. | P1 |
| Poll vote ingestion/results | Native | Native | Platform-specific | Missing | P / N / N | Bridge maps message contents, not poll update/vote events, and product has no vote result model. | P2 |
| Approved template send | N/A | App quick replies are not API templates | Core official capability | Partial | Y / P / N | Cloud provider sends approved templates. QR provider interpolates local variables and sends free-form content; it is not an approved Meta template and must not be marketed as one. | P0 claims |
| Template language/components | N/A | App labels/quick replies | Native official template fields | Partial | Y / P / N | Local template variables/media/interactions exist; QR rendering is ordinary messages and is not validated/approved by Meta. | P1 |
| Product/catalog message | N/A | Native catalog/product features | Official commerce messages where enabled | Missing | P / N / N | No catalog, product, cart, order, or commerce provider contract. | P2 |
| Payment message | Native availability is region-specific | Business availability is region-specific | Official/payment availability is market-specific | Missing | N / N / N | Razorpay handles SaaS subscription billing, not WhatsApp payment messages. | P3 |
| Voice call | Native | Native business calling | Cloud calling availability/rules are separate | Missing | N / N / N | Twilio voice is a separate product module; there is no WhatsApp call signaling/media. | P3 |
| Video call | Native | Native | Not normal messaging API parity | Missing | N / N / N | No WhatsApp video-call stack. | P4 |

## 3. Inbound messages, receipts, and conversation fidelity

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| New direct text ingestion | Immediate and durable | Immediate and durable | Signed webhook | Broken | Y / N / N | `messages.upsert` maps direct chats into a signed Django webhook (`baileysProvider.ts:376-405,580-645`), but live audit logs showed sustained Signal `Bad MAC`/no-session failures while health remained green. | P0 |
| Own-message echo suppression | App does not duplicate sent bubbles | Same | Correlated outbound/status data | Implemented | Y / P / N | Bridge filters `fromMe` and Django correlates outbound rows. Superseded socket guards reduce double forwarding. | P1 |
| Group-vs-direct discrimination | Both are visible in their own threads | Same | Group chat is not standard Cloud API scope | Implemented as exclusion | Y / Y / N | Group and broadcast JIDs are deliberately rejected before correspondent extraction, preventing a group member from becoming a false private conversation (`baileysProvider.ts:61-124,605-607`). This is safe filtering, not group support. | P2 |
| Inbound image metadata | Native image bubble | Native | Webhook metadata plus media retrieval | Broken | P / N / N | Bridge emits only type/caption, not media ID/URL/bytes; Django stores a caption or placeholder (`qr_bridge.py:116-154`; `inbox/services.py:111-114`). | P0 product |
| Inbound image download/display | Native | Native | Authenticated media download | Missing | P / N / N | No download pipeline, object storage, scanner, media URL, thumbnail, or authenticated display path for QR inbound images. | P0 product |
| Inbound video | Native | Native | Supported webhook/media retrieval | Broken | P / N / N | Same caption-only loss as image; the actual video is not retained. | P0 product |
| Inbound document | Native, named file | Native | Supported webhook/media retrieval | Broken | P / N / N | Filename/MIME/size/bytes are lost; inbox records only a placeholder/caption. | P0 product |
| Inbound audio/voice note | Native player | Native | Supported webhook/media retrieval | Broken | P / N / N | Bridge recognizes audio at the low layer, adapter treats it as unsupported, and inbox has no audio type/player. | P1 |
| Inbound sticker | Native | Native | Supported within platform payload set | Broken | P / N / N | Detected in bridge but becomes an unsupported descriptor; no asset is stored/rendered. | P2 |
| Inbound GIF | Native | Native | Media payload | Missing | P / N / N | No explicit classification or media retrieval. | P3 |
| Inbound contact card | Native | Native | Contacts payload supported | Missing on QR | P / N / N | Inbox can describe a Cloud-shaped contacts payload, but QR bridge does not map contact content into that shape. | P2 |
| Inbound location | Native map | Native | Location payload supported | Missing on QR | P / N / N | Cloud-shaped description helper exists (`inbox/services.py:48-60`), but bridge does not emit coordinates/location objects. | P2 |
| Inbound reaction | Native on target bubble | Native | Reaction webhook supported | Missing on QR | P / N / N | Inbox parser has a reaction text fallback, but bridge does not map reaction content or target message context. | P2 |
| Native button reply | Native selection | Native | Supported reply payload | Partial | Y / N / N | Bridge normalizes several reply envelopes (`qr-bridge/src/buttonReply.ts`) and Django stores `replied_button_id`; outbound native QR buttons themselves are unreliable and crypto failures can lose the reply. | P1 |
| List reply | Native selection | Native | Supported reply payload | Missing on QR | P / N / N | Cloud-shaped inbox parser understands interactive replies, but QR never sends a native list. Numbered-text fallback is a different path. | P2 |
| Poll vote update | Native | Native | Product-specific | Missing | P / N / N | No poll-update listener/model/analytics. | P2 |
| Quoted/replied-to context | Native visual quote | Native | Webhook context can identify prior message | Missing | P / N / N | Canonical inbound event discards context/message linkage beyond provider ID. | P2 |
| Sender push name | Native | Native | Contact/profile fields where supplied | Partial | Y / P / N | Bridge extracts push name, but adapter's Cloud-shape translation does not retain it in the returned message object (`qr_bridge.py:122-154`). | P3 |
| Sent receipt | Check mark | Check mark | Status webhook | Partial | Y / P / N | Numeric updates map to sent/delivered/read (`baileysProvider.ts:697-732`). Best-effort webhook delivery can lose the update. | P0 reliability |
| Delivered receipt | Double check | Double check | Status webhook | Partial | Y / P / N | Modeled and reported, but no durable bridge outbox/retry. | P0 reliability |
| Read receipt | Blue checks subject to privacy/settings | Same | Read status webhook when available | Partial | Y / P / N | Modeled through message/report layers, but delivery is lossy and privacy/client behavior means absence is not failure. | P1 |
| Failed/undeliverable receipt | User-visible failure | Same | Error/status payload | Partial | P / N / N | Send calls return classified errors; bridge's status map only includes numeric sent/delivered/read and is not a comprehensive asynchronous failure taxonomy. | P1 |
| Inbound deduplication | No duplicated bubble | Same | Webhook event IDs/idempotency | Partial | Y / P / N | Django has unique provider event IDs and event storage, but a receiver failure after `get_or_create` can strand the event because subsequent delivery sees it as already created. | P0 |
| Durable inbound webhook delivery | First-party app owns transport | Same | Webhook sender retries according to platform behavior; receiver must be idempotent | Broken | Y / N / N | Bridge performs one `fetch`, with no timeout policy, retry queue, backoff, or disk spool, then logs and discards failures (`qr-bridge/src/webhooks.ts:8-24`). | P0 |
| Replay failed inbound events | App transparently recovers | Same | Consumer-side replay/dead-letter practice | Partial | Y / P / N | Django stores events and has `replay_inbound_events`, but bridge failures before Django receives an event are unrecoverable, and the created-event signal trap can require operator intervention. | P0 |
| Message-order preservation | Ordered thread | Ordered thread | Webhooks can require consumer ordering logic | Partial | Y / N / N | Asynchronous fire-and-forget forwarding and worker execution have no documented per-chat sequencer; no load evidence proves order under reconnect/backlog. | P1 |
| Full-fidelity raw payload retention | First-party store | First-party store | Webhook payload can be retained subject to policy | Partial | Y / P / N | Django persists received raw event JSON. Content already discarded by the bridge cannot be recovered from that JSON. | P1 |

## 4. Groups, broadcasts, communities, channels, and status

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Receive group messages | Native | Native | Not standard Cloud API business messaging | Missing | P / N / N | Group JIDs are explicitly dropped (`baileysProvider.ts:100-124,605-607`). | P2 |
| Send to a group | Native | Native | Not standard Cloud API business messaging | Missing | P / N / N | Recipient normalization always builds a direct-user JID; no group target model/API. | P2 |
| Group thread in shared inbox | Native group thread | Native | N/A | Missing | P / N / N | Conversation model assumes one contact and one WhatsApp account, not group/member identity. | P2 |
| Group mentions/replies | Native | Native | N/A | Missing | P / N / N | No participant/mention/context fields. | P3 |
| Group participant management | Native subject to role | Native | N/A | Missing | P / N / N | No group metadata/admin operations. | P3 |
| Group create/settings/invite | Native | Native | N/A | Missing | P / N / N | No provider contract or UI. | P3 |
| Broadcast lists | Native with delivery constraints | Native | Official one-to-one messaging/templates, not app broadcast-list control | Missing | P / N / N | Broadcast JIDs are rejected and there is no import/control of app broadcast lists. Campaigns are a separate server-side fan-out. | P2 |
| Communities | Native | Native | N/A | Missing | P / N / N | No community model or transport operations. | P4 |
| Channels | Native | Native | Separate product surface | Missing | P / N / N | No channel/newsletter model or API. | P4 |
| View contacts' status/stories | Native | Native | N/A | Missing | P / N / N | Status/broadcast JIDs are not ingested. | P4 |
| Publish status/story | Native | Native | N/A | Missing | P / N / N | No status-send API or composer. | P4 |
| Status viewers/replies | Native | Native | N/A | Missing | P / N / N | No status event pipeline. | P4 |

## 5. Presence, privacy, profile, and chat controls

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Online/offline presence | Native subject to privacy | Native | Not a general Cloud business capability | Missing | P / N / N | No presence subscription or event model. `CONNECTED` describes the bridge session, not a contact's presence. | P3 |
| Typing/recording indicator receive | Native | Native | Limited/not core Cloud parity | Missing | P / N / N | No chatstate listener or frontend event. | P3 |
| Send typing indicator | Native | Native | Not core Cloud parity | Missing | P / N / N | No presence update before replies/AI generation. | P3 |
| Last seen | Native subject to privacy | Native | Not core Cloud parity | Missing | P / N / N | No field/provider operation. | P4 |
| Mark read on WhatsApp | Sends read receipt subject to setting | Same | Official mark-read endpoint | Missing on QR | P / N / N | `inbox.services.mark_read` changes local database status only (`inbox/services.py:192-196`); it does not call WhatsApp. | P2 |
| Mute chat | Native local control | Native | N/A | Missing | P / N / N | No chat setting. | P4 |
| Archive/unarchive chat | Native | Native | N/A | Missing | P / N / N | Closing a SaaS conversation is not WhatsApp archive state. | P3 |
| Pin chat | Native | Native | N/A | Missing | P / N / N | No pin state. | P4 |
| Block/unblock contact | Native | Native | N/A/limited | Missing | P / N / N | Product opt-out suppresses marketing but does not invoke WhatsApp block. | P3 |
| Report user/business | Native | Native | N/A | Missing | N / N / N | No report operation; it should not be simulated. | P4 |
| Contact WhatsApp registration check | App resolves registered recipient | Same | Official behavior/error handling | Partial | P / P / N | Send path normalizes digits and relies on the provider result; no product-grade preflight/quality taxonomy is exposed. | P2 |
| Own profile photo/name/about management | Native | Native | Business profile APIs are separate/limited | Missing | P / N / N | Connection metadata can store number/display name, but cannot edit profile/photo/about. | P3 |
| Business hours/address/category/site | N/A | Native business profile | Official business profile endpoint | Partial | N / N / N/A | SaaS has its own `BusinessProfile` for prompting/branding, not synchronization to the WhatsApp Business profile. | P2 |
| Labels synchronized with Business App | N/A | Native labels | Not a standard Cloud sync feature | Missing | P / N / N | Inbox labels are SendStack-local (`inbox/models.py:53-64`). | P3 |
| Catalog synchronization | N/A | Native catalog | Official commerce/business assets | Missing | P / N / N | No Meta Commerce/catalog integration. | P2 |

## 6. Shared inbox and agent collaboration

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Unified direct-message thread | One chat per person | One chat per person | Build from webhooks | Partial | Y / N / N | Conversation/message models and UI exist; reliability is broken by decrypt and webhook-loss defects. | P0 |
| Multiple agents sharing inbox | Not a personal-app feature | Limited linked devices | Typical SaaS layer over official API | Implemented | Y / P / N | Tenant conversations support assignment and role-controlled access (`inbox/models.py:5-35`; `inbox/services.py:199-220`). | P1 |
| Agent assignment | N/A | App labels/manual ownership only | SaaS concern | Implemented | Y / P / N | Assigned agent relation and service/UI actions exist. | P2 |
| Conversation labels | N/A | Business App labels | SaaS concern | Implemented | Y / P / N | Tenant-local labels/colors, filters, and many-to-many relation exist. They are not synchronized to the phone. | P2 |
| Internal notes | N/A | Not native chat content | SaaS concern | Implemented | Y / Y / N/A | `ConversationNote` is stored separately from customer-visible messages (`inbox/models.py:66-79`). | P2 |
| Close/reopen conversation | N/A | Archive-like workflow | SaaS concern | Implemented | Y / Y / N/A | Explicit open/closed state and timestamps. Does not archive WhatsApp itself. | P2 |
| Unread count/local mark-read | Native | Native | SaaS concern | Implemented | Y / P / N/A | Local unread/read workflow exists; it should be labeled local because it does not send a WhatsApp read receipt. | P2 |
| Live inbox updates | Native push | Native | Webhook + app realtime | Implemented | Y / P / N | Tenant-scoped Redis pub/sub and Server-Sent Events exist (`backend/apps/realtime/events.py`). It cannot display an event the bridge lost. | P1 |
| Search/filter | Native | Native | SaaS concern | Partial | Y / P / N/A | Conversation filters/labels/status exist; full-text/media/history parity is limited by data loss and no history sync. | P2 |
| Send text reply from inbox | Native | Native | Supported | Implemented | Y / P / N | Creates queued Message then Celery send (`inbox/services.py:224-279`). No operation ID is guaranteed on all historical/non-campaign call paths per provider contract comment. | P0 reliability |
| Send media reply from inbox | Native | Native | Supported | Partial | Y / P / N | Outbound image/video/document works, but arbitrary URL fetch creates SSRF/exfiltration risk and inbound media is absent. | P0 security |
| Send native-button reply from inbox | N/A | Business interaction | Supported official forms | Broken on QR | Y / N / N | Capability gate should fall back instead of falsely promising native QR buttons. | P1 |
| Collision prevention / agent presence | N/A | N/A | SaaS concern | Missing | Y / N / N/A | No visible “agent is typing/viewing”, optimistic lock, draft ownership, or double-reply guard. | P2 |
| SLA timers/queues | N/A | N/A | SaaS concern | Partial | Y / P / N/A | Awaiting-reply and assignment timestamps exist, but no complete SLA policy/escalation surface was found. | P3 |
| Conversation export | App export | App export | Data portability concern | Partial | Y / P / N/A | Campaign CSV reports exist; no user-facing full chat export with inbound media fidelity. | P2 |
| AI reply suggestion | N/A | Some first-party business tooling varies | SaaS layer | Implemented | Y / P / N | Suggest-reply endpoint/provider abstraction exists (`backend/apps/ai/views.py:386-423`). Output quality/safety depends on configuration and source data. | P2 |
| AI conversation summary | N/A | Product-dependent | SaaS layer | Implemented | Y / P / N | Summarise endpoint exists; lost inbound messages make summaries incomplete. | P1 |
| Human handoff/resume bot | N/A | N/A | SaaS layer | Implemented | Y / P / N | Chatbot session can hand off, pause/resume, and assign agents. Transport loss still breaks the trigger chain. | P1 |

## 7. Contacts, CRM, and consent

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Contact CRUD | Phone contact book | Phone contact book | Customer system responsibility | Implemented | Y / Y / N/A | Tenant-scoped contacts include E.164-like phone, name, company, country, custom fields, owner, lifecycle, source. | P2 |
| CSV import | Phone import/sync | Business tools vary | Customer system responsibility | Implemented | Y / P / N/A | Asynchronous import, mapping, errors, groups/tags, and streaming tests exist (`contacts/models.py:201-249`). | P2 |
| Paste import | N/A | N/A | SaaS concern | Implemented | Y / Y / N/A | Preview/dedup UI and backend parsing exist. | P3 |
| Duplicate detection/upsert | Phone merges vary | Same | Customer responsibility | Implemented | Y / P / N/A | Normalization/upsert/import duplicate paths and tests exist; cross-country number ambiguity still requires correct input. | P2 |
| Groups and tags for audience | App contacts/groups | Business labels | Customer responsibility | Implemented | Y / Y / N/A | `ContactGroup`, `ContactTag`, many-to-many assignments (`contacts/models.py:9-26,153-154`). These are not WhatsApp group chats. | P2 |
| Dynamic segments | N/A | Business tools vary | Customer responsibility | Implemented | Y / P / N/A | JSON filter segments and segment services exist. Performance at very large scale is unproven. | P2 |
| Custom fields/personalization data | N/A | Business tools vary | CRM responsibility | Implemented | Y / P / N/A | JSON custom fields feed templates/workflows. Schema governance/audit at scale is limited. | P2 |
| CRM lifecycle/owner/company | N/A | Limited | CRM responsibility | Implemented | Y / Y / N/A | Contact lifecycle and owner fields plus separate CRM module exist. | P3 |
| Marketing consent state | Manual responsibility | Manual responsibility | Required policy/process concern | Implemented | Y / P / N | Explicit `UNKNOWN/GRANTED/...` state, source/reference/timestamp and consent history exist (`contacts/models.py:87-128`; `compliance/tests_consent_evidence.py`). Controls reduce abuse; they do not authorize QR automation. | P0 policy |
| STOP/START keyword handling | Manual | Business expected | Official policy-aligned practice | Implemented | Y / P / N | Built-in/custom keywords, confirmation, precedence, and tests exist. Crypto/webhook loss can miss the opt-out message. | P0 |
| Suppress unsubscribed contacts | Manual | Expected | Required policy/process concern | Implemented | Y / P / N | Campaign eligibility and automation stand-down paths exist; missed inbound STOP remains a critical failure mode. | P0 |
| Consent evidence/audit history | Manual records | Expected for compliant operations | Required business control | Implemented | Y / Y / N/A | Consent events are append-only/read-only and imports require source/reference semantics. Legal sufficiency is jurisdiction-specific. | P1 |
| Contact blocklist/suppression reason | Native block | Native block | Customer responsibility | Partial | Y / P / N | Subscription/consent status suppresses campaigns; no global hashed suppression service or synchronization with WhatsApp blocks. | P2 |
| Data export/erasure for a contact | App controls | App controls | Privacy-law/customer responsibility | Partial | Y / P / N/A | Retention and tenant deletion machinery exist, but no complete one-click subject export/erasure with downstream AI/vector/media/webhook proof was established. | P1 |

## 8. Campaigns, bulk sending, and sender controls

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Draft campaign | Manual send/broadcast | Broadcast list/manual tools | Official template campaigns built by integrator | Implemented | Y / Y / N | Campaign draft/status model, serializers, composer, and API exist. Commercial safety remains no for unofficial bulk automation. | P0 policy |
| Audience: all subscribed | Manual selection | Broadcast constraints | Customer-managed audience | Implemented | Y / P / N | Audience resolver filters eligibility/consent. | P1 |
| Audience: contact group | Contact selection | Labels/broadcast lists | Customer-managed audience | Implemented | Y / P / N | `AudienceType.GROUP` and tenant-scoped group relation. Not a WhatsApp group send. | P1 |
| Audience: selected contacts | Manual selection | Manual selection | Customer-managed audience | Implemented | Y / P / N | `CONTACT_IDS` audience and preview exist. | P1 |
| Audience: dynamic segment | N/A | Business tooling varies | Customer-managed audience | Implemented | Y / P / N | ContactSegment filters can define audiences. Scale benchmark absent. | P2 |
| Preview/resolved recipient count | Manual confirmation | Manual confirmation | Recommended integrator control | Implemented | Y / P / N | Preview reports eligibility, content degradation, and account assignment (`campaigns/preview.py`). | P1 |
| Personalization variables | Manual edit | Quick replies/templates | Official template variables | Implemented | Y / P / N | Local `{{...}}` resolution exists across templates/campaigns. QR result is free-form, not an approved template. | P1 |
| Immediate launch | Manual send | Broadcast/manual | API send | Implemented | Y / P / N | Creates per-recipient message rows and durable scheduled jobs. P0 duplicate windows remain. | P0 reliability |
| Future scheduling | App supports scheduled events only in limited contexts | Business tooling may schedule | Integrator scheduler | Broken | Y / N / N | `campaigns/services.py:245-304` creates queued messages and a future dispatch job; stalled recovery scans any old queued campaign and schedules an immediate resume without excluding future `scheduled_at` (`scheduling/services.py:358-417`). A campaign over one hour in the future can send early. | P0 |
| Pause campaign | N/A | Business tooling | Integrator concern | Implemented | Y / P / N | Service and API pause queued/running campaigns. Already claimed/in-flight sends cannot be recalled. | P1 |
| Resume campaign | N/A | Business tooling | Integrator concern | Partial | Y / N / N | Normal resume exists, but the same resume handler is misused by stalled recovery and can violate the schedule. | P0 |
| Cancel campaign | N/A | Business tooling | Integrator concern | Implemented | Y / P / N | Queued rows become cancelled and reserved credits are handled. Sent/in-flight messages remain irreversible. | P1 |
| Per-recipient delay | Manual pacing | Manual pacing | Rate/quality policy | Implemented | Y / P / N | Random/fixed delay bounds are stored and assigned. This is not a ban-prevention guarantee. | P1 |
| Randomized delay | Manual behavior | Manual behavior | Not a substitute for policy | Implemented | Y / P / N | Tenant delay modes and assigned per-message delays exist. Marketing it as “anti-ban” would be unsafe. | P0 claims |
| Batch size and sleep windows | Manual behavior | Manual behavior | Integrator throttling | Implemented | Y / P / N | Tenant sending settings include batch/sleep controls. No production throughput envelope is demonstrated. | P1 |
| Quiet hours | Manual judgment | Expected business setting | Policy/customer concern | Implemented | Y / P / N | Timezone-aware quiet-hour deferral exists (`backend/apps/compliance`). A timezone fallback can be a courtesy rather than a legal guarantee. | P1 |
| Daily/hourly account quotas | Manual | App/platform limits | Official limits/quality are Meta-managed plus integrator controls | Implemented | Y / P / N | `SendingPolicy` and `AccountSendQuota` reserve slots under database locks (`whatsapp/models.py:281-376`). Limits cannot make unauthorized automation approved. | P0 policy |
| Warm-up schedule | Manual gradual use | Manual gradual use | Not an official authorization mechanism | Implemented | Y / P / N | Progressive caps exist with tests (`whatsapp/tests_sender_health.py`). Call it risk control, not “safe from bans.” | P0 claims |
| Automatic sender pause | User stops manually | User stops manually | Integrator quality guard | Implemented | Y / P / N | Failure/ban-hint health events can pause an account. False negatives remain because crypto/session health is not truthfully observed. | P0 reliability |
| Account health score | First-party app surfaces account state | Same | Official quality/status surfaces | Broken | Y / N / N | Score uses stored account/send state and can remain healthy during massive decryption failure. | P0 |
| Multiple-account rotation | Manual account switching | Manual | Multiple official phone numbers | Implemented | Y / P / N | Auto assignment uses shuffled round-robin and excludes paused/unavailable accounts (`campaigns/assignment.py`). Cross-number messaging can confuse recipients and quality systems. | P1 |
| Sticky sender per contact | User normally sees one business number | Same | Business chooses phone number | Partial | Y / P / N | Assignment favors campaign distribution; no strong, documented lifetime contact-to-number affinity across campaigns. | P2 |
| Failure-aware failover | User retries | Same | Integrator retry/routing | Partial | Y / N / N | Paused accounts are excluded and retry logic exists, but ambiguous sends must not simply move to another sender; recovery currently can requeue ambiguity. | P0 |
| Retry transient failures | Manual retry | Manual retry | Error-code-aware retry | Partial | Y / N / N | Classified errors, backoff and leases exist. QR operation recovery incorrectly requeues a known ambiguous outcome (`messaging/recovery.py:250-257`), risking duplicate delivery. | P0 |
| Exactly-once/idempotent submission | App hides transport details | Same | Idempotency must be designed by integrator | Broken | Y / N / N | Bridge performs operation lookup, send, then remember without an atomic per-key lock; two concurrent calls can send twice, and a crash after send before remember loses proof (`baileysProvider.ts:770-850`; `operationStore.ts:75-89`). | P0 |
| Crash recovery for leased messages | First-party recovery | Same | Integrator concern | Partial | Y / N / N | DB leases/attempts and a recovery sweep are thoughtful, but the ambiguity bug turns uncertain delivery into a new operation ID and possible duplicate. | P0 |
| Duplicate recipient suppression in one launch | Manual selection | Broadcast behavior | Integrator concern | Implemented | Y / P / N | Resolved audience/message uniqueness paths are tested; this does not protect transport-level duplicate sends. | P1 |
| Media campaign | Manual media send | Broadcast/media | Supported subject to policy/templates | Partial | Y / P / N | Image/video/document and multi-item campaigns exist; arbitrary URL fetch is a P0 SSRF/exfiltration primitive and no handset scale test exists. | P0 security |
| Poll campaign | Manual poll | Manual poll | Repo Cloud path does not support poll | Partial | Y / P / N | QR poll composer/send exists, but no real vote ingestion or recorded device-matrix validation. | P1 |
| Native interactive campaign | N/A | Business messaging | Supported in official forms | Broken on QR | Y / N / N | Product correctly degrades unsupported QR buttons/lists to readable text; it must not show native-interactive parity. | P1 |
| Opt-out affordance | Manual text | Expected | Policy-aligned | Implemented | Y / P / N | Button when supported, otherwise STOP footer/fallback (`campaigns/models.py:73-92`; compliance services). Native QR button unavailable. | P0 |
| Link click tracking | N/A | Business tools vary | Integrator analytics | Implemented | Y / P / N/A | Per-recipient tracked redirects, first/last/count, report totals (`tracking/services.py`; `reports/services.py:176-192`). Privacy notice/retention still needed. | P1 |
| Delivery/read/reply analytics | Native checks | Native checks | Webhook/status analytics | Partial | Y / N / N | Reports calculate sent/delivered/read/reply and per-account breakdown. Lost receipts/inbound events make the metrics incomplete. | P0 |
| CSV campaign export | Manual export limited | Business tools vary | Integrator concern | Implemented | Y / Y / N/A | Tenant-scoped export includes status/timestamps/error (`reports/views.py:84-127`). | P2 |
| A/B testing | Manual experimentation | Business tools vary | Integrator concern | Missing | Y / N / N/A | No variant randomization, holdout, significance, or winner logic. | P3 |
| Recurring campaign | N/A | Business tools vary | Integrator scheduler | Missing | Y / N / N | One scheduled launch exists; no recurrence model/timezone semantics. | P3 |
| Frequency cap per contact across campaigns | Manual judgment | Expected marketing control | Integrator/policy concern | Partial | Y / P / N | Account hourly/daily limits and cooldowns exist, but no clear tenant-wide per-contact marketing frequency cap across campaigns. | P1 |
| Complaint/block-rate feedback | User sees blocks locally | Business quality signals | Official quality/feedback surfaces | Missing on QR | N / N / N | QR transport has no authoritative quality rating or block/complaint feed. “Ban hint” parsing is not equivalent. | P0 business |

## 9. Automation, chatbot, and AI

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Welcome/new-conversation automation | Manual response | Greeting message in Business App | Webhook-driven automation | Implemented | Y / P / N | Automation triggers and presets exist. “New” means first observed by SendStack, not historical WhatsApp truth. | P1 |
| Keyword contains/exact/starts-with | Manual | Quick replies/away tools are simpler | Integrator concern | Implemented | Y / P / N | Rule and chatbot matchers implement multiple strategies (`automation/models.py:67-114`; `chatbot/flow.py:97-123`). | P1 |
| Regex trigger | N/A | N/A | Integrator concern | Implemented | Y / P / N | Validated chatbot regex trigger exists; test hardening is present. | P2 |
| Button trigger | N/A | Interactive workflow | Official interactive replies | Partial | Y / N / N | Reply-ID plumbing exists, but QR native buttons are unreliable; numbered fallback has separate resolution. | P1 |
| Follow-up after no reply | Manual reminder | Business tooling varies | Integrator concern | Implemented | Y / P / N | Template-scoped follow-up rules, arming/cancel-on-reply and tests exist. Missed inbound messages can incorrectly send a follow-up. | P0 reliability |
| Workflow graph | N/A | N/A | Integrator concern | Implemented | Y / P / N | Trigger, wait, condition, message, tag, assign-agent, AI-reply, and end nodes execute with persisted runs (`automation/services.py:686-1043`). | P1 |
| Visual chatbot flow | N/A | N/A | Integrator concern | Implemented | Y / P / N | Rich chatbot node set includes start/message/question/condition/AI/knowledge/media/delay/tag/variable/handoff/tool/end (`chatbot/flow.py:58-80`). | P1 |
| Ask question and resume | Manual | Business flows | Integrator concern | Implemented | Y / P / N | Waiting question nodes and persisted session/resume paths exist. Inbound loss can leave a flow stuck. | P0 reliability |
| Variables and conditions | N/A | N/A | Integrator concern | Implemented | Y / P / N | Scoped variables, branching, validation, and loop checks exist. | P2 |
| Delay/wait node | Manual wait | Away automation | Integrator scheduler | Implemented | Y / P / N | Durable scheduling is used; broad scheduler correctness needs regression coverage because campaign scheduling is already broken. | P1 |
| Tag/add/remove action | N/A | Business labels | Integrator concern | Implemented | Y / Y / N/A | Workflow actions mutate tenant-local contact tags. | P2 |
| Assign/handoff | N/A | Linked-device handoff is manual | Integrator concern | Implemented | Y / P / N | Agent assignment, handoff and chatbot transfer actions exist (`chatbot/actions.py:43-69`). | P1 |
| Send automation media | Manual | Greeting/away media varies | Supported | Partial | Y / P / N | Same outbound media path and SSRF risk as campaigns. | P0 security |
| Call external HTTP API | N/A | N/A | Integrator concern | Implemented | Y / P / N/A | Chatbot `CALL_API` and outbound URL validation exist. Operational allowlists/egress policy still need production review. | P1 |
| Invoke internal tool | N/A | N/A | Integrator/AI concern | Implemented | Y / P / N/A | Tool allowlist/validation and audit models exist. Tool side effects require per-tool threat modeling. | P1 |
| Trigger campaign/automation from bot | N/A | N/A | Integrator concern | Implemented | Y / P / N | Actions exist; campaign scheduling/send correctness defects propagate into this surface. | P0 |
| AI automatic reply | N/A | Some business products offer AI | Integrator concern | Implemented | Y / P / N | Multi-provider config, agent processing, budgets/usage, and inbound receiver exist. Safety and completeness depend on received history. | P1 |
| Knowledge-base retrieval | N/A | N/A | Integrator concern | Implemented | Y / P / N | Typed/uploaded docs, chunking, embeddings, retrieval and knowledge-only mode exist (`backend/apps/ai`). | P1 |
| Knowledge citations/source grounding | N/A | N/A | Integrator concern | Partial | Y / P / N | Retrieval evidence is stored, but customer-facing answer citation behavior and hallucination evals are not proven for production. | P2 |
| AI provider choice/BYOK | N/A | N/A | Integrator concern | Implemented | Y / P / N/A | OpenAI, Anthropic, OpenRouter, OpenAI-compatible/custom providers are abstracted. Secret handling and vendor DPAs remain operator work. | P1 |
| AI usage metering | N/A | N/A | Integrator concern | Implemented | Y / P / N/A | Usage log/read API exists. Cost-limit enforcement under concurrency needs production evidence. | P2 |
| Human approval before AI send | Manual | Manual | Safety control | Partial | Y / P / N | Suggest-reply enables approval, while automatic agent mode can send. No universal approval gate is enforced by design. | P2 |
| Bot loop protection | N/A | N/A | Integrator concern | Implemented | Y / P / N | Workflow validation, duplicate-run, throttle, and loop/crash-containment tests exist. | P1 |
| Bot analytics/trace | N/A | N/A | Integrator concern | Implemented | Y / P / N/A | Node traces, session/resolution/outcome models, and overview analytics exist. Missing inbound events still skew them. | P1 |
| Multilingual rules/AI | Native language text | Business language tools vary | Supported content/language templates | Partial | Y / P / N | Unicode and configurable chatbot language/AI prompts exist; there is no broad locale QA or automatic translation guarantee. | P3 |

## 10. Public API, webhooks, tenancy, billing, and reseller surface

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Public message-send API | No | No general app API | Official core capability | Implemented | Y / P / N | `/api/public/v1/messages/send`, message read/status, API-key authentication, scopes and idempotency mixin exist (`publicapi/urls.py:71-75`; `publicapi/scopes.py`). Underlying QR transport remains unofficial/broken. | P0 policy |
| Public campaign API | No | No general app API | Integrator-built around official API | Implemented | Y / P / N | Create/read/results/start/pause/resume/cancel endpoints and scoped keys exist. Future scheduling and transport idempotency are P0. | P0 |
| Public contacts/templates/accounts/reports API | No | No general app API | Integrator concern | Implemented | Y / P / N | Resource endpoints and granular scopes exist (`publicapi/urls.py:87-104`). QR “templates” are local content, not Meta approval objects. | P1 claims |
| OpenAPI documentation | N/A | N/A | Official API has maintained docs | Implemented | Y / P / N/A | OpenAPI JSON, interactive docs/reference and Postman export exist. Deployment check reports many schema-generation warnings, so completeness must be verified. | P2 |
| API key hashing and one-time display | N/A | N/A | Security baseline | Implemented | Y / Y / N/A | Keys are generated/hashed, scoped, revocable and environment-labelled (`backend/apps/publicapi/keys.py`, `authentication.py`). | P1 |
| Granular API scopes/default deny | N/A | N/A | Security baseline | Implemented | Y / Y / N/A | Flat explicit scope vocabulary and default-deny permission design (`publicapi/scopes.py:1-52`). | P1 |
| API rate limits | N/A | N/A | Platform and customer limits | Implemented | Y / P / N/A | Public API throttling exists; production capacity and distributed-limit accuracy require load tests. | P1 |
| API idempotency key | N/A | N/A | Important send/create control | Partial | Y / N / N | HTTP response idempotency exists, but it cannot close the bridge send/remember race or crash gap. | P0 |
| Customer outbound webhooks | N/A | N/A | Common integration surface | Implemented | Y / P / N/A | Endpoint management, HMAC secrets, retries/delivery history are represented in public API. This is separate from the bridge-to-Django webhook, which is broken. | P1 |
| Bridge-to-Django webhook authentication | N/A | N/A | Signed webhook baseline | Implemented | Y / P / N | HMAC validation and distinct signature headers exist (`qr_bridge.py:41-63`). Authentication does not provide durability. | P0 reliability |
| Bridge-to-Django retry/outbox | N/A | N/A | Production webhook baseline | Broken | Y / N / N | Single fire-and-forget `fetch`; no retry, timeout, durable queue, DLQ, or replay token (`qr-bridge/src/webhooks.ts`). | P0 |
| Multi-tenant data model | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Tenant foreign keys/scoped query patterns are pervasive; central isolation tests cover important resources, not demonstrably every endpoint/model. | P1 |
| Role hierarchy | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Superadmin/reseller/user roles and tenant hierarchy are modeled (`accounts/models.py:35-115`; `tenants/models.py:7-31`). | P1 |
| Reseller child customers | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Platform → reseller → customer hierarchy, managed-user boundaries, plans/coupons/credits and tests exist. | P1 |
| White-label branding | N/A | Business brand lives in WhatsApp | SaaS concern | Implemented | Y / P / N/A | Tenant branding supports product name/logo/color/support email/custom domain (`tenants/models.py:48-85`). It does not change WhatsApp's own brand/account profile. | P2 |
| Custom-domain TLS automation | N/A | N/A | SaaS concern | Partial | Y / N / N/A | Custom-domain data model/proxy deployment assets exist, but automatic certificate/domain validation lifecycle for arbitrary resellers was not established. | P1 |
| Subscription plans/feature gates | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Plans, intervals, limits/features, grace/expiry, upgrades/downgrades exist (`subscriptions/models.py`). | P1 |
| Online payments | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Razorpay checkout, signature verification, webhooks, cancellation, plan change, coupons and history exist. Live credentials/reconciliation are deployment responsibilities. | P1 |
| Credits and reservation ledger | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Transactional assign/deduct/reserve/release/consume with operation IDs and hierarchy safeguards (`credits/services.py`). | P1 |
| Usage entitlement blocks sending | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Messaging gates check subscription/credits/features. P0 recovery duplicates can make the commercial/message ledger disagree with recipient reality. | P0 |
| Audit log | N/A | N/A | SaaS/security concern | Implemented | Y / P / N/A | Append-oriented `AuditEvent` and call-site/immutability tests exist. Coverage of every privileged action still needs an explicit inventory. | P1 |
| Account suspension/data preservation | N/A | N/A | SaaS concern | Implemented | Y / P / N/A | Suspended authentication/sending and preservation tests exist. | P1 |
| Admin impersonation with trace | N/A | N/A | High-risk SaaS support feature | Implemented | Y / P / N/A | Explicit session/reason/refresh-JTI model exists (`accounts/models.py:117-138`). Browser token storage raises session-theft blast radius elsewhere in the audit. | P1 security |

## 11. Security, privacy, operations, and scale

| Capability | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| Safe outbound media retrieval | First-party app handles local media | Same | URL/upload constraints under operator control | Broken | Y / N / N | Tenant-controlled `http(s)` URLs, including internal hostnames, pass to Baileys for server-side fetch (`backend/core/media.py:31-85`; `baileysProvider.ts:1109-1122`). This is SSRF plus possible internal-data exfiltration to a WhatsApp recipient. | P0 |
| Media type/magic/size validation for uploads | First-party app validates | Same | Security baseline | Implemented | Y / P / N/A | Local uploads use extension/magic/size checks and UUID paths. Remote URLs bypass those guarantees until fetched. | P0 remote URL |
| Malware scanning | First-party ecosystem protections | Same | Operator responsibility | Missing | Y / N / N/A | No antivirus/content-disarm pipeline for customer or inbound files. | P1 |
| Authenticated/expiring media access | App-controlled | App-controlled | Signed/authorized media download | Missing | Y / N / N/A | `/media` is served through unguessable paths but is not per-request tenant-authorized or expiring. Inbound media is not stored at all. | P1 |
| Secure browser token storage | First-party secure client | Same | Integrator responsibility | Partial | Y / N / N/A | JWT access/refresh and impersonation originals are in `localStorage`; nginx SPA responses lack a strong CSP in the audited configuration. Any XSS has high blast radius. | P1 |
| Secret separation | Device manages keys | Device manages keys | App secret/tokens scoped by Meta/operator | Partial | Y / P / N/A | Bridge API bearer and webhook HMAC are separated, but one global bridge credential spans sessions and auth-state files lack encryption. | P1 |
| Inbound webhook signature | N/A | N/A | Required | Implemented | Y / P / N | HMAC validation uses the configured secret. Replay/durability semantics still fail. | P0 reliability |
| Database-backed durable message queue | First-party managed | Same | Integrator concern | Implemented | Y / P / N/A | Message rows, attempts, leases, scheduled jobs, Celery/Redis and recovery tools exist. Correctness has P0 edge failures. | P0 |
| Backpressure/capacity limit | First-party managed | Same | Rate limits/queues | Partial | Y / N / N/A | Queues, batches and per-account quotas exist, but no demonstrated 1k/10k/100k production envelope or bridge saturation control. | P1 |
| Horizontal Django/worker scale | N/A | N/A | Normal production design | Partial | Y / P / N/A | Stateless API and DB/Redis queues are broadly scalable; some scheduling/locking behavior needs load proof. | P1 |
| Horizontal QR bridge scale | N/A | N/A | Meta manages Cloud service | Missing | P / N / N | In-memory socket/session map plus local auth volume has no routing/ownership/fencing design. | P1 |
| Durable bridge operation ledger | N/A | N/A | Provider IDs/webhook correlation | Broken | Y / N / N | JSON operation store writes on a later tick and swallows persistence errors; 24-hour TTL and crash gap cannot support exactly-once recovery (`operationStore.ts:18-29,75-89`). | P0 |
| Metrics for message pipeline | First-party telemetry | Same | Official analytics plus operator observability | Partial | Y / N / N/A | Health/operations reports and structured logs exist, but bridge crypto health and lost webhooks are not authoritative metrics/alerts. | P0 |
| Alert on silent inbox outage | First-party app visibly fails | Same | Operator webhook alerting | Missing | Y / N / N/A | Audit observed the exact failure: session green, inbound counters zero, crypto errors only in logs. | P0 |
| Dead-letter/recovery console | First-party managed | Same | Integrator concern | Implemented | Y / P / N/A | Health dead-letter endpoints and recovery-required operator actions exist. They cannot recover events never delivered by bridge. | P1 |
| Database backup | First-party managed | Same | Operator responsibility | Partial | Y / N / N/A | Backup/restore scripts and verification checks exist; at audit time available dumps were stale, plaintext, and on-host. | P0 launch |
| Media/session-volume backup | First-party managed | Same | Operator responsibility | Missing/Partial | Y / N / N/A | No verified off-host encrypted restore of media plus QR auth state; database-only restore does not restore a working WhatsApp service. | P0 launch |
| Documented RPO/RTO achieved | First-party managed | Same | Operator responsibility | Broken | Y / N / N/A | Documentation targets roughly daily backup and short recovery, but observed backup age was about six days and host loss remained a common-mode failure. | P0 launch |
| Disaster recovery drill | First-party managed | Same | Operator responsibility | Partial | Y / N / N/A | Restore verification exists for database artifacts, not a full clean-host reconstruction with media, secrets, DNS, certificates, and QR sessions. | P1 |
| Production configuration gate | First-party managed | Same | Operator responsibility | Implemented but currently failing | Y / N / N/A | Strict production check exists and correctly flags current dev settings (debug/HTTPS/cookies/CORS/email/frontend/backups). Do not charge while it fails. | P0 launch |
| TLS/reverse proxy deployment | First-party managed | Same | HTTPS required | Partial | Y / P / N/A | Compose production overlay/nginx/certbot assets exist and config validates; current active stack is the development override and is not launch evidence. | P0 launch |
| Dependency vulnerability audit | First-party maintained | Same | Operator responsibility | Partial | Y / P / N/A | Frontend/bridge production `npm audit` reported zero during this audit; Python vulnerability audit tooling was absent. Baileys major v6 is no longer the maintained major even though installed 6.7.24 avoids the cited `<6.7.22` advisory. | P1 |
| Real-device regression suite | First-party exhaustive | First-party exhaustive | Contract tests/sandbox/dev numbers | Missing | Y / N / N/A | QR self-tests validate local construction, not rendering, tap/vote behavior, media codecs, reconnect, or delivery on Android/iOS/Web across versions. | P0 |
| End-to-end load test evidence | First-party operated at scale | Same | Official capacity plus client load tests | Missing | P / N / N/A | Benchmark/seed scripts exist; no signed test report demonstrates queue latency, bridge memory, DB growth, rate behavior, or recovery at target scale. | P1 |
| Frontend regression tests | First-party tested | Same | SaaS concern | Missing | Y / N / N/A | No frontend unit/E2E suite was found; lint currently reports a conditional React Hook in `ContactProfile.jsx`. | P1 |

## 12. Claims that are safe, conditional, or unsafe

| Capability / proposed claim | Normal app expectation | Business App expectation | Cloud API benchmark | Repo status | QR verdict T/R/C | Evidence / notes | Priority |
|---|---|---|---|---|---|---|---|
| “Connect your own WhatsApp number by scanning a QR” | Plausible linked-device action | Plausible | Not Cloud API | Technically true | Y / P / N | It describes mechanics but omits that the shipped bridge is unofficial automation. It needs a prominent limitation/risk statement and still may not be commercially acceptable. | P0 legal |
| “Supports normal WhatsApp and WhatsApp Business App” | Both can technically link | Both can technically link | Different product | Misleading without qualification | Y / P / N | Support is limited to direct-message subsets. It does not provide full app feature parity and does not confer authorization. | P0 marketing |
| “Send bulk WhatsApp campaigns safely” | Conflicts with ordinary-app expectations/terms when unauthorized | Same | Possible only under official policies/limits | Unsafe | Y / N / N | Consent, pacing, warmup and pause controls are useful abuse controls, but WhatsApp expressly identifies impermissible bulk/auto messaging and unauthorized non-personal use. | P0 marketing |
| “Anti-ban” / “ban-proof” | No such guarantee | No such guarantee | Official quality/policy compliance still cannot guarantee | False/unsafe | N / N / N | Product has no authoritative QR quality rating or immunity from client/protocol enforcement. Do not use this claim. | P0 marketing |
| “Native buttons and menus” on QR accounts | Not general personal feature | Expected in business interactions | Supported officially | False for QR | Y / N / N | Code experiments exist, but capability flags are false because real handset rendering was not dependable. Advertise numbered-text fallback only. | P0 marketing |
| “WhatsApp templates” on QR accounts | N/A | Quick replies are not approved templates | Approved templates are official objects | Misleading | Y / P / N | Local reusable message templates are useful, but QR sends are free-form messages. Call them “saved messages” or explicitly distinguish them from Meta-approved templates. | P0 marketing |
| “Real-time shared inbox” | Native app is real time | Native | Webhook-based | Currently false as an SLA | Y / N / N | UI/SSE is real-time after Django records an event, but crypto failure and one-shot bridge webhook delivery can silently lose it. | P0 marketing |
| “Exactly-once campaign delivery” | First-party hides duplicates | Same | Not guaranteed without integrator controls | False | Y / N / N | Concurrent operation race, post-send/pre-persist crash gap, and ambiguous recovery requeue can duplicate messages. | P0 marketing |
| “Production ready” | High reliability expected | High reliability expected | Official/operated production controls | False today | Y / N / N | P0 correctness, SSRF, transport observability, backup/DR, production-config, and device-test gates are open. | P0 |
| “Cloud API compatible provider architecture” | N/A | N/A | Valid benchmark | Largely true | Y / P / Y | The provider abstraction genuinely supports QR and Cloud implementations. Sell/operate the Cloud path only after its own credentials, onboarding, policy, webhook and E2E launch review. | P1 |

## Coverage summary and product decision

The codebase has broad SaaS feature breadth but only a limited, fragile subset of WhatsApp transport parity. The biggest gap is not the number of missing convenience features; it is that the core direct-message loop cannot presently be promised as lossless or duplicate-safe:

1. A QR session can say `CONNECTED` while inbound decryption is failing at high volume.
2. Bridge-to-Django message and status webhooks have no durable retry/outbox.
3. Ambiguous send recovery can requeue and generate a new operation ID.
4. The bridge's check-send-remember sequence is non-atomic and its operation file is asynchronously persisted.
5. Future campaigns can be mistaken for stalled campaigns and sent early.
6. Remote attachment URLs can make the bridge fetch internal services and forward their bytes.

Those six facts make “implemented” transport rows conditional and place the current sell decision at **do not take money for QR/session messaging**. Closing feature gaps such as groups, reactions, status, calls, native lists, and history would not change that verdict until the P0 correctness/security/policy issues are closed.

### Minimum defensible launch boundary

A narrowly described beta could only be reconsidered after all P0 rows are fixed and independently retested on real devices. The supported QR envelope should then be stated as direct one-to-one text plus explicitly tested image/video/document and poll behavior, with numbered text options instead of native buttons/lists. Unsupported app features must stay visible in this matrix rather than disappearing from marketing.

For a commercially durable WhatsApp SaaS, the official Cloud API should remain an operable, policy-reviewed route and the QR/session route should never be presented as equivalent, approved, ban-proof, or full WhatsApp parity. This is a product/legal risk conclusion, not legal advice; obtain counsel and written platform authorization before selling the unofficial route.
