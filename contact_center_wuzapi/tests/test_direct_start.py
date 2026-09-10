from unittest import mock

import requests

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
)

from ..services.adapter import WuzapiAdapter
from .common import WuzapiCase
from .test_group_metadata import REQUEST_PATCH, FakeResponse

PHONE = "5511987654321"
PN = "%s@s.whatsapp.net" % PHONE
LID = "987654321012345@lid"


def _check(*, query=PHONE, jid=PN, registered=True):
    return FakeResponse(
        200,
        {
            "code": 200,
            "success": True,
            "data": {
                "Users": [{"Query": query, "IsInWhatsapp": registered, "JID": jid}]
            },
        },
    )


def _lid(*, jid=PN, lid=LID):
    return FakeResponse(
        200, {"code": 200, "success": True, "data": {"jid": jid, "lid": lid}}
    )


class TestWuzapiDirectStart(WuzapiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adapter = WuzapiAdapter(cls.env)

    @mock.patch(REQUEST_PATCH)
    def test_exact_registration_and_lid_use_bounded_read_only_requests(self, request):
        responses = [_check(), _lid()]
        request.side_effect = responses
        self.assertTrue(
            self.adapter.supports_direct_conversation_start(self.connection)
        )
        result = self.adapter.resolve_direct_address(self.connection, PHONE)
        self.assertEqual(result.state, "ready")
        self.assertEqual(result.conversation_ref, PN)
        self.assertEqual(
            [address.value_normalized for address in result.addresses], [PN, LID]
        )
        self.assertEqual(result.addresses[0].confidence, "protocol")
        self.assertEqual(result.addresses[0].resolution_scope, "company")
        self.assertEqual(result.addresses[1].resolution_scope, "account")
        calls = request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].args, ("POST", "%s/user/check" % self.connection.wuzapi_base_url)
        )
        self.assertEqual(calls[0].kwargs["json"], {"Phone": [PHONE]})
        self.assertEqual(
            calls[1].args,
            ("GET", "%s/user/lid/%s" % (self.connection.wuzapi_base_url, PN)),
        )
        self.assertIsNone(calls[1].kwargs["json"])
        self.assertNotIn("Content-Type", calls[1].kwargs["headers"])
        for call in calls:
            self.assertEqual(call.kwargs["timeout"], (5, 30))
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertIs(call.kwargs["stream"], True)
        self.assertTrue(all(response.closed for response in responses))

    @mock.patch(REQUEST_PATCH)
    def test_mobile_ninth_digit_requires_exact_query_proof_in_both_directions(
        self, request
    ):
        for query, canonical in (("551187654321", PHONE), (PHONE, "551187654321")):
            with self.subTest(query=query):
                canonical_jid = "%s@s.whatsapp.net" % canonical
                request.side_effect = [
                    _check(query=query, jid=canonical_jid),
                    _lid(jid=canonical_jid),
                ]
                result = self.adapter.resolve_direct_address(self.connection, query)
                self.assertEqual(result.conversation_ref, canonical_jid)
                self.assertEqual(result.addresses[0].resolution_scope, "company")
                self.assertEqual(
                    result.addresses[1].value_normalized, "%s@s.whatsapp.net" % query
                )
                self.assertEqual(result.addresses[1].confidence, "observed")
                self.assertEqual(result.addresses[1].resolution_scope, "account")

    @mock.patch(REQUEST_PATCH)
    def test_lid_only_result_never_claims_a_company_phone(self, request):
        request.return_value = _check(jid=LID)
        result = self.adapter.resolve_direct_address(self.connection, PHONE)
        self.assertEqual(result.conversation_ref, LID)
        self.assertEqual(len(result.addresses), 2)
        self.assertTrue(
            all(address.resolution_scope == "account" for address in result.addresses)
        )
        self.assertEqual(result.addresses[1].value_normalized, PN)
        self.assertEqual(result.addresses[1].confidence, "observed")
        request.assert_called_once()

    @mock.patch(REQUEST_PATCH)
    def test_not_registered_is_not_a_transport_failure_or_new_identity(self, request):
        request.return_value = _check(jid="", registered=False)
        result = self.adapter.resolve_direct_address(self.connection, PHONE)
        self.assertEqual(result.state, "not_registered")
        self.assertFalse(result.conversation_ref)
        self.assertFalse(result.addresses)
        request.assert_called_once()

    @mock.patch(REQUEST_PATCH)
    def test_missing_lid_is_optional_but_other_http_failures_are_not(self, request):
        request.side_effect = [_check(), FakeResponse(404, {"error": "LID not found"})]
        result = self.adapter.resolve_direct_address(self.connection, PHONE)
        self.assertEqual(
            [address.value_normalized for address in result.addresses], [PN]
        )
        for status, exception in (
            (401, ProviderPausedError),
            (403, ProviderPausedError),
            (429, ProviderRateLimitError),
            (500, TransientAdapterError),
            (503, TransientAdapterError),
            (302, AdapterError),
        ):
            with self.subTest(status=status):
                response = FakeResponse(
                    status,
                    {"error": "opaque provider response"},
                    headers={"Retry-After": "23"},
                )
                request.side_effect = [_check(), response]
                with self.assertRaises(exception) as caught:
                    self.adapter.resolve_direct_address(self.connection, PHONE)
                self.assertTrue(response.closed)
                if status in (429, 500, 503):
                    self.assertEqual(caught.exception.retry_after_seconds, 23)

    @mock.patch(REQUEST_PATCH)
    def test_registration_http_failure_is_never_not_registered(self, request):
        for status, exception in (
            (404, AdapterError),
            (401, ProviderPausedError),
            (429, ProviderRateLimitError),
            (500, TransientAdapterError),
        ):
            with self.subTest(status=status):
                request.return_value = FakeResponse(
                    status, {"error": "opaque provider response"}
                )
                with self.assertRaises(exception):
                    self.adapter.resolve_direct_address(self.connection, PHONE)

    @mock.patch(REQUEST_PATCH)
    def test_timeout_and_oversized_responses_fail_closed(self, request):
        request.side_effect = requests.Timeout("not logged")
        with self.assertRaises(TransientAdapterError):
            self.adapter.resolve_direct_address(self.connection, PHONE)
        request.side_effect = None
        response = _check()
        response.headers["Content-Length"] = str(33 * 1024)
        request.return_value = response
        with self.assertRaises(AdapterError):
            self.adapter.resolve_direct_address(self.connection, PHONE)
        self.assertTrue(response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_invalid_query_flag_and_unrelated_recipient_are_rejected(self, request):
        for response in (
            _check(query="5511999999999"),
            _check(query="+%s" % PHONE),
            _check(registered="true"),
            _check(registered=1),
            _check(jid="5511999999999@s.whatsapp.net"),
            _check(jid="5521987654321@s.whatsapp.net"),
            _check(jid="5511987654321:1@s.whatsapp.net"),
            _check(jid="5511987654321@c.us"),
            _check(jid="123456@g.us"),
            _check(jid="https://example.invalid"),
            _check(jid=""),
            _check(jid={}),
            _check(jid="1" * 21 + "@lid"),
            _check(jid=LID, registered=False),
        ):
            with self.subTest(response=response._payload):
                request.return_value = response
                with self.assertRaises(AdapterError):
                    self.adapter.resolve_direct_address(self.connection, PHONE)

    @mock.patch(REQUEST_PATCH)
    def test_ninth_digit_rule_excludes_fixed_lines_foreign_and_changed_ddd(
        self, request
    ):
        for query, jid in (
            ("551134567890", "5511934567890@s.whatsapp.net"),
            ("12025550123", "192025550123@s.whatsapp.net"),
            ("551187654321", "5521987654321@s.whatsapp.net"),
        ):
            request.return_value = _check(query=query, jid=jid)
            with self.subTest(query=query), self.assertRaises(AdapterError):
                self.adapter.resolve_direct_address(self.connection, query)

    @mock.patch(REQUEST_PATCH)
    def test_registration_result_count_and_lid_mapping_are_strict(self, request):
        for users in (
            None,
            {},
            [],
            [_check()._payload["data"]["Users"][0]] * 2,
            [False],
        ):
            request.return_value = FakeResponse(
                200, {"code": 200, "success": True, "data": {"Users": users}}
            )
            with self.subTest(users=users), self.assertRaises(AdapterError):
                self.adapter.resolve_direct_address(self.connection, PHONE)
        for response in (
            _lid(jid="other@s.whatsapp.net"),
            _lid(lid=PN),
            _lid(lid="123:1@lid"),
            _lid(lid=""),
            _lid(lid={}),
        ):
            request.side_effect = [_check(), response]
            with self.subTest(lid=response._payload), self.assertRaises(AdapterError):
                self.adapter.resolve_direct_address(self.connection, PHONE)

    @mock.patch(REQUEST_PATCH)
    def test_invalid_input_is_rejected_before_provider_io(self, request):
        for value in (
            None,
            False,
            5511987654321,
            "",
            "+%s" % PHONE,
            PN,
            "(11) 98765-4321",
            "1" * 16,
        ):
            with self.subTest(value=value), self.assertRaises(AdapterError):
                self.adapter.resolve_direct_address(self.connection, value)
        request.assert_not_called()
