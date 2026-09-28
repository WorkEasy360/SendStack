"""
The list and poll content types, and the degradation that makes them portable.

The tests that matter most here are not the validation ones -- those merely
keep bad payloads out. They are the ones pinning `as_options` to the
``core.buttons`` item shape, because that equivalence is the entire reason a
menu works on a connection that cannot draw menus: the same numbered fallback,
the same PendingButtonPrompt, and the same automation matcher serve all three
kinds of question. If that shape drifts, lists and polls stop being answerable
on QR numbers and nothing else in the system notices.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.buttons import (
    CALL,
    REPLY,
    URL,
    as_numbered_options,
    body_with_text_buttons,
    button_kind,
    clean_buttons,
    ensure_ids,
    numbered_options,
    plan_delivery,
)
from core.interactive import (
    BUTTON,
    LIST,
    MAX_LIST_ROWS,
    MAX_POLL_OPTIONS,
    POLL,
    TEXT,
    as_options,
    clean_list_content,
    clean_poll_content,
    content_kind,
    exclusive_content_error,
    list_as_text,
    list_rows,
    poll_as_text,
)


def _list(**overrides):
    content = {
        "header": "Choose a service",
        "button": "View services",
        "sections": [
            {
                "title": "Services",
                "rows": [
                    {"title": "Web development", "description": "Sites and apps"},
                    {"title": "WhatsApp marketing"},
                ],
            }
        ],
    }
    content.update(overrides)
    return content


class ListValidationTests(SimpleTestCase):
    def test_empty_content_is_not_a_list(self):
        for empty in (None, "", {}, []):
            with self.subTest(value=empty):
                self.assertEqual(clean_list_content(empty), {})

    def test_rows_get_ids_derived_from_their_titles(self):
        cleaned = clean_list_content(_list())

        self.assertEqual(
            [row["id"] for row in list_rows(cleaned)],
            ["web_development", "whatsapp_marketing"],
        )

    def test_a_supplied_row_id_is_never_rewritten(self):
        """
        A row id is what automation matches on. Deriving one over the top of
        the author's would silently unhook every rule pointing at it.
        """
        cleaned = clean_list_content(
            _list(sections=[{"rows": [{"id": "svc_web", "title": "Something else entirely"}]}])
        )

        self.assertEqual(list_rows(cleaned)[0]["id"], "svc_web")

    def test_two_rows_cannot_share_an_id(self):
        with self.assertRaises(ValidationError) as caught:
            clean_list_content(
                _list(sections=[{"rows": [{"id": "a", "title": "One"}, {"id": "a", "title": "Two"}]}])
            )

        self.assertIn("each row needs its own", str(caught.exception))

    def test_the_row_ceiling_counts_every_section(self):
        """
        WhatsApp caps the whole menu at ten rows, not ten per section -- so a
        three-section menu of five rows each is refused by Meta. Catching it
        on save is what stops a campaign being launched against it.
        """
        sections = [
            {"title": f"S{index}", "rows": [{"title": f"Row {index}-{row}"} for row in range(4)]}
            for index in range(3)
        ]

        with self.assertRaises(ValidationError) as caught:
            clean_list_content(_list(sections=sections))

        self.assertIn(str(MAX_LIST_ROWS), str(caught.exception))

    def test_a_section_needs_a_title_only_when_there_is_more_than_one(self):
        self.assertEqual(
            clean_list_content(_list(sections=[{"rows": [{"title": "Only"}]}]))["sections"][0]["title"], "",
        )

        with self.assertRaises(ValidationError):
            clean_list_content(
                _list(sections=[{"rows": [{"title": "One"}]}, {"rows": [{"title": "Two"}]}])
            )

    def test_a_button_label_is_supplied_when_the_author_omits_it(self):
        """A list with no call to action is a message the recipient cannot open."""
        cleaned = clean_list_content(_list(button=""))

        self.assertTrue(cleaned["button"])

    def test_an_over_long_row_title_is_refused_with_the_limit_named(self):
        with self.assertRaises(ValidationError) as caught:
            clean_list_content(_list(sections=[{"rows": [{"title": "x" * 40}]}]))

        self.assertIn("24", str(caught.exception))

    def test_a_list_needs_at_least_one_section_with_one_row(self):
        for bad in ({"sections": []}, {"sections": [{"title": "Empty", "rows": []}]}):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    clean_list_content(bad)


class PollValidationTests(SimpleTestCase):
    def test_a_poll_normalises_to_question_options_and_the_multi_switch(self):
        cleaned = clean_poll_content({
            "question": "Which service?",
            "options": ["Web", {"title": "Marketing"}],
            "allow_multiple": True,
        })

        self.assertEqual(cleaned["question"], "Which service?")
        self.assertEqual(cleaned["options"], ["Web", "Marketing"])
        self.assertTrue(cleaned["allow_multiple"])

    def test_duplicate_options_are_refused_rather_than_de_duplicated(self):
        """
        Two identical answers make the result meaningless, and quietly
        dropping one is worse than saying so.
        """
        with self.assertRaises(ValidationError) as caught:
            clean_poll_content({"question": "Pick", "options": ["Yes", "yes"]})

        self.assertIn("repeats", str(caught.exception))

    def test_a_poll_needs_two_options_and_allows_no_more_than_twelve(self):
        with self.assertRaises(ValidationError):
            clean_poll_content({"question": "Pick", "options": ["Only one"]})

        with self.assertRaises(ValidationError) as caught:
            clean_poll_content({"question": "Pick", "options": [f"Option {i}" for i in range(13)]})

        self.assertIn(str(MAX_POLL_OPTIONS), str(caught.exception))

    def test_a_poll_needs_a_question(self):
        with self.assertRaises(ValidationError):
            clean_poll_content({"question": "", "options": ["A", "B"]})


class DegradationTests(SimpleTestCase):
    """
    The equivalence that makes a menu or a poll portable.

    `as_options` must produce exactly what `core.buttons` produces, because
    every downstream reader -- the numbered renderer, PendingButtonPrompt, and
    automation's `_match_option` -- is that module's.
    """

    def test_list_rows_become_reply_buttons_carrying_their_own_ids(self):
        options = as_options(list_content=clean_list_content(_list()))

        self.assertEqual(
            options,
            [
                {"type": REPLY, "id": "web_development", "title": "Web development", "url": ""},
                {"type": REPLY, "id": "whatsapp_marketing", "title": "WhatsApp marketing", "url": ""},
            ],
        )

    def test_poll_options_get_deterministic_ids(self):
        """
        A poll has no author-supplied ids, so they are derived -- and derived
        the same way every time, which is what lets an automation rule point
        at one.
        """
        content = clean_poll_content({"question": "Q", "options": ["Web development", "Other"]})

        first = as_options(poll_content=content)
        second = as_options(poll_content=content)

        self.assertEqual(first, second)
        self.assertEqual([option["id"] for option in first], ["web_development", "other"])

    def test_the_degraded_options_are_numbered_the_way_the_matcher_reads_them(self):
        """
        The rendered numbering and the matcher's positional lookup have to
        agree. They agree by both being derived from the same list, and this
        pins that.
        """
        options = as_options(list_content=clean_list_content(_list()))

        self.assertEqual(
            numbered_options(options), ["1. Web development", "2. WhatsApp marketing"],
        )

    def test_a_degraded_list_keeps_header_footer_and_descriptions(self):
        text = list_as_text("Please pick one.", clean_list_content(_list(footer="Reply anytime")))

        self.assertIn("Choose a service", text)
        self.assertIn("Please pick one.", text)
        self.assertIn("1. Web development", text)
        self.assertIn("Sites and apps", text)
        self.assertIn("Reply anytime", text)

    def test_a_degraded_poll_says_whether_several_answers_are_allowed(self):
        single = poll_as_text("", clean_poll_content({"question": "Q", "options": ["A", "B"]}))
        multiple = poll_as_text(
            "", clean_poll_content({"question": "Q", "options": ["A", "B"], "allow_multiple": True})
        )

        self.assertIn("the option number.", single)
        self.assertIn("separated by commas", multiple)

    def test_no_options_for_content_that_is_neither(self):
        self.assertEqual(as_options(), [])


class ContentKindTests(SimpleTestCase):
    def test_the_kind_is_derived_from_whichever_content_is_present(self):
        self.assertEqual(content_kind({}), TEXT)
        self.assertEqual(content_kind({"buttons": [{"type": REPLY, "id": "a", "title": "A"}]}), BUTTON)
        self.assertEqual(content_kind({"list_content": {"sections": []}}), LIST)
        self.assertEqual(content_kind({"poll_content": {"question": "Q"}}), POLL)

    def test_it_reads_an_object_as_well_as_a_dict(self):
        class Spec:
            buttons = []
            list_content = {}
            poll_content = {"question": "Q"}

        self.assertEqual(content_kind(Spec()), POLL)

    def test_two_interactive_kinds_at_once_are_named_as_the_conflict(self):
        message = exclusive_content_error(buttons=[{"title": "A"}], poll_content={"question": "Q"})

        self.assertIn("not both", message)

    def test_one_kind_is_no_conflict(self):
        self.assertEqual(exclusive_content_error(poll_content={"question": "Q"}), "")


class CallButtonTests(SimpleTestCase):
    """
    A call button is accepted, normalised, and honestly reported as something
    no connection delivers natively -- see the module docstring in core.buttons.
    """

    def test_a_call_button_keeps_its_number_in_e164(self):
        cleaned = clean_buttons([{"type": "CALL", "title": "Call now", "phone": "+91 98765 43210"}])

        self.assertEqual(cleaned[0]["type"], CALL)
        self.assertEqual(cleaned[0]["phone"], "+919876543210")

    def test_a_call_button_without_a_usable_number_is_refused(self):
        for bad in ("", "98765", "not a number"):
            with self.subTest(phone=bad):
                with self.assertRaises(ValidationError):
                    clean_buttons([{"type": "CALL", "title": "Call now", "phone": bad}])

    def test_only_one_call_button_is_allowed(self):
        with self.assertRaises(ValidationError):
            clean_buttons([
                {"type": "CALL", "title": "Sales", "phone": "+919876543210"},
                {"type": "CALL", "title": "Support", "phone": "+919876543211"},
            ])

    def test_a_call_button_sits_beside_reply_buttons(self):
        cleaned = clean_buttons([
            {"type": "REPLY", "title": "Yes"},
            {"type": "CALL", "title": "Call", "phone": "+919876543210"},
        ])

        self.assertEqual([button["type"] for button in cleaned], [REPLY, CALL])

    def test_it_degrades_to_a_labelled_number_rather_than_a_numbered_option(self):
        """
        Nothing to reply *with*: the number is printed bare so the recipient's
        own client turns it into a dial link, and it takes no position in the
        numbering that answers would be matched against.
        """
        buttons = clean_buttons([{"type": "CALL", "title": "Call now", "phone": "+919876543210"}])

        text = as_numbered_options("Need help?", buttons)

        self.assertIn("Call now: +919876543210", text)
        self.assertNotIn("1.", text)


class MixedButtonTests(SimpleTestCase):
    """
    The three button kinds combine in one list, and `plan_delivery` is the one
    place that decides what that costs on the wire.

    These are the tests that matter here: the Cloud API provider and the
    composer's delivery preview both call it, so an author is shown the split
    the sender will make rather than a guess at it. If they ever disagreed, the
    composer would promise a tappable link that arrives as text and nothing
    would notice until a customer did.
    """

    def _mixed(self):
        return clean_buttons([
            {"type": "REPLY", "title": "Yes"},
            {"type": "REPLY", "title": "No"},
            {"type": "URL", "title": "Shop now", "url": "https://example.test/shop"},
            {"type": "CALL", "title": "Call us", "phone": "+919876543210"},
        ])

    def test_all_three_kinds_save_together(self):
        self.assertEqual(
            [button["type"] for button in self._mixed()], [REPLY, REPLY, URL, CALL]
        )

    def test_the_per_kind_limits_still_hold(self):
        for extra in (
            {"type": "URL", "title": "Also", "url": "https://example.test/two"},
            {"type": "CALL", "title": "Also", "phone": "+919876543211"},
        ):
            with self.subTest(kind=extra["type"]):
                with self.assertRaises(ValidationError):
                    clean_buttons([extra, dict(extra)])

    def test_four_reply_buttons_are_still_refused_alongside_a_link(self):
        with self.assertRaises(ValidationError):
            clean_buttons(
                [{"title": f"Option {n}"} for n in range(4)]
                + [{"type": "URL", "title": "Shop", "url": "https://example.test"}]
            )

    def test_reply_buttons_keep_the_tap_and_the_rest_become_text(self):
        tappable, spelled_out = plan_delivery(self._mixed())

        self.assertEqual([button["title"] for button in tappable], ["Yes", "No"])
        self.assertEqual([button["type"] for button in spelled_out], [URL, CALL])

    def test_a_lone_link_still_takes_the_interactive_envelope(self):
        buttons = clean_buttons([
            {"type": "URL", "title": "Shop now", "url": "https://example.test/shop"},
            {"type": "CALL", "title": "Call us", "phone": "+919876543210"},
        ])

        tappable, spelled_out = plan_delivery(buttons)

        self.assertEqual(button_kind(tappable), URL)
        self.assertEqual([button["type"] for button in spelled_out], [CALL])

    def test_a_call_button_is_never_tappable(self):
        buttons = clean_buttons([{"type": "CALL", "title": "Call us", "phone": "+919876543210"}])

        tappable, spelled_out = plan_delivery(buttons)

        self.assertEqual(tappable, [])
        self.assertEqual(spelled_out, buttons)

    def test_the_spelled_out_buttons_are_appended_as_their_own_paragraph(self):
        """
        A blank line, not a space: gluing a URL onto the author's closing
        sentence is how a link stops being recognised as one.
        """
        _, spelled_out = plan_delivery(self._mixed())

        text = body_with_text_buttons("Coming on Friday?", spelled_out)

        self.assertEqual(
            text, "Coming on Friday?\n\nShop now: https://example.test/shop\nCall us: +919876543210"
        )

    def test_a_connection_with_no_buttons_at_all_numbers_only_the_repliable_ones(self):
        text = as_numbered_options("Coming on Friday?", self._mixed())

        self.assertIn("1. Yes", text)
        self.assertIn("2. No", text)
        self.assertIn("Shop now: https://example.test/shop", text)
        self.assertIn("Call us: +919876543210", text)
        # The link and the call button take no number, because there is nothing
        # to answer them with -- and `resolve_option_reply` numbers the same
        # list the same way when the reply comes back.
        self.assertNotIn("3.", text)


class EnsureIdsTests(SimpleTestCase):
    """
    The delivery-time backstop for a reply button's id.

    ``clean_buttons`` fills these in on the authoring path, so everything that
    came through a serializer already has one. This is for everything that did
    not -- a fixture, a data migration, the admin, a caller assembling an action
    dict by hand -- because the id is what comes back when the button is tapped,
    and one that goes out without it is an option no rule can ever match.
    """

    def test_a_missing_id_is_derived_from_the_label(self):
        filled = ensure_ids([{"type": REPLY, "title": "Talk to Agent"}])

        self.assertEqual(filled[0]["id"], "talk_to_agent")

    def test_an_id_already_set_is_never_rewritten(self):
        """Rules store these, so rewriting one would silently unhook its rule."""
        filled = ensure_ids([{"type": REPLY, "id": "keep_me", "title": "Pricing"}])

        self.assertEqual(filled[0]["id"], "keep_me")

    def test_a_derived_id_does_not_collide_with_one_already_taken(self):
        filled = ensure_ids([
            {"type": REPLY, "id": "pricing", "title": "Plans"},
            {"type": REPLY, "title": "Pricing"},
        ])

        self.assertEqual(filled[0]["id"], "pricing")
        self.assertNotEqual(filled[1]["id"], "pricing")
        self.assertTrue(filled[1]["id"])

    def test_two_untitled_buttons_still_get_distinct_ids(self):
        filled = ensure_ids([{"type": REPLY, "title": ""}, {"type": REPLY, "title": ""}])

        self.assertNotEqual(filled[0]["id"], filled[1]["id"])

    def test_link_and_call_buttons_are_left_alone(self):
        """Neither sends anything back, so neither has an id to match on."""
        filled = ensure_ids([
            {"type": URL, "title": "Shop", "url": "https://example.test"},
            {"type": CALL, "title": "Call us", "phone": "+919876543210"},
        ])

        self.assertEqual([button.get("id", "") for button in filled], ["", ""])

    def test_it_agrees_with_clean_buttons_on_the_same_input(self):
        """
        The two derive ids for the same reason and must not drift: a button
        authored through a serializer and the same button written straight to
        the database have to end up matching the same rule.
        """
        raw = [{"type": REPLY, "title": "Pricing"}, {"type": REPLY, "title": "Talk to Agent"}]

        self.assertEqual(
            [button["id"] for button in ensure_ids(raw)],
            [button["id"] for button in clean_buttons(raw)],
        )

    def test_it_is_idempotent(self):
        once = ensure_ids([{"type": REPLY, "title": "Pricing"}])

        self.assertEqual(ensure_ids(once), once)

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(ensure_ids([]), [])
        self.assertEqual(ensure_ids(None), [])
