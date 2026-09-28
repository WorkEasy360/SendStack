"""
The permission vocabulary of the public API.

One flat list of `resource:action` strings. A key holds the scopes it was
created with and nothing else -- there is no hierarchy, no implication, and no
wildcard, because every one of those is a way for a key to end up with a
permission nobody deliberately gave it.

**Default deny.** `HasApiScope` refuses any view whose `required_scopes` are
not all present on the key, and a view with no `required_scopes` declared is a
programming error rather than a public endpoint (see permissions.py). A scope
added to this list is inert until some view asks for it, and a scope removed
from a key takes effect on the key's very next request.
"""

MESSAGES_SEND = "messages:send"
MESSAGES_READ = "messages:read"
MESSAGES_STATUS = "messages:status"
CAMPAIGNS_CREATE = "campaigns:create"
CAMPAIGNS_READ = "campaigns:read"
CAMPAIGNS_SEND = "campaigns:send"
CAMPAIGNS_CANCEL = "campaigns:cancel"
TEMPLATES_READ = "templates:read"
TEMPLATES_WRITE = "templates:write"
CONTACTS_READ = "contacts:read"
CONTACTS_WRITE = "contacts:write"
MEDIA_UPLOAD = "media:upload"
WEBHOOKS_READ = "webhooks:read"
WEBHOOKS_MANAGE = "webhooks:manage"
ACCOUNTS_READ = "accounts:read"
REPORTS_READ = "reports:read"

#: Every scope, with the sentence shown next to its checkbox in the dashboard.
#: Ordered by resource so the create-key screen reads as groups.
SCOPES = {
    MESSAGES_SEND: "Send messages and media to a recipient.",
    MESSAGES_READ: "List and read messages.",
    MESSAGES_STATUS: "Read delivery status for a single message.",
    CAMPAIGNS_CREATE: "Create campaigns.",
    CAMPAIGNS_READ: "List campaigns and read their results.",
    CAMPAIGNS_SEND: "Start, pause and resume a campaign.",
    CAMPAIGNS_CANCEL: "Cancel a running campaign.",
    TEMPLATES_READ: "List and read message templates.",
    TEMPLATES_WRITE: "Create, update and delete message templates.",
    CONTACTS_READ: "List and read contacts.",
    CONTACTS_WRITE: "Create and update contacts.",
    MEDIA_UPLOAD: "Upload media for use in messages.",
    WEBHOOKS_READ: "List webhook endpoints and their delivery history.",
    WEBHOOKS_MANAGE: "Create, update and delete webhook endpoints.",
    ACCOUNTS_READ: "List the connected WhatsApp accounts available to send from.",
    REPORTS_READ: "Read aggregate delivery reports.",
}

ALL_SCOPES = tuple(SCOPES)

#: The read-only subset, offered as one click in the dashboard because
#: "let this integration look but not touch" is the commonest key anybody
#: creates and hand-ticking eight boxes gets it wrong.
READ_ONLY_SCOPES = (
    MESSAGES_READ, MESSAGES_STATUS, CAMPAIGNS_READ, TEMPLATES_READ,
    CONTACTS_READ, WEBHOOKS_READ, ACCOUNTS_READ, REPORTS_READ,
)


def validate(scopes) -> list[str]:
    """
    Normalise a caller-supplied scope list, or raise ValueError naming the bad one.

    Deduplicated and returned in the canonical order of SCOPES rather than the
    order they arrived in, so two keys created with the same permissions
    compare equal and the dashboard lists them identically.
    """
    if not isinstance(scopes, (list, tuple)):
        raise ValueError("Scopes must be a list.")
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s): {', '.join(sorted(set(unknown)))}")
    return [s for s in ALL_SCOPES if s in set(scopes)]


# --- The human-readable layer -------------------------------------------
#
# `SCOPES` above is the *contract*: those strings appear in keys, in 403 bodies
# and in the OpenAPI document, and they never change. What follows is the
# *presentation* of the same set, and it exists because "messages:send" next to
# a checkbox asks a customer to guess which of their words maps to ours.
#
# The two are deliberately not merged. A label is prose and is improved freely;
# a scope string is a published identifier and is not. Keeping them in one dict
# would invite somebody to "tidy" a label and silently break every key holding
# that scope. `SCOPE_DETAILS` is derived from `SCOPES`, and the coverage test
# asserts every scope has exactly one entry, so adding a scope without a label
# fails the suite rather than shipping a blank checkbox.

#: Resource -> the section heading the create-key screen prints.
SCOPE_GROUPS = {
    "messages": "Messages",
    "templates": "Templates",
    "contacts": "Contacts",
    "campaigns": "Campaigns",
    "accounts": "Account",
    "reports": "Reports",
    "media": "Media",
    "webhooks": "Webhooks",
}

