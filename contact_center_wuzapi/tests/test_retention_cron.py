"""Daily orchestration with real WuzAPI eligibility and rollback-capable cursors."""

import copy
import datetime
import threading
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.sql_db import TestCursor

from odoo.addons.contact_center_base.models import retention, retention_ingress
from odoo.addons.contact_center_base.models.retention import _service_context
from odoo.addons.contact_center_base.tests import test_retention as retention_fixtures

from .common import WuzapiCase


class TestDailyGroupRetention(WuzapiCase):
    _group = retention_fixtures.TestHistoryRetention._group
    _message = retention_fixtures.TestHistoryRetention._message
    _indexed_event = retention_fixtures.TestHistoryRetention._indexed_event

    def setUp(self):
        super().setUp()
        self.service = self.env["contact.center.retention"]
        self.members = self.agent
        self.now = fields.Datetime.now()
        self.old = self.now - datetime.timedelta(days=31)
        self.new = self.now - datetime.timedelta(days=1)
        self.account.with_context(**_service_context()).write(
            {"retention_enabled": True, "retention_days": 30}
        )
        self.channel, self.binding = self._group()
        self.scope = [self.account.id]
        # Native TestCursor commits/releases its own savepoint, while closing an
        # uncommitted batch rolls back that savepoint. Never commit the test case.
        cursor = mock.patch.object(
            self.registry, "cursor", new=lambda: TestCursor(self.cr, threading.RLock())
        )
        cursor.start()
        self.addCleanup(cursor.stop)
        binding_type = type(self.binding)
        search = binding_type.search

        def scoped_search(model, domain, *args, **kwargs):
            return search(
                model,
                list(domain) + [("account_id", "in", self.scope)],
                *args,
                **kwargs
            )

        scope = mock.patch.object(binding_type, "search", new=scoped_search)
        scope.start()
        self.addCleanup(scope.stop)

    def _run(self):
        self.env.flush_all()
        with mock.patch.object(
            self.cr, "commit", side_effect=AssertionError("Caller commit")
        ), mock.patch.object(
            self.cr, "rollback", side_effect=AssertionError("Caller rollback")
        ), mock.patch.object(
            fields.Datetime, "now", return_value=self.now
        ):
            try:
                return self.service._cron_purge()
            finally:
                self.env.invalidate_all()

    def _other_account(self, adapter="wuzapi"):
        account = self.account.copy({"name": "Daily retention other account"})
        self.scope.append(account.id)
        values = {
            "name": "Daily retention fixture",
            "account_id": account.id,
            "adapter_key": adapter,
            "role": "standby",
            "active": True,
            "inbound_active": False,
            "outbound_active": False,
        }
        if adapter == "wuzapi":
            values.update(
                wuzapi_base_url="https://wuzapi.invalid/",
                wuzapi_api_token="fixture-" + str(uuid.uuid4()),
                wuzapi_hmac_secret="unit-test-hmac-secret-at-least-32-chars",
            )
        connection = self.env["contact.center.provider.connection"].create(values)
        account.with_context(**_service_context()).write(
            {"retention_enabled": True, "retention_days": 30}
        )
        return account, connection

    def test_all_groups_multiple_batches_and_fixed_cutoff(self):
        old = self.env["mail.message"]
        for _index in range(101):
            old |= self._message()[0]
        _channel, other = self._group()
        old |= self._message(binding=other)[0]
        recent = self._message(date=self.new)[0]
        boundary = self._message(date=self.now - datetime.timedelta(days=30))[0]
        _channel, preserved = self._group()
        preserved.with_context(**_service_context()).write({"retention_preserve": True})
        protected = self._message(binding=preserved)[0]
        seen = []
        purge = type(self.service)._purge_binding

        def record(model, binding, **kwargs):
            seen.append(binding.id)
            self.assertEqual(kwargs["now"], self.now)
            return purge(model, binding, **kwargs)

        with mock.patch.object(type(self.service), "_purge_binding", new=record):
            result = self._run()
        self.assertEqual(result["message_count"], 102)
        self.assertFalse(old.exists())
        self.assertEqual(
            (recent | boundary | protected).exists(), recent | boundary | protected
        )
        self.assertEqual(seen[:2], [self.binding.id, other.id])
        self.assertTrue(preserved.retention_preserve)
        self.assertEqual(self.account.retention_days, 30)

    def test_disabled_archived_merged_and_other_provider_are_excluded(self):
        archived = self._group()[1]
        archived.write({"active": False})
        merged = self._group()[1]
        merged.write({"merged_into_id": self.binding.id})
        excluded = self._message(binding=archived)[0] | self._message(binding=merged)[0]
        original_account, original_connection = self.account, self.connection
        for mode in ("disabled", "inactive", "other_provider"):
            self.account, self.connection = self._other_account(
                adapter="test.retention" if mode == "other_provider" else "wuzapi"
            )
            other = self._group()[1]
            excluded |= self._message(binding=other)[0]
            if mode == "disabled":
                self.account.with_context(**_service_context()).write(
                    {"retention_enabled": False}
                )
            if mode == "inactive":
                self.account.write({"active": False})
        self.account, self.connection = original_account, original_connection
        removed = self._message()[0]
        self._run()
        self.assertEqual(excluded.exists(), excluded)
        self.assertFalse(removed.exists())

    def test_direct_conversation_is_excluded(self):
        identity = self.env["contact.center.identity"].create(
            {
                "name": "Daily direct fixture",
                "company_id": self.env.company.id,
                "mail_guest_id": self.env["mail.guest"]
                .create({"name": "Daily direct guest"})
                .id,
            }
        )
        self.binding.write({"conversation_type": "direct", "identity_id": identity.id})
        pending = self._message()[0]
        self.assertFalse(self._run())
        self.assertTrue(pending.exists())

    def test_more_than_100_expired_cancelled_schedules_without_messages(self):
        schedules = self.env["contact.center.scheduled.message"].with_context(
            **_service_context()
        )
        values = {
            "channel_id": self.channel.id,
            "requested_by_id": self.agent.id,
            "body": "Expired synthetic schedule",
            "body_sha256": "b" * 64,
            "scheduled_at": self.old,
            "state": "cancelled",
        }
        expired = schedules.create(
            [dict(values, ui_request_id=str(uuid.uuid4())) for _index in range(101)]
        )
        result = self._run()
        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["schedule_count"], 101)
        self.assertFalse(expired.exists())

    def _failure_isolated(self, error):
        failed = self._message()[0]
        other = self._group()[1]
        removed = self._message(binding=other)[0]
        purge = type(self.service)._purge_binding

        def fail_after_delete(model, binding, **kwargs):
            result = purge(model, binding, **kwargs)
            if binding.id == self.binding.id:
                self.assertFalse(failed.with_env(model.env).exists())
                raise error
            return result

        with mock.patch.object(
            type(self.service), "_purge_binding", new=fail_after_delete
        ):
            if isinstance(error, ValidationError):
                self._run()
            else:
                with self.assertRaisesRegex(ValidationError, "1 falha"):
                    self._run()
        self.assertTrue(failed.exists())
        self.assertFalse(removed.exists())
        self.assertFalse(self.binding.retention_expired_before)
        self.assertTrue(self.binding.retention_last_blocked)
        self.assertFalse(
            self.env["contact.center.retention.receipt"].search_count(
                [("conversation_ref", "=", self.binding.conversation_ref)]
            )
        )

    def test_expected_failure_rolls_back_and_other_group_continues(self):
        self._failure_isolated(ValidationError("Synthetic dependency"))

    def test_unexpected_failure_rolls_back_and_is_reported_after_other_group(self):
        self._failure_isolated(RuntimeError("Synthetic failure after deletion"))

    def test_staging_is_bounded_and_does_not_block_other_groups(self):
        pending = self._message()[0]
        other = self._group()[1]
        removed = self._message(binding=other)[0]
        prepare = type(self.service)._retention_prepare_dependencies
        calls = []

        def stage(model, binding, *args):
            if binding.id == self.binding.id:
                calls.append(binding.id)
                return False
            return prepare(model, binding, *args)

        with mock.patch.object(
            type(self.service), "_retention_prepare_dependencies", new=stage
        ):
            self._run()
        self.assertEqual(len(calls), 10)
        self.assertTrue(pending.exists())
        self.assertFalse(removed.exists())
        self.assertFalse(self.binding.retention_expired_before)
        self.assertTrue(self.binding.retention_last_blocked)

    def test_more_than_100_controls_without_messages(self):
        events = self.env["contact.center.inbox.event"]
        for _index in range(101):
            events |= self._indexed_event("control", [], date=self.old)
        result = self._run()
        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["inbox_count"], 101)
        self.assertTrue(
            all(event.raw_envelope_json == {"content_erased": True} for event in events)
        )

    def test_quote_staging_over_500_advances_in_one_run(self):
        target = self._message()[1].external_message_id
        template = self.load_fixture("message_group_text_lid.json")
        template["event"]["Info"]["Chat"] = self.binding.conversation_ref
        context = template["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        context.update(stanzaID=target, quotedMessage={"conversation": "Expired quote"})
        events = self.env["contact.center.inbox.event"]
        for _index in range(501):
            item = copy.deepcopy(template)
            item["event"]["Info"]["ID"] = str(uuid.uuid4())
            item["event"]["Info"]["Timestamp"] = self.new.isoformat() + "Z"
            events |= events.with_context(contact_center_skip_enqueue=True).create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": str(uuid.uuid4()),
                    "provider_schema_version": "v1.0.8",
                    "raw_envelope_json": item,
                    "state": "done",
                }
            )
        self.assertEqual(len(events.filtered("retention_reply_id")), 501)
        result = self._run()
        self.assertEqual(result["message_count"], 1)
        self.assertFalse(events.filtered("retention_reply_id"))
        self.assertNotIn("Expired quote", str(events.mapped("raw_envelope_json")))

    def test_unindexed_account_is_blocked_without_reindexing_other_account_cleans(self):
        event = self._indexed_event("control", [], date=self.old)
        event.with_context(
            **{
                retention_ingress._INDEX_CONTEXT: retention_ingress._RETENTION_INDEX_TOKEN
            }
        ).write({"retention_route_indexed": False})
        untouched = copy.deepcopy(event.raw_envelope_json)
        pending = self._message()[0]
        original_account, original_connection = self.account, self.connection
        self.account, self.connection = self._other_account()
        other = self._group()[1]
        removed = self._message(binding=other)[0]
        self.account, self.connection = original_account, original_connection
        self._run()
        self.assertTrue(pending.exists())
        self.assertFalse(removed.exists())
        self.assertTrue(self.binding.retention_last_blocked)
        self.assertFalse(event.retention_route_indexed)
        self.assertEqual(event.raw_envelope_json, untouched)

    def test_budget_leaves_remaining_groups_for_next_daily_run(self):
        removed = self._message()[0]
        other = self._group()[1]
        pending = self._message(binding=other)[0]
        with mock.patch.object(retention, "monotonic", side_effect=[0, 0, 301, 301]):
            result = self._run()
        self.assertEqual(result["message_count"], 1)
        self.assertFalse(removed.exists())
        self.assertTrue(pending.exists())

    def test_preserve_changed_after_first_batch_stops_further_batches(self):
        old = self.env["mail.message"]
        for _index in range(101):
            old |= self._message()[0]
        purge = type(self.service)._purge_binding

        def preserve_after_batch(model, binding, **kwargs):
            result = purge(model, binding, **kwargs)
            binding.with_context(**_service_context()).write(
                {"retention_preserve": True}
            )
            return result

        with mock.patch.object(
            type(self.service), "_purge_binding", new=preserve_after_batch
        ):
            result = self._run()
        self.assertEqual(result["message_count"], 100)
        self.assertEqual(len(old.exists()), 1)
        self.assertTrue(self.binding.retention_preserve)
