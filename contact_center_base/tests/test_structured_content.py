import copy

from odoo.tests.common import TransactionCase

from ..services.dto import DTOValidationError, MessageDTO
from ..services.structured_content import (
    OUTBOUND_SPEC_LIMITS,
    outbound_structured_capabilities,
    validate_outbound_structured_capabilities,
    validate_outbound_structured_content,
)


def outbound_specs(*kinds):
    """A synthetic provider exercising the neutral contract without a Wuz dependency."""
    result = {}
    for kind in kinds:
        spec = {name: bounds[1] for name, bounds in OUTBOUND_SPEC_LIMITS[kind].items()}
        has_body = kind in {"buttons", "list"}
        spec.update(
            body_mode="required" if has_body else "none",
            max_body_length=4096 if has_body else 0,
        )
        if kind == "buttons":
            spec["action_types"] = ["reply", "url", "phone"]
        if kind == "location":
            spec["allow_live"] = False
        result[kind] = spec
    return result


class TestStructuredContent(TransactionCase):
    def test_cards_round_trip_without_provider_payloads(self):
        cards = [
            {
                "type": "buttons",
                "buttons": [{"type": "reply", "title": "Sim", "id": "yes"}],
            },
            {
                "type": "list",
                "button_text": "Escolher",
                "sections": [
                    {"title": "Opções", "rows": [{"id": "one", "title": "Um"}]}
                ],
            },
            {
                "type": "contacts",
                "contacts": [
                    {
                        "name": "Ana",
                        "phones": ["+55 (11) 99999-9999"],
                        "emails": ["ana@example.invalid"],
                    }
                ],
            },
            {"type": "location", "latitude": 0, "longitude": -46.6, "live": False},
            {"type": "selection", "id": "one", "title": "Um"},
            {
                "type": "shared",
                "items": [
                    {"kind": "story", "title": "Story indisponível"},
                    {"kind": "post", "url": "https://www.instagram.com/p/example/"},
                ],
            },
        ]
        for card in cards:
            with self.subTest(card=card["type"]):
                dto = MessageDTO(structured_content=card)
                self.assertEqual(
                    MessageDTO.from_dict(dto.to_dict()).structured_content, card
                )
        self.assertEqual(
            MessageDTO.from_dict({"text": "old snapshot"}).structured_content, {}
        )

    def test_rejects_invalid_actions_and_unbounded_collections(self):
        button = {"type": "reply", "title": "Sim", "id": "yes"}
        invalid = [
            {
                "type": "buttons",
                "buttons": [dict(button, id=str(i)) for i in range(26)],
            },
            {"type": "buttons", "buttons": [button, button]},
            {
                "type": "buttons",
                "buttons": [{"type": "execute", "title": "Run", "code": "x"}],
            },
            {"type": "buttons", "buttons": [{"type": [], "title": "Run"}]},
            {"type": "contacts", "contacts": [{"name": "A\r\nFN:B"}]},
            {"type": "location", "latitude": float("nan"), "longitude": 1},
            {"type": "location", "latitude": True, "longitude": 1},
            {"type": "location", "latitude": 91, "longitude": 1},
            {"type": "location", "latitude": 10**400, "longitude": 1},
            {"type": "selection", "id": "x", "title": "X", "provider_payload": {}},
            {"type": "shared", "items": [{"kind": []}]},
            {
                "type": "contacts",
                "contacts": [
                    {"name": "A", "emails": ["a" * 240 + "@x.co"] * 20}
                    for _index in range(50)
                ],
            },
        ]
        for card in invalid:
            with self.subTest(card=card), self.assertRaises(DTOValidationError):
                MessageDTO(structured_content=card)

    def test_links_and_contact_fields_cannot_smuggle_credentials_or_actions(self):
        card = {"type": "shared", "items": [{"kind": "link", "url": ""}]}
        for url in (
            "javascript:alert(1)",
            "https://user:password@example.invalid/",
            "https://example.invalid/?access_token=secret",
            "https://example.invalid/?Signature=secret",
            "https://example.invalid:8443/",
            "https://example.invalid/\\evil",
            "https://example.invalid/ x",
        ):
            value = copy.deepcopy(card)
            value["items"][0]["url"] = url
            with self.subTest(url=url), self.assertRaises(DTOValidationError):
                MessageDTO(structured_content=value)
        for key, value in (
            ("phones", "javascript:alert(1)"),
            ("emails", "x@example.invalid?bcc=y@evil.invalid"),
        ):
            with self.subTest(key=key), self.assertRaises(DTOValidationError):
                MessageDTO(
                    structured_content={
                        "type": "contacts",
                        "contacts": [{"name": "A", key: [value]}],
                    }
                )

    def test_list_row_limit_applies_across_sections(self):
        sections = [
            {
                "title": str(index),
                "rows": [
                    {"id": "%s-%s" % (index, row), "title": "R"} for row in range(51)
                ],
            }
            for index in range(2)
        ]
        with self.assertRaises(DTOValidationError):
            MessageDTO(
                structured_content={
                    "type": "list",
                    "button_text": "Open",
                    "sections": sections,
                }
            )

    def test_inbound_card_capacity_is_independent_of_outbound_support(self):
        card = {
            "type": "buttons",
            "buttons": [
                {"type": "reply", "id": str(i), "title": "A" * 40} for i in range(5)
            ],
        }
        self.assertEqual(MessageDTO(structured_content=card).structured_content, card)
        with self.assertRaises(ValueError):
            validate_outbound_structured_content(card, "Choose", {})
        capabilities = outbound_specs("buttons")
        capabilities["buttons"].update(
            max_buttons=5, max_button_title_length=40, max_body_length=2048
        )
        self.assertTrue(
            validate_outbound_structured_content(card, "A" * 1500, capabilities)
        )
        for receive_only in (
            {"type": "selection", "id": "one", "title": "One"},
            {"type": "shared", "items": [{"kind": "story"}]},
        ):
            MessageDTO(structured_content=receive_only)
            with self.assertRaises(ValueError):
                validate_outbound_structured_content(receive_only, "", capabilities)

    def test_reply_only_provider_rejects_other_actions_and_smaller_limits(self):
        capabilities = outbound_specs("buttons")
        capabilities["buttons"].update(
            action_types=["reply"], max_buttons=1, max_button_title_length=5
        )
        good = {
            "type": "buttons",
            "buttons": [{"type": "reply", "id": "yes", "title": "Yes"}],
        }
        self.assertTrue(
            validate_outbound_structured_content(good, "Choose", capabilities)
        )
        for button in (
            {"type": "url", "title": "Open", "url": "https://example.com"},
            {"type": "phone", "title": "Call", "phone": "+5511999999999"},
            {"type": "reply", "title": "Too long", "id": "no"},
        ):
            with self.subTest(button=button), self.assertRaises(ValueError):
                validate_outbound_structured_content(
                    {"type": "buttons", "buttons": [button]}, "Choose", capabilities
                )

    def test_malformed_outbound_specs_fail_closed_without_legacy_fallback(self):
        valid = outbound_specs("buttons")
        invalid = [
            [],
            {"buttons": {}},
            {"selection": {}},
            {"buttons": {**valid["buttons"], "max_buttons": True}},
            {"buttons": {**valid["buttons"], "max_buttons": 26}},
            {"buttons": {**valid["buttons"], "body_mode": "none"}},
            {"buttons": {**valid["buttons"], "action_types": ["reply", "reply"]}},
            {"buttons": {**valid["buttons"], "action_types": ["execute"]}},
            {"buttons": {**valid["buttons"], "unexpected": True}},
        ]
        for specs in invalid:
            with self.subTest(specs=specs):
                with self.assertRaises(ValueError):
                    validate_outbound_structured_capabilities(specs)
                self.assertEqual(
                    outbound_structured_capabilities(
                        {"outbound_structured_content": specs}
                    ),
                    {},
                )
        self.assertEqual(
            outbound_structured_capabilities({"structured_content": ["buttons"]}), {}
        )
        for outer in (None, [], "buttons", True):
            self.assertEqual(outbound_structured_capabilities(outer), {})

    def test_body_and_live_policy_are_outbound_provider_options(self):
        location = {"type": "location", "latitude": 0, "longitude": 0, "live": True}
        MessageDTO(structured_content=location)
        capabilities = outbound_specs("location")
        with self.assertRaises(ValueError):
            validate_outbound_structured_content(location, "", capabilities)
        capabilities["location"].update(
            allow_live=True, body_mode="optional", max_body_length=200
        )
        self.assertTrue(
            validate_outbound_structured_content(location, "Meeting here", capabilities)
        )
        capabilities["location"].update(body_mode="required")
        with self.assertRaises(ValueError):
            validate_outbound_structured_content(location, "", capabilities)