#: scope -> (short label, the sentence under it, whether ticking it can spend
#: money or reach a customer's phone).
#:
#: `sensitive` is not a second permission check -- the scope is the permission.
#: It drives one thing in the UI: a warning next to the boxes that cost money
#: or delete data, so somebody ticking "everything" at least sees which four
#: of those they were.
SCOPE_DETAILS_RAW = {
    MESSAGES_SEND: (
        "Send WhatsApp messages",
        "Allows this API token to send messages through your connected WhatsApp accounts. "
        "Each message uses one credit.",
        True,
    ),
    MESSAGES_STATUS: (
        "View message status",
        "Look up whether a single message was sent, delivered, read or failed.",
        False,
    ),
    MESSAGES_READ: (
        "View message history",
        "List past messages, including their text and who they went to.",
        False,
    ),
    TEMPLATES_READ: (
        "View templates",
        "List your message templates and read their content and variables.",
        False,
    ),
    TEMPLATES_WRITE: (
        "Create, edit and delete templates",
        "Add new templates, change existing ones, and delete templates no campaign is using.",
        True,
    ),
    CONTACTS_READ: (
        "View contacts",
        "List and search the people in your contact book.",
        False,
    ),
    CONTACTS_WRITE: (
        "Create and edit contacts",
        "Add new contacts and update the details of existing ones. Cannot delete contacts.",
        True,
    ),
    CAMPAIGNS_READ: (
        "View campaigns and reports",
        "List campaigns and read their per-recipient results.",
        False,
    ),
    CAMPAIGNS_CREATE: (
        "Create campaigns",
        "Build a campaign with a list of recipients. Creating one does not start it.",
        True,
    ),
    CAMPAIGNS_SEND: (
        "Start and pause campaigns",
        "Begin sending a campaign, and pause or resume one that is running. "
        "This is what actually sends messages to your audience.",
        True,
    ),
    CAMPAIGNS_CANCEL: (
        "Cancel campaigns",
        "Stop a running campaign for good. Messages already sent cannot be recalled.",
        True,
    ),
    ACCOUNTS_READ: (
        "View WhatsApp account status",
        "See which of your WhatsApp numbers are connected and available to send from.",
        False,
    ),
    REPORTS_READ: (
        "View delivery reports",
        "Read the sent / delivered / read / failed totals for your workspace.",
        False,
    ),
    MEDIA_UPLOAD: (
        "Upload media",
        "Upload images, video and documents so they can be attached to messages.",
        True,
    ),
    WEBHOOKS_READ: (
        "View webhook configuration",
        "List where events are being delivered and whether those deliveries are succeeding.",
        False,
    ),
    WEBHOOKS_MANAGE: (
        "Manage webhook configuration",
        "Add, change and remove the URLs we send your events to.",
        True,
    ),
}

#: The shape the dashboard renders, in the canonical scope order.
SCOPE_DETAILS = [
    {
        "value": scope,
        "label": SCOPE_DETAILS_RAW[scope][0],
        "help": SCOPE_DETAILS_RAW[scope][1],
        "sensitive": SCOPE_DETAILS_RAW[scope][2],
        "group": SCOPE_GROUPS[scope.split(":")[0]],
        # The contract string, still shown -- smaller and second, but shown.
        # A developer reading a 403 that names `messages:send` has to be able
        # to find the box that grants it.
        "scope": scope,
    }
    for scope in ALL_SCOPES
]


def label_for(scope: str) -> str:
    """The plain-English name of one scope, falling back to the scope itself."""
    entry = SCOPE_DETAILS_RAW.get(scope)
    return entry[0] if entry else scope


def labels_for(scope_list) -> list[str]:
    return [label_for(scope) for scope in (scope_list or [])]


#: Named starting points on the create-key screen. Each is a *job somebody is
#: trying to do*, not a permission level, because "what do I tick to send an
#: order confirmation?" is the actual question and a list of sixteen checkboxes
#: does not answer it.
#:
#: `send_messages` is the default the screen opens on, and it is deliberately
#: the smallest set that makes the commonest integration work: send, and check
#: what happened. It grants no read access to the contact book, no template
#: editing and no campaign control.
SCOPE_PRESETS = [
    {
        "id": "send_messages",
        "label": "Send messages",
        "description": "Send WhatsApp messages from your own software and check their delivery status.",
        "scopes": [MESSAGES_SEND, MESSAGES_STATUS, TEMPLATES_READ, ACCOUNTS_READ],
        "recommended": True,
    },
    {
        "id": "read_only",
        "label": "Read only",
        "description": "Look at everything, change nothing. Safe for dashboards and reporting tools.",
        "scopes": list(READ_ONLY_SCOPES),
        "recommended": False,
    },
    {
        "id": "campaigns",
        "label": "Run campaigns",
        "description": "Create, start and monitor bulk campaigns, plus everything the send preset covers.",
        "scopes": [
            MESSAGES_SEND, MESSAGES_STATUS, TEMPLATES_READ, ACCOUNTS_READ,
            CONTACTS_READ, CONTACTS_WRITE,
            CAMPAIGNS_CREATE, CAMPAIGNS_READ, CAMPAIGNS_SEND, CAMPAIGNS_CANCEL,
            REPORTS_READ,
        ],
        "recommended": False,
    },
]

#: What a fresh create-key form starts with. Least privilege that still does
#: the job somebody came here for -- not the empty set, which trains people to
#: click "select all", and not everything, which is how keys end up able to
#: delete templates because somebody wanted to send an OTP.
DEFAULT_SCOPES = list(SCOPE_PRESETS[0]["scopes"])
