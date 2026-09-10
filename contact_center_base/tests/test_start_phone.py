import dataclasses

from odoo.tests.common import TransactionCase

from ..services.adapter import UnsupportedEventError
from ..services.dto import AddressDTO, DirectAddressResult, DTOValidationError
from ..services.phone import normalize_start_phone
from .test_dto_adapter import DummyAdapter


class TestStartPhone(TransactionCase):
    def test_brazilian_formatting_and_explicit_international_numbers(self):
        cases = {
            "(11) 98765-4321": "5511987654321",
            "11 98765.4321": "5511987654321",
            "+55 (11) 98765-4321": "5511987654321",
            "0055 11 98765-4321": "5511987654321",
            "5511987654321": "5511987654321",
            "(11) 3456-7890": "551134567890",
            "551134567890": "551134567890",
            "(55) 99999-1234": "5555999991234",
            "(55) 3333-1234": "555533331234",
            "11 8765-4321": "551187654321",
            "11\u00a098765\u202f4321": "5511987654321",
            "+1 (202) 555-0123": "12025550123",
            "001 202 555 0123": "12025550123",
            "+44 20 7946 0018": "442079460018",
            "+683 4000": "6834000",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_start_phone(value), expected)

    def test_ninth_digit_is_never_added_or_removed_by_normalization(self):
        for value in ("551187654321", "5511987654321"):
            self.assertEqual(normalize_start_phone("+%s" % value), value)

    def test_rejects_ambiguous_invalid_and_non_phone_input(self):
        for value in (
            None,
            False,
            True,
            11987654321,
            [],
            {},
            "",
            " ",
            "-",
            "+",
            "98765-4321",
            "3456-7890",
            "(00) 98765-4321",
            "(20) 98765-4321",
            "5511987654321000",
            "1111111111111111",
            "(11) 98765-4321 ramal 4",
            "5511987654321@s.whatsapp.net",
            "5511987654321@lid",
            "https://wa.me/5511987654321",
            "(11)98765-4321/(11)99876-5432",
            "11987654321;11998765432",
            "11987654321\n",
            "1198765\t4321",
            "11987654321\x00",
            "11987654321\x7f",
            "++5511987654321",
            "55+11987654321",
            "+05511987654321",
            "0 (11) 98765-4321",
            "１１９８７６５４３２１",
            "1" * 65,
            "442079460018",
            "+44 (0) 20 7946 0018",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_start_phone(value)

    def test_default_country_must_be_a_real_region(self):
        self.assertEqual(normalize_start_phone("2025550123", "US"), "12025550123")
        for region in (None, False, "", "ZZ", "55", "BR;US"):
            with self.subTest(region=region), self.assertRaises(ValueError):
                normalize_start_phone("11987654321", region)

    def test_direct_start_requires_explicit_provider_support(self):
        adapter = DummyAdapter(self.env)
        self.assertFalse(adapter.supports_direct_conversation_start(None))
        with self.assertRaises(UnsupportedEventError):
            adapter.resolve_direct_address(None, "5511987654321")

    def test_registration_result_is_immutable_and_serializable(self):
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="5511987654321@s.whatsapp.net",
            value_normalized="5511987654321@s.whatsapp.net",
            confidence="protocol",
            resolution_scope="company",
        )
        result = DirectAddressResult(
            state="ready",
            conversation_ref=address.value_normalized,
            addresses=(address,),
        )
        self.assertEqual(result.to_dict()["addresses"], [address.to_dict()])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.state = "not_registered"
        self.assertEqual(
            DirectAddressResult(state="not_registered").to_dict(),
            {
                "state": "not_registered",
                "conversation_ref": "",
                "addresses": [],
            },
        )

    def test_registration_result_rejects_partial_or_untrusted_proofs(self):
        address = AddressDTO(
            namespace="phone",
            value="5511987654321",
            value_normalized="5511987654321",
            confidence="protocol",
        )
        for values in (
            {"state": "unsupported"},
            {"state": "ready"},
            {"state": "not_registered", "conversation_ref": address.value_normalized},
            {
                "state": "ready",
                "conversation_ref": address.value_normalized,
                "addresses": [address],
            },
            {
                "state": "ready",
                "conversation_ref": address.value_normalized,
                "addresses": (address, address),
            },
            {
                "state": "ready",
                "conversation_ref": "different",
                "addresses": (address,),
            },
            {
                "state": "ready",
                "conversation_ref": address.value_normalized,
                "addresses": (address.to_dict(),),
            },
            {
                "state": "ready",
                "conversation_ref": address.value_normalized,
                "addresses": (dataclasses.replace(address, confidence="observed"),),
            },
            {
                "state": "ready",
                "conversation_ref": address.value_normalized,
                "addresses": (dataclasses.replace(address, role="alternate"),),
            },
        ):
            with self.subTest(values=values), self.assertRaises(DTOValidationError):
                DirectAddressResult(**values)
