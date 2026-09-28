# The chatbot

Automated WhatsApp conversations: rules a business writes down, a flow they draw,
and the AI agent for everything else — with a person able to take over at any
point and hand it back.

## What it is, and what it is not

It is a **configuration and orchestration layer**. It owns which bot answers
which number, what that bot says, where a conversation has got to, and what
happened.

It owns none of the machinery underneath, and that is deliberate:

| The chatbot needs        | It uses                                              |
| ------------------------ | ---------------------------------------------------- |
| an inbound message       | `apps.whatsapp` webhooks + provider adapters          |
| a customer               | `contacts.Contact`                                    |
| a thread                 | `inbox.Conversation`                                  |
| a message                | `messaging.Message`                                   |
| sending                  | `automation.send_automated_message` → `messaging.send_message_task` |
| an AI answer             | `ai.services`, `ai.agent`, `ai.tools`, `ai.knowledge` |
| documents                | `ai.KnowledgeBase` / `KnowledgeDocument`              |
| live updates             | `apps.realtime` (SSE over Redis pub/sub)              |
| the audit trail          | `audit.log_event`                                     |
| compliance and limits    | `messaging.gating`, `whatsapp.limits`, `credits`      |

There is no second provider abstraction, no second send path, no second AI
system and no second real-time mechanism. A chatbot reply lands in the same
`Message` row, passes the same compliance gate and spends the same credit as a
message typed into the inbox.

## The flow of one message

```
customer's phone
   │
WhatsApp  ──►  Cloud API  ─┐
               QR bridge  ─┤
                           ▼
     POST /api/v1/whatsapp/webhook/<account_id>/        ← HMAC verified
                           │
     provider.parse_webhook_payload()                   ← one normalised shape
                           │
     WhatsAppWebhookEvent   UNIQUE(account, provider_event_id)   ← idempotency #1
                           │  signal: webhook_event_stored
     inbox.record_inbound_message()  →  Contact + Conversation + Message
                           │  signal: inbound_message_recorded
     automation.receivers ladder
       0. compliance (STOP words answer themselves and end it)
       0.5 ── chatbot ──────────────────────────────────────┐
       1..5 legacy welcome / keyword / workflow / AI agent  │  (skipped when a
                                                            │   bot owns the number)
                           ┌────────────────────────────────┘
                           ▼
     chatbot.tasks.process_inbound_message_task            ← Celery; the webhook
                           │                                 has already returned
     chatbot.engine.handle()
       1 claim (row lock + high-water mark)                ← idempotency #2
       2 loop protection
       3 state — is this a person's conversation?
       4 timeout — has it gone cold?
       5 language
       6 readable? (a sticker gets a sentence, not a model call)
       7 asked for a person? → straight to handoff
       8 business hours, then the reply ceiling
       9 welcome
      10 the answer to a question we asked last turn
      11 rules, in priority order
      12 flow, if its Start trigger claims the message
      13 AI  →  retrieval → relevance gate → model → tools
      14 fallback / handoff
                           │
     outbound.deliver()  ──► optional delay (Celery countdown + delivery token)
                           │
     automation.send_automated_message()  →  Message row
                           │
     messaging.send_message_task  →  gating → limits → credits → provider
                           │
                  back to the customer's phone
```

Every step writes a `ChatbotEvent` carrying one `correlation_id`, which is what
the debug panel renders and what the structured log lines carry.

## Getting one working

1. **Connect a number** under *Devices*. Cloud API or QR — both work.
2. **Chatbot → Bots → Create chatbot.** Pick the number and a type
   (*Hybrid* is the default and the right answer for almost everyone).
3. **Write a rule or two** under *Rules*: "when the message contains `price`,
   send *Our basic plan starts at ₹999/month*". Use **Test a message** to check
   it matches before anybody sees it.
4. **Optionally draw a flow** under *Flows* for a conversation with branches.
   Pick what starts it from the palette — any message, a keyword, a tapped
   option — and connect the Start step to the first thing that should happen.
5. **Publish.** Nothing answers a customer until you do.

To let the AI answer what the rules do not, configure a provider and key under
*AI Agent* first, and put your documents in a *Knowledge base*. The chatbot has
no credentials of its own — it uses the workspace's AI agent, with its own
prompt, documents and reply length layered on top.

