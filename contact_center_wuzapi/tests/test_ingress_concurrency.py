"""Exercise webhook admission with independent PostgreSQL transactions."""

import hashlib
import hmac
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
)

from ..controllers import webhook


@tagged("-at_install", "post_install")
class TestWuzapiIngressConcurrency(TransactionCase):
    """No retry may hide the shared-to-exclusive admission deadlock."""

    TIMEOUT = 12
    SECRET = "ingress-race-synthetic-secret-at-least-32-chars"

    def _fixture(self):
        token = uuid.uuid4().hex
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Ingress race agent",
                        "login": "ingress-race-" + token,
                        "company_id": env.company.id,
                        "company_ids": [(6, 0, env.company.ids)],
                        "groups_id": [
                            (
                                6,
                                0,
                                env.ref(
                                    "contact_center_base.group_contact_center_agent"
                                ).ids,
                            )
                        ],
                    }
                )
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Ingress race " + token,
                    "platform": "whatsapp",
                    "company_id": env.company.id,
                    "access_user_ids": [(6, 0, agent.ids)],
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Ingress race connection",
                    "account_id": account.id,
                    "adapter_key": "wuzapi",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                    "wuzapi_base_url": "https://ingress-race.invalid/",
                    "wuzapi_api_token": "synthetic-test-only",
                    "wuzapi_hmac_secret": self.SECRET,
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            fixture = dict(agent=agent.id, account=account.id, connection=connection.id)
        self.addCleanup(self._cleanup_fixture, fixture)
        return fixture

    def _cleanup_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.inbox.event"].search(
                [("provider_connection_id", "=", fixture["connection"])]
            ).unlink()
            env["contact.center.retention.receipt"].with_context(
                contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
            ).search([("account_id", "=", fixture["account"])]).unlink()
            env["contact.center.provider.connection"].browse(
                fixture["connection"]
            ).unlink()
            env["contact.center.account"].browse(fixture["account"]).unlink()
            env["res.users"].browse(fixture["agent"]).unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def _envelope(self, group=True):
        path = Path(__file__).parent / "fixtures" / "message_group_text_lid.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["event"]["Info"]["ID"] = "ingress-" + uuid.uuid4().hex
        if not group:
            envelope["event"]["Info"].update(
                Chat="5511900000001@s.whatsapp.net", IsGroup=False
            )
        return envelope

    def _admit(self, env, fixture, envelope):
        connection = env["contact.center.provider.connection"].browse(
            fixture["connection"]
        )
        body = json.dumps(envelope, sort_keys=True).encode()
        headers = {
            "x-hmac-signature": hmac.new(
                self.SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
        }
        response, reason = webhook._conversation_ingress_response(
            connection, connection.get_adapter(), headers, body, envelope
        )
        self.assertFalse(reason)
        if response is not None:
            return json.loads(response.data)
        sanitized = webhook.sanitize_webhook_envelope(envelope)
        digest = hashlib.sha256(body).hexdigest()
        values = webhook._inbox_ledger_values(
            connection, envelope, sanitized, body, digest, "race:" + digest, ""
        )
        inbox, duplicate = webhook._find_or_create_inbox(
            env["contact.center.inbox.event"].with_context(
                contact_center_skip_enqueue=True
            ),
            [
                ("provider_connection_id", "=", connection.id),
                ("inbox_dedupe_key", "=", values["inbox_dedupe_key"]),
            ],
            values,
        )
        self.assertFalse(duplicate)
        return {"inbox_id": inbox.id, "state": inbox.state}

    def _two_callbacks(self, group):
        fixture = self._fixture()
        release = threading.Event()
        reached = [threading.Event(), threading.Event()]
        connected = [threading.Event(), threading.Event()]
        local_request = threading.local()
        pids, results, errors = {}, {}, {}
        retention_class = type(self.env["contact.center.retention"])
        original = retention_class._retention_route_is_expired
        names = ["cc-ingress-race-a", "cc-ingress-race-b"]

        def pause_at_retention(service, connection, route):
            name = threading.current_thread().name
            if name in names and connection.id == fixture["connection"]:
                index = names.index(name)
                if not reached[index].is_set():
                    reached[index].set()
                    if not release.wait(self.TIMEOUT):
                        raise TimeoutError("Coordinator did not release retention")
            return original(service, connection, route)

        def callback(index):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '8s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    cr.execute("SELECT pg_backend_pid()")
                    pids[index] = cr.fetchone()[0]
                    connected[index].set()
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    local_request.env = env
                    results[index] = self._admit(env, fixture, self._envelope(group))
                    cr.commit()  # pylint: disable=invalid-commit
            except Exception as error:  # report thread errors in the test process
                errors[index] = error
                connected[index].set()

        threads = [
            threading.Thread(target=callback, args=(i,), name=names[i], daemon=True)
            for i in range(2)
        ]
        observed = None
        with mock.patch.object(webhook, "request", local_request), mock.patch.object(
            retention_class, "_retention_route_is_expired", new=pause_at_retention
        ):
            try:
                threads[0].start()
                self.assertTrue(reached[0].wait(self.TIMEOUT), errors)
                threads[1].start()
                self.assertTrue(connected[1].wait(self.TIMEOUT), errors)
                with self.registry.cursor() as observer:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if reached[1].is_set():
                            observed = "concurrent_readers"
                            break
                        self.assertFalse(errors, errors)
                        observer.execute("SELECT pg_blocking_pids(%s)", [pids[1]])
                        if pids[0] in observer.fetchone()[0]:
                            observed = "serialized_before_retention"
                            break
                        reached[1].wait(0.01)
                self.assertIsNotNone(
                    observed,
                    "Second callback neither progressed nor waited on the first",
                )
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(self.TIMEOUT)
        self.assertFalse([thread.name for thread in threads if thread.is_alive()])
        self.assertFalse(errors, {key: repr(value) for key, value in errors.items()})
        self.assertEqual(len(results), 2)
        self.assertEqual({result["state"] for result in results.values()}, {"pending"})
        self.assertEqual(len({result["inbox_id"] for result in results.values()}), 2)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertEqual(
                env["contact.center.inbox.event"].search_count(
                    [("provider_connection_id", "=", fixture["connection"])]
                ),
                2,
            )
        return observed

    def test_two_group_callbacks_commit_without_deadlock(self):
        self.assertEqual(self._two_callbacks(group=True), "serialized_before_retention")

    def test_direct_callbacks_remain_concurrent_readers(self):
        self.assertEqual(self._two_callbacks(group=False), "concurrent_readers")

    def test_committed_expiry_rejects_stale_snapshot_and_fresh_callback(self):
        fixture = self._fixture()
        envelope = self._envelope()
        with self.registry.cursor() as stale_cr:
            stale_env = api.Environment(stale_cr, SUPERUSER_ID, {})
            self.assertTrue(
                stale_env["contact.center.provider.connection"]
                .browse(fixture["connection"])
                .account_id
            )
            with self.registry.cursor() as writer_cr:
                writer_env = api.Environment(writer_cr, SUPERUSER_ID, {})
                account = writer_env["contact.center.account"].browse(
                    fixture["account"]
                )
                account._lock_conversation_policy()
                writer_env["contact.center.retention.receipt"].with_context(
                    contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
                ).create(
                    {
                        "account_id": account.id,
                        "conversation_ref": writer_env[
                            "contact.center.retention"
                        ]._retention_route(
                            writer_env["contact.center.provider.connection"].browse(
                                fixture["connection"]
                            ),
                            envelope,
                        )[
                            "conversation_ref"
                        ],
                        "external_message_id": envelope["event"]["Info"]["ID"],
                    }
                )
                writer_cr.commit()  # pylint: disable=invalid-commit
            with mock.patch.object(webhook, "request", SimpleNamespace(env=stale_env)):
                with self.assertRaises(SerializationFailure):
                    self._admit(stale_env, fixture, envelope)
            stale_cr.rollback()
        with self.registry.cursor() as fresh_cr:
            fresh_env = api.Environment(fresh_cr, SUPERUSER_ID, {})
            with mock.patch.object(webhook, "request", SimpleNamespace(env=fresh_env)):
                self.assertEqual(
                    self._admit(fresh_env, fixture, envelope),
                    {"accepted": True, "expired": True},
                )
            self.assertFalse(
                fresh_env["contact.center.inbox.event"].search(
                    [("provider_connection_id", "=", fixture["connection"])]
                )
            )

    def test_demoted_connection_rejects_stale_snapshot_and_fresh_callback(self):
        fixture = self._fixture()
        envelope = self._envelope()
        with self.registry.cursor() as stale_cr:
            stale_env = api.Environment(stale_cr, SUPERUSER_ID, {})
            self.assertEqual(
                stale_env["contact.center.provider.connection"]
                .browse(fixture["connection"])
                .role,
                "primary",
            )
            with self.registry.cursor() as writer_cr:
                writer_env = api.Environment(writer_cr, SUPERUSER_ID, {})
                writer_env["contact.center.provider.connection"].browse(
                    fixture["connection"]
                ).action_set_standby()
                writer_cr.commit()  # pylint: disable=invalid-commit
            with mock.patch.object(webhook, "request", SimpleNamespace(env=stale_env)):
                with self.assertRaises(SerializationFailure):
                    self._admit(stale_env, fixture, envelope)
            stale_cr.rollback()
        with self.registry.cursor() as fresh_cr:
            fresh_env = api.Environment(fresh_cr, SUPERUSER_ID, {})
            with mock.patch.object(webhook, "request", SimpleNamespace(env=fresh_env)):
                self.assertEqual(
                    self._admit(fresh_env, fixture, envelope),
                    {"accepted": True, "ignored": "inactive_transport"},
                )
            self.assertFalse(
                fresh_env["contact.center.inbox.event"].search(
                    [("provider_connection_id", "=", fixture["connection"])]
                )
            )

    def _operational_interleaving(self, writer_first):
        fixture = self._fixture()
        release = threading.Event()
        writer_account = threading.Event()
        group_retention = threading.Event()
        connected = {"group": threading.Event(), "operation": threading.Event()}
        pids, results, errors = {}, {}, {}
        request_local = threading.local()
        cursor_class = type(self.env.cr)
        execute = cursor_class.execute
        retention_class = type(self.env["contact.center.retention"])
        retention = retention_class._retention_route_is_expired

        def observed_execute(cursor, query, *args, **kwargs):
            result = execute(cursor, query, *args, **kwargs)
            if (
                threading.current_thread().name == "cc-operational-writer"
                and isinstance(query, str)
                and "SELECT id FROM contact_center_account" in query
                and "FOR SHARE" in query
            ):
                writer_account.set()
                if writer_first and not release.wait(self.TIMEOUT):
                    raise TimeoutError("Operational writer was not released")
            return result

        def paused_retention(service, connection, route):
            if (
                threading.current_thread().name == "cc-operational-group"
                and connection.id == fixture["connection"]
                and not group_retention.is_set()
            ):
                group_retention.set()
                if not release.wait(self.TIMEOUT):
                    raise TimeoutError("Group callback was not released")
            return retention(service, connection, route)

        def worker(kind):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '8s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    cr.execute("SELECT pg_backend_pid()")
                    pids[kind] = cr.fetchone()[0]
                    connected[kind].set()
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    if kind == "group":
                        request_local.env = env
                        results[kind] = self._admit(env, fixture, self._envelope())
                    else:
                        connections = env[
                            "contact.center.provider.connection"
                        ]._contact_center_lock_operational_admission(
                            [fixture["account"]]
                        )
                        self.assertIn(fixture["connection"], connections.ids)
                        results[kind] = "committed"
                    cr.commit()  # pylint: disable=invalid-commit
            except Exception as error:
                errors[kind] = error
                connected[kind].set()

        threads = {
            "group": threading.Thread(
                target=worker, args=("group",), name="cc-operational-group", daemon=True
            ),
            "operation": threading.Thread(
                target=worker,
                args=("operation",),
                name="cc-operational-writer",
                daemon=True,
            ),
        }
        first, second = (
            ("operation", "group") if writer_first else ("group", "operation")
        )
        progress = writer_account if writer_first else group_retention
        observed_wait = False
        with mock.patch.object(
            cursor_class, "execute", new=observed_execute
        ), mock.patch.object(
            retention_class, "_retention_route_is_expired", new=paused_retention
        ), mock.patch.object(
            webhook, "request", request_local
        ):
            try:
                threads[first].start()
                self.assertTrue(progress.wait(self.TIMEOUT), errors)
                threads[second].start()
                self.assertTrue(connected[second].wait(self.TIMEOUT), errors)
                with self.registry.cursor() as observer:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        self.assertFalse(errors, errors)
                        if writer_first and group_retention.is_set():
                            observed_wait = True
                            break
                        observer.execute("SELECT pg_blocking_pids(%s)", [pids[second]])
                        if pids[first] in observer.fetchone()[0]:
                            observed_wait = True
                            break
                        threading.Event().wait(0.01)
                self.assertTrue(
                    observed_wait, "Interleaving did not reach the intended contention"
                )
            finally:
                release.set()
                for thread in threads.values():
                    if thread.ident is not None:
                        thread.join(self.TIMEOUT)
        self.assertFalse(
            [thread.name for thread in threads.values() if thread.is_alive()]
        )
        self.assertFalse(errors, {key: repr(value) for key, value in errors.items()})
        self.assertEqual(results["operation"], "committed")
        self.assertEqual(results["group"]["state"], "pending")
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertEqual(
                env["contact.center.inbox.event"].search_count(
                    [("provider_connection_id", "=", fixture["connection"])]
                ),
                1,
            )

    def test_operation_before_group_admission_preserves_account_first_order(self):
        self._operational_interleaving(writer_first=True)

    def test_group_admission_before_operation_has_no_lock_cycle(self):
        self._operational_interleaving(writer_first=False)

    def test_group_retention_routes_survive_sanitization(self):
        fixture = self._fixture()
        folder = Path(__file__).parent / "fixtures"
        paths = sorted(folder.glob("message_group_*.json")) + [
            folder / name
            for name in (
                "group_info_webhook.json",
                "joined_group_webhook.json",
                "picture_group_webhook.json",
            )
        ]
        envelopes = [(path.name, json.loads(path.read_text())) for path in paths]
        receipt = json.loads((folder / "read_receipt.json").read_text())
        receipt["event"].update(Chat="120363000000001@g.us", IsGroup=True)
        envelopes.append(("group_read_receipt", receipt))
        unsupported = self._envelope()
        unsupported["event"]["Message"] = {
            "futureUnsupportedMessage": {"private": "synthetic"}
        }
        envelopes.append(("unsupported_group", unsupported))
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            connection = env["contact.center.provider.connection"].browse(
                fixture["connection"]
            )
            service = env["contact.center.retention"]
            for name, envelope in envelopes:
                with self.subTest(fixture=name):
                    raw_route = service._retention_route(connection, envelope)
                    self.assertTrue(raw_route)
                    self.assertEqual(raw_route["conversation_type"], "group")
                    self.assertEqual(
                        raw_route,
                        service._retention_route(
                            connection, webhook.sanitize_webhook_envelope(envelope)
                        ),
                    )
