# Lead tools: number filter and group grabber

Two tools that answer a question only a phone can answer, through one of the
workspace's QR-linked numbers:

* **Number filter** -- which numbers on a list are registered on WhatsApp.
* **Group grabber** -- who is in the WhatsApp groups the linked number belongs to.

Both live in `apps/leadtools` behind `/api/v1/leadtools/`, both are plan
features (`number_filter`, `group_grabber` -- see [ENTITLEMENTS.md](ENTITLEMENTS.md)),
and both are unavailable on Cloud API numbers: Meta's API has no contact
lookup and no group surface, by design. The screens say so rather than
offering those numbers.

## What they produce, and what they do not

Everything either tool saves goes through the ordinary contact importer
(`ContactImport`). That is the whole reason the importer exists as one thing:
the plan's contact ceiling, the never-overwrite-a-corrected-name rule and the
consent model are enforced there, and a second write path would be a second
implementation of three rules a customer would notice being applied
inconsistently.

**Neither tool records consent.** A number being on WhatsApp is a fact about
the number, and membership of a group is a fact about the group. The contacts
are created with `marketing_consent = UNKNOWN`, the same as a pasted list, so
a workspace that requires consent before marketing will skip them until
somebody records where consent actually came from. See [CONSENT.md](CONSENT.md).

## Number filter

`POST /leadtools/number-checks/` with either a pasted list (`source: PASTE`,
`text`, optional `default_country_code`) or the workspace's contacts
(`source: CONTACTS`, optional `contact_group`). The rows are created at once;
the asking happens on the worker (`run_number_check`) in batches of
`LEADTOOLS_NUMBER_CHECK_BATCH_SIZE` with `LEADTOOLS_NUMBER_CHECK_PAUSE_SECONDS`
between them. A linked phone that fires thousands of lookups back to back
looks like the scraping WhatsApp bans numbers for, so the pause is not
optional and the per-check ceiling (`LEADTOOLS_NUMBER_CHECK_MAX_NUMBERS`) is
deliberately a refusal rather than a warning.

Each row ends `ON_WHATSAPP`, `NOT_ON_WHATSAPP` or `ERROR`; contacts on the
account are stamped with `whatsapp_status` and `whatsapp_checked_at` by phone
number, so the contact list can be filtered on it. A provider failure
part-way through finishes the check as `FAILED` with the reason and leaves the
unasked rows `PENDING`, which is exactly what they are.

* `GET .../{id}/entries/?result=ON_WHATSAPP` -- paged rows.
* `GET .../{id}/export/?result=...` -- CSV, formula-safe.
* `POST .../{id}/save/ {"result": "ON_WHATSAPP", "group_name": "..."}` -- into a
  contact group, through the importer.

## Group grabber

`GET /leadtools/groups/?account=<id>` lists every group the number is in.
`POST /leadtools/group-grabs/ {"whatsapp_account", "group_ids", "contact_group_name"}`
reads the members of the chosen groups (at most `LEADTOOLS_GROUP_GRAB_MAX_GROUPS`)
and hands them to the importer as one CSV: one import and one contact group
per grab, each person once, with the WhatsApp group they were read from kept
as the custom field `WhatsApp group`.

Members WhatsApp exposes only by an anonymous id (a "LID", with no phone
number) are counted as `members_hidden` and never guessed at. The linked
number itself is excluded.

## The bridge side

Three routes on `qr-bridge`, documented in `backend/apps/whatsapp/providers/qr_bridge.py`:

| Route | Baileys call |
| --- | --- |
| `POST /sessions/{id}/numbers/check` | `onWhatsApp()`, 50 numbers per query, 300 ms apart |
| `GET /sessions/{id}/groups` | `groupFetchAllParticipating()` |
| `POST /sessions/{id}/groups/members` | `groupMetadata()` per group |

A session without a live socket answers 409 `{"code": "not_connected"}`, which
Django reports as the number needing to be reconnected from Devices.

## Capabilities

`WhatsAppProvider.capabilities()` carries two new keys, `number_check` and
`groups`. Only `WhatsAppQRBridgeProvider` says True; `ConnectionManager.supports(account, ...)`
is what the views ask before offering either tool, so a deployment with no
bridge configured refuses cleanly instead of failing inside a task.