## Publishing, versions and rollback

Configuration is edited freely; a **published version** is what answers
customers. That separation is what makes editing safe.

- **Publish** freezes the rules, the flow, the settings and the hours into a
  `ChatbotVersion` and points the bot at it.
- **Pause** stops the bot answering and keeps the version, so *Resume* is one
  click rather than a republish.
- **Roll back** points at an older version. It is pointed at, not copied
  forward, so the version list stays an honest record of what was live and when.

A conversation **part-way through a flow** keeps the version it started on: the
node it is standing on may not exist in the new graph. Everything else picks up
the current published version immediately, so a rule fixed at lunchtime reaches
a customer who wrote in that morning.

Only **one bot may be published per number**, enforced by a partial unique
index rather than by application code — two bots on one line would both answer
every message.

## How it fits with the older automation

A published chatbot has its own welcome message, its own priority-ordered rules,
its own flow and its own AI. Those are the same jobs the older
`apps.automation` keyword rules and workflows do, so running both would answer
one message twice.

**A published bot therefore answers instead of them.** A workspace with no
published bot reaches none of the new code and behaves exactly as it did before
this feature existed.

For a workspace migrating gradually, **Let existing automation answer first**
(`Chatbot.defer_to_automation`) restores the original ladder and gives the bot
only what nothing below it claimed. The Bots screen says so when a number has
both configured.

## Rules

Eight ways to match: exactly, contains, starts with, ends with, any of these
words, a regular expression, a button tap, or any message at all.

- Lower priority numbers run first. **The first rule that matches wins** — a
  catch-all at priority 1 silences everything below it, which is why the list is
  shown in evaluation order.
- **Keyword** matching is on whole words, so `price` does not fire on
  `surprise`.
- **Regular expressions** are compiled when saved, so a broken one is a form
  error rather than a rule that silently never matches. At match time the
  subject is truncated: a pattern written by one customer runs against text
  written by another, which is the classic catastrophic-backtracking setup.
- A **cooldown** stops a customer typing the same word five times getting five
  identical replies. A rule on cooldown still *claims* the message rather than
  letting it fall through to a completely different answer.

## Actions

The same vocabulary in rules and in flow nodes:

`SEND_MESSAGE` · `SEND_MEDIA` · `ASK_QUESTION` · `SET_VARIABLE` · `ADD_TAG` ·
`REMOVE_TAG` · `ASSIGN_AGENT` · `HANDOFF` · `TRANSFER_CHATBOT` ·
`TRIGGER_CAMPAIGN` · `TRIGGER_AUTOMATION` · `AI_REPLY` · `KNOWLEDGE_ANSWER` ·
`CALL_TOOL` · `CALL_API` · `END_CONVERSATION`

Everything is validated **when it is saved**: a knowledge base belonging to
another workspace, a regular expression that will not compile, a URL pointing at
`127.0.0.1`, an action after one that ends the conversation. By the time the
engine runs an action, the only failures left are the ones that genuinely could
not be known in advance.

`TRIGGER_CAMPAIGN` adds the contact to a campaign's audience; it does not launch
it. A rule that launched a bulk send because one person typed a keyword would
turn one inbound message into ten thousand outbound ones.

## Flows

A graph of nodes on `ChatbotVersion.flow`, validated on save:

`START` · `MESSAGE` · `QUESTION` · `CONDITION` · `AI` · `KNOWLEDGE` · `MEDIA` ·
`DELAY` · `TAG` · `VARIABLE` · `HANDOFF` · `TOOL` · `END`

### What starts a flow

The `START` node carries a **trigger**, and it is the only thing that decides
whether an inbound message enters the graph at all:

| Trigger | Starts the flow when |
| --- | --- |
| `ANY_MESSAGE` | any message arrives — the default, and what a graph saved before triggers existed does |
| `NEW_CONVERSATION` | the message is the first of a conversation |
| `KEYWORD` | the message mentions one of a comma-separated list, matched as whole words |
| `CONTAINS` / `EXACT` / `STARTS_WITH` | the text matches, the way the same-named rule does |
| `REGEX` | a regular expression matches (compiled on save, subject-bounded at match time) |
| `BUTTON` | the customer chose a reply option, by its **id** and never its label |

Every trigger except the first two is a `ChatbotRule.MatchType` under another
name, and `flow.trigger_matches` delegates to `matching.rule_matches` — so a
keyword trigger and a keyword rule claim exactly the same messages, including
the whole-word rule and the regex defence.

A trigger is asked once, when a conversation is *not* already in the graph. A
flow half-run carries on whatever the customer says next; the trigger has
already done its job. Messages a trigger declines fall through to the AI and
then to the fallback, and the trail says which trigger turned them away.

Refused on save: more than one Start, an edge to a node that does not exist, a
node nothing leads to, an action a node type cannot perform, and **a loop with
no Question node in it** — a cycle that never waits for the customer would run
until it hit the step cap, having sent forty messages on the way. A loop *with*
a Question is fine, because "ask again if the answer was not usable" is a good
loop.

Position lives on `ChatbotSession.current_node_id` / `awaiting_node_id`, so a
flow half-run when a worker restarts carries on from where it was.

## Variables

```
{{name}} {{first_name}} {{phone}} {{email}} {{company}} {{date}} {{time}}
{{language}} {{last_message}}         …and anything the bot has remembered
```

Two scopes. **Conversation** (`ChatbotSession.variables`) for things true of this
chat — the size they asked for, the branch they took. **Customer**
(`Contact.custom_fields`, which campaigns and templates already render from) for
things true of the person. A conversation variable wins: if they said "Pune"
thirty seconds ago and their record says Mumbai, answer with Pune.

Platform names (`phone`, `name`, `email`, …) are reserved and cannot be
overwritten — a flow that could set `{{phone}}` could make a later message
address somebody else.

## Languages

English, Hindi (Devanagari) and Hinglish are detected deterministically, with no
model call: script for Hindi, and a closed list of Hindi *function words* for
Hinglish. Function words rather than nouns, because "delivery", "order" and
"payment" appear in Hinglish constantly and matching on them would call every
English message Hinglish.

Detection is a hint. `AUTO` (the default) tells the model to mirror the customer,
which is what it does well; a configured language always overrides.

## Handing over to a person

Configurable per bot, defaulting to the cautious answers:

| Condition            | Default |
| -------------------- | ------- |
| the customer asks for a person | on |
| the bot cannot answer N times in a row | 3 |
| the customer sounds unhappy | off |
| the AI provider is unreachable | on |
| the bot has sent N replies | no limit |
| a keyword you choose | none |

"Talk to a human", "agent se baat karni hai" and "एजेंट से बात" all work, matched
before anything is retrieved so the request costs nothing.

On handoff the state moves to `HUMAN_ACTIVE`, the conversation is auto-assigned
if the workspace has routing on, the response clock restarts, the assigned agent
is notified, and **any reply the bot had already scheduled is cancelled**.

**Take over** and **Return to bot** are buttons in the inbox. Replying to a
conversation also takes it over, because a person who has just answered does not
want the bot answering over the top of them.

## Not answering twice

The single most important property, defended in four independent places:

1. **`WhatsAppWebhookEvent`** has `UNIQUE(account, provider_event_id)` — a
   redelivered webhook is stored once and signals once.
2. **`ChatbotSession.last_processed_message_id`** is a high-water mark written
   under a row lock. A redelivered Celery task, or a message overtaken by a newer
   one from the same customer, is refused.
3. **The ladder**: the first thing that speaks ends the turn.
4. **`MAX_REPLIES_PER_TURN`** and a per-conversation rate limit bound the damage
   if all three somehow failed.

The engine also refuses to answer a number belonging to this same workspace,
which is how two bots on two of your own numbers would otherwise talk to each
other forever.

**Ordering** is a separate concern from duplication, and has its own lock. One
turn at a time per conversation, held in the cache rather than as a database
transaction — a turn can take seconds, and holding `SELECT ... FOR UPDATE`
across a model call would tie up a connection for the duration. A second
message arriving mid-turn is *retried* rather than queued behind a blocked
worker, which is what turns "three messages in one breath" into three answers
in order. With no cache reachable the lock fails open: ordering is a nicety,
answering at all is not — and the one-reply-per-message guarantee does not
depend on it.

## Response delay

Randomised between a minimum and a maximum, so replies do not all land on the
same beat. Implemented as a Celery `countdown`, never a `sleep` — a worker
asleep for three seconds is a worker not answering anybody else.

Cancellation is a **token**, not a Celery revoke: revocation is advisory and a
worker already holding the task will run it regardless. Every scheduled reply
carries the session's `delivery_token` as it stood; a takeover, a close, or
another inbound message bumps it, and the delayed task declines to send.

## The debug panel

Every conversation has one, under **Debug** in the inbox. It shows each inbound
message as one journey, step by step, with timings:

```
[ ok ] RECEIVED      5ms  Inbound message received.
[ ok ] SESSION       7ms  Session claimed.
[ ok ] LANGUAGE     11ms  Replying in english.
[ ok ] RETRIEVAL   340ms  GOOD match (0.71).
[ ok ] AI_REQUEST  1.9s   The model answered.
[ ok ] SEND         20ms  Handed to the send pipeline.
[ ok ] DELIVERY      —    DELIVERED
```

It exists because "the bot didn't reply" has about fifteen causes — the number
was unlinked, an agent had the conversation, retrieval scored below the floor,
the matched rule was on cooldown, the model errored — and none of them are
visible from the thread.

Nothing key-shaped reaches it: `trace._safe` strips anything whose name looks
like a credential, and truncates long values. Rows are pruned after
`CHATBOT_EVENT_RETENTION_DAYS` (30 by default).

## Permissions

Mapped onto the roles the platform already has rather than a second RBAC system.
**An agent works conversations; a manager configures the bot** — but only once
somebody has actually been made a manager.

| | Agent | Manager / owner |
| --- | --- | --- |
| `CHATBOT_VIEW`, `CHATBOT_ANALYTICS` | ✓ | ✓ |
| `CONVERSATION_VIEW/REPLY/ASSIGN/TAKEOVER` | ✓ | ✓ |
| `CHATBOT_CREATE/EDIT/DELETE/PUBLISH` | — | ✓ |

That qualifier is the whole subtlety, and getting it wrong locked a workspace
out of its own chatbot. `TeamMember.role` defaults to `AGENT` and the platform
assigns it unprompted: `teams.services.ensure_member` creates a row the first
time anyone opens the inbox, and the team list endpoint bulk-creates one for
*every* active user in the tenant. So an ordinary account becomes an agent by
bookkeeping rather than by anyone's decision, and reading that as "may not
configure the bot" left the owner with no way to create a bot and no way to
grant themselves one.

So an `AGENT` row restricts **only when the roster also holds a `MANAGER`** —
the one thing in it that can only have come from a decision
(`permissions._roster_is_curated`). Until somebody is made a manager on the Team
page, every account in the workspace gets everything, which is what a
one-person or never-configured workspace needs. Somebody with no `TeamMember`
row at all likewise gets everything. A view declaring no permission is
**refused**, not allowed.

### Several numbers

A chatbot is scoped to one WhatsApp number, so a workspace runs **as many bots
as it has numbers** — create one per number and publish each. The only limit is
the partial unique index `one_published_chatbot_per_account`: a number answers
with one bot. Publishing a second onto an occupied number is refused with a
sentence naming the bot already live there, and the number picker marks those
numbers while you choose rather than at publish time.

## Tenant isolation

Every queryset filters on `request.user.tenant` **in the query**, so another
workspace's object is a 404 and never a 403 — a 403 would confirm it exists.
Every id an action names is confirmed against the caller's tenant at save time,
in one place (`actions.validate`), because the alternative is one forgotten
filter away from a cross-tenant send.

## Calling your own API

The `CALL_API` action is the most dangerous thing here, and it is small on
purpose. A URL typed into a form is fetched *by this deployment's servers*.

- **https only.**
- **The resolved address is checked, not the hostname** — a denylist of
  "localhost" is defeated by any DNS record pointing there.
- **Every** address the name resolves to must be public, not merely the first.
- **The socket is pinned to the vetted IP** with the hostname carried in the Host
  header and TLS SNI, which closes the DNS-rebind window between checking and
  connecting.
- **No redirects**, an 8-second timeout, and a 64 KB response cap.

The response becomes bounded session **variables**, never instructions to the
model.

## Prompt injection

Customer text reaches the system prompt in one place: variables a `QUESTION`
node stored. They arrive fenced and explicitly labelled as quoted user input,
and the fence markers are stripped from the values so it cannot be closed from
inside.

That is defence in depth, not the defence. The load-bearing property is
structural: **a model cannot reach an action.** Actions come from the published
version; tools are gated by `ai.tools.granted`; knowledge-only mode still
applies. A successful injection costs a badly worded reply and nothing more.

## Configuration

The chatbot has no credentials of its own. The model, key and provider are the
AI agent's; the number is the WhatsApp account's. The only setting is retention:

```env
CHATBOT_EVENT_RETENTION_DAYS=30          # 0 keeps the debug trail forever
CHATBOT_SESSION_SWEEP_INTERVAL_SECONDS=900
CHATBOT_EVENT_PRUNE_INTERVAL_SECONDS=86400
```

Both sweeps run from `celery-beat`. Without the worker and beat running, nothing
is answered and nothing is pruned.

## API

```
GET    /api/v1/chatbot/overview/                    headline figures
GET    /api/v1/chatbot/capabilities/                what each number can deliver

GET    /api/v1/chatbot/bots/                        list
POST   /api/v1/chatbot/bots/                        create
GET    /api/v1/chatbot/bots/{id}/                   read
PATCH  /api/v1/chatbot/bots/{id}/                   update
DELETE /api/v1/chatbot/bots/{id}/                   delete
POST   /api/v1/chatbot/bots/{id}/publish/           freeze and go live
POST   /api/v1/chatbot/bots/{id}/pause/
POST   /api/v1/chatbot/bots/{id}/resume/
POST   /api/v1/chatbot/bots/{id}/archive/
POST   /api/v1/chatbot/bots/{id}/rollback/          {"version_id": n}
GET    /api/v1/chatbot/bots/{id}/versions/
GET|PUT /api/v1/chatbot/bots/{id}/flow/             the draft graph
GET|PUT /api/v1/chatbot/bots/{id}/working-hours/
POST   /api/v1/chatbot/bots/{id}/test-rules/        {"message": "..."} — sends nothing
GET    /api/v1/chatbot/bots/{id}/analytics/

GET/POST/PATCH/DELETE  /api/v1/chatbot/rules/
POST   /api/v1/chatbot/rules/reorder/               {"order": [{"id":1,"priority":10}]}
GET/POST/PATCH/DELETE  /api/v1/chatbot/assets/

GET    /api/v1/chatbot/conversations/               ?bucket=bot|human|resolved|active
GET    /api/v1/chatbot/conversations/{id}/
GET    /api/v1/chatbot/conversations/{id}/messages/
POST   /api/v1/chatbot/conversations/{id}/reply/
POST   /api/v1/chatbot/conversations/{id}/takeover/
POST   /api/v1/chatbot/conversations/{id}/resume-bot/
POST   /api/v1/chatbot/conversations/{id}/assign/
POST   /api/v1/chatbot/conversations/{id}/close/    reopen/ · resolve/
POST   /api/v1/chatbot/conversations/{id}/mark-read/ · mark-unread/
GET    /api/v1/chatbot/conversations/{id}/trace/    the debug trail
GET    /api/v1/chatbot/conversations/{id}/handoffs/
```

**There is no new webhook.** Inbound WhatsApp traffic already arrives at
`/api/v1/whatsapp/webhook/<account_id>/`, which is signature-verified and
deduplicated; a second inbound path would be a second thing to secure for no
benefit.

## Verifying a deployment

```bash
docker compose exec backend python manage.py verify_chatbot
```

Walks one message through the running install — the real webhook over real HTTP,
the real worker, the real database — and prints what each stage did. By default
nothing reaches a phone: the send is made against credentials no provider
accepts, so it proves every stage up to and including the provider call and says
so.

To prove the last hop as well, with a really connected number:

```bash
docker compose exec backend python manage.py verify_chatbot \
    --account <id> --live --to +919999999999
```

That really does put a message on that phone, and really does spend a credit.
