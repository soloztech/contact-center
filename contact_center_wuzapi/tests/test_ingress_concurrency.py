"""Exercise webhook admission with independent PostgreSQL transactions."""

import datetime
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

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.translate import code_translations

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
)
from odoo.addons.queue_job.controllers.main import _prevent_commit
from odoo.addons.queue_job.job import Job

from ..controllers import webhook
from ..models.provider_connection import _WUZAPI_OWN_IDENTITY_TOKEN
from .test_adapter import REQUEST_PATCH, FakeResponse


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

    def _reconnect_fixture(self):
        fixture = self._fixture()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            connection = env["contact.center.provider.connection"].browse(
                fixture["connection"]
            )
            connection.account_id.own_external_identity = "5511888888888@s.whatsapp.net"
            actor = env["res.users"].browse(fixture["agent"])
            actor.groups_id |= env.ref("contact_center_base.group_contact_center_admin")
            request = (
                env["contact.center.wuzapi.reconnect.wizard"]
                .with_user(actor)
                .create({"connection_id": connection.id})
            )
            request.action_start()
            fixture.update(
                wizard=request.id, job_uuid=connection.wuzapi_onboarding_job_uuid
            )
            cr.commit()  # pylint: disable=invalid-commit
        self.addCleanup(self._cleanup_reconnect_job, fixture["job_uuid"])
        return fixture

    def _cleanup_reconnect_job(self, job_uuid):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["queue.job"].search([("uuid", "=", job_uuid)]).unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def _assert_reconnect_lock_held(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertFalse(Job.load(env, fixture["job_uuid"]).lock())

    def _reconnect_during_write(self, *, drift=False, reconfigure=False, rerun=False):
        fixture = self._reconnect_fixture()
        calls = []

        def transport(method, url, **values):
            endpoint = "/" + url.split("/", 3)[-1]
            calls.append((method, endpoint))
            started = ("POST", "/session/connect") in calls
            if (method, endpoint) == ("POST", "/session/connect"):
                self.assertEqual(values["json"], {"Subscribe": [], "Immediate": True})
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    cr.execute(
                        "UPDATE contact_center_provider_connection "
                        "SET outbound_admission_revision = outbound_admission_revision + 1 "
                        "WHERE id = %s",
                        [fixture["connection"]],
                    )
                    if reconfigure:
                        connection = env["contact.center.provider.connection"].browse(
                            fixture["connection"]
                        )
                        next_revision = connection.health_configuration_revision + 1
                        connection.write(
                            {"health_configuration_revision": next_revision}
                        )
                    cr.commit()  # pylint: disable=invalid-commit
                return FakeResponse(200, {"code": 200, "success": True})
            if (method, endpoint) == ("GET", "/session/status"):
                return FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "connected": started,
                            "loggedIn": False,
                            "events": "Message"
                            if drift and started
                            else "Message,Connected",
                            "proxy_url": "http://proxy.invalid:8080" if started else "",
                        },
                    },
                )
            self.assertEqual((method, endpoint), ("GET", "/session/qr"))
            return FakeResponse(500, {"error": "not connected"})

        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = Job.load(env, fixture["job_uuid"])
            self.assertTrue(job.job_config.allow_commit)
            job.set_started()
            job.store()
            cr.commit()  # pylint: disable=invalid-commit
            self.assertTrue(job.lock())
            request_type = type(env["contact.center.wuzapi.reconnect.wizard"])
            current = request_type._current
            lock_checks = []
            translations = {}

            def checked_current(request, revision, *, lock=False):
                result = current(request, revision, lock=lock)
                if lock:
                    self._assert_reconnect_lock_held(fixture)
                    lock_checks.append(True)
                    language = (
                        request.env.lang or request.env.user.context_get()["lang"]
                    )
                    translations.update(
                        code_translations.get_python_translations(
                            "contact_center_wuzapi", language
                        )
                    )
                return result

            with mock.patch(REQUEST_PATCH, side_effect=transport), mock.patch.object(
                request_type, "_current", new=checked_current
            ), _prevent_commit(cr):
                job.perform()
                self.assertTrue(lock_checks)
                if rerun:
                    # The temporary result committed, but the outer done did not.
                    cr.rollback()
                    env.invalidate_all()
                    job = Job.load(env, fixture["job_uuid"])
                    self.assertTrue(job.lock())
                    job.perform()
                self._assert_reconnect_lock_held(fixture)
                job.set_done()
                job.store()
            cr.commit()  # pylint: disable=invalid-commit

        self.assertEqual(calls.count(("POST", "/session/connect")), 1)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            request = env["contact.center.wuzapi.reconnect.wizard"].browse(
                fixture["wizard"]
            )
            connection = request.connection_id
            self.assertEqual(connection.outbound_admission_revision, 1)
            if reconfigure:
                self.assertEqual(request.state, "error")
                self.assertFalse(request.pending_observation_json)
                self.assertNotEqual(
                    request.configuration_revision,
                    connection.health_configuration_revision,
                )
                self.assertEqual(connection.wuzapi_pairing_revision, 0)
            elif drift:
                self.assertEqual(request.state, "error")
                self.assertEqual(connection.wuzapi_webhook_sync_state, "drift")
                expected = (
                    "The provider's subscribed events changed. "
                    "Review the webhook configuration."
                )
                self.assertIn(translations.get(expected, expected), request.message)
            else:
                self.assertEqual(request.state, "waiting")
                self.assertFalse(request.pending_observation_json["proxy_present"])
                self.assertEqual(
                    request.pending_observation_json["events"], "Message,Connected"
                )
                expected = "The provider applied its configured network policy."
                self.assertIn(translations.get(expected, expected), request.message)

    def test_reconnect_preserves_baseline_during_concurrent_admission(self):
        self._reconnect_during_write()

    def test_reconnect_detects_drift_during_concurrent_admission(self):
        self._reconnect_during_write(drift=True)

    def test_reconnect_refuses_configuration_changed_during_http(self):
        self._reconnect_during_write(reconfigure=True)

    def test_reconnect_rerun_preserves_committed_waiting_result(self):
        self._reconnect_during_write(rerun=True)

    def test_reconnect_snapshot_reset_requires_allow_commit_and_keeps_job_lock(self):
        fixture = self._reconnect_fixture()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = Job.load(env, fixture["job_uuid"])
            job.set_started()
            job.store()
            cr.commit()  # pylint: disable=invalid-commit
            self.assertTrue(job.lock())
            function = job.db_record().job_function_id
            # Uncommitted metadata change is rolled back with this test cursor.
            function.allow_commit = False
            request = env["contact.center.wuzapi.reconnect.wizard"].browse(
                fixture["wizard"]
            )
            with self.assertRaisesRegex(RuntimeError, "allow_commit"):
                request.with_context(
                    job_uuid=fixture["job_uuid"]
                )._refresh_job_snapshot()
            self._assert_reconnect_lock_held(fixture)
            cr.rollback()
            env.invalidate_all()
            env["queue.job.function"].clear_caches()

    def _envelope(self, group=True):
        path = Path(__file__).parent / "fixtures" / "message_group_text_lid.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["event"]["Info"]["ID"] = "ingress-" + uuid.uuid4().hex
        if not group:
            envelope["event"]["Info"].update(
                Chat="5511900000001@s.whatsapp.net", IsGroup=False
            )
        return envelope

    def _admit(self, env, fixture, envelope, *, allow_pairing=False):
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
        if not allow_pairing:
            self.assertFalse(reason)
        else:
            self.assertEqual(reason, "pairing_unverified")
        if response is not None:
            return json.loads(response.data)
        sanitized = webhook.sanitize_webhook_envelope(envelope)
        digest = hashlib.sha256(body).hexdigest()
        values = webhook._inbox_ledger_values(
            connection, envelope, sanitized, body, digest, "race:" + digest, reason
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

    def test_qr_commit_rejects_stale_admission_then_fresh_callback_is_held(self):
        fixture = self._fixture()
        envelope = self._envelope(group=False)
        with self.registry.cursor() as stale_cr:
            stale_env = api.Environment(stale_cr, SUPERUSER_ID, {})
            connection = stale_env["contact.center.provider.connection"].browse(
                fixture["connection"]
            )
            self.assertFalse(connection._wuzapi_pairing_pending())
            with self.registry.cursor() as qr_cr:
                qr_env = api.Environment(qr_cr, SUPERUSER_ID, {})
                qr_cr.execute(
                    "SELECT id FROM contact_center_provider_connection "
                    "WHERE id = %s FOR UPDATE",
                    [connection.id],
                )
                qr_env["contact.center.provider.connection"].browse(
                    connection.id
                ).with_context(
                    wuzapi_own_identity_token=_WUZAPI_OWN_IDENTITY_TOKEN
                ).write(
                    {"wuzapi_pairing_state": "qr", "wuzapi_pairing_revision": 1}
                )
                qr_cr.commit()  # pylint: disable=invalid-commit
            with mock.patch.object(
                webhook, "request", SimpleNamespace(env=stale_env)
            ), self.assertRaises(SerializationFailure):
                self._admit(stale_env, fixture, envelope)
            stale_cr.rollback()
        with self.registry.cursor() as fresh_cr:
            fresh_env = api.Environment(fresh_cr, SUPERUSER_ID, {})
            with mock.patch.object(webhook, "request", SimpleNamespace(env=fresh_env)):
                result = self._admit(fresh_env, fixture, envelope, allow_pairing=True)
            self.assertEqual(result["state"], "blocked")
            event = fresh_env["contact.center.inbox.event"].browse(result["inbox_id"])
            self.assertEqual(event.metadata_json["hold_epoch"], 1)
            self.assertFalse(event.normalized_dto_json)
            fresh_cr.commit()  # pylint: disable=invalid-commit

    def test_committed_negative_receipt_is_seen_outside_old_release_snapshot(self):
        fixture = self._fixture()
        with self.registry.cursor() as setup_cr:
            env = api.Environment(setup_cr, SUPERUSER_ID, {})
            connection = env["contact.center.provider.connection"].browse(
                fixture["connection"]
            )
            connection.with_context(
                wuzapi_own_identity_token=_WUZAPI_OWN_IDENTITY_TOKEN
            ).write({"wuzapi_pairing_revision": 1, "wuzapi_pairing_state": "verified"})
            event = env["contact.center.inbox.event"].create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "origin-held",
                    "provider_schema_version": "v1.0.8",
                    "raw_envelope_json": {"type": "Message"},
                    "state": "blocked",
                    "metadata_json": {
                        "blocked_reason": "pairing_unverified",
                        "hold_epoch": 1,
                        "hold_configuration_revision": connection.health_configuration_revision,
                    },
                }
            )
            event_id = event.id
            connection._apply_health_result(
                {"state": "connected", "identity_matches": True},
                records_health_probe=True,
                observed_at=fields.Datetime.now() + datetime.timedelta(seconds=2),
            )
            setup_cr.commit()  # pylint: disable=invalid-commit
        with self.registry.cursor() as stale_cr:
            env = api.Environment(stale_cr, SUPERUSER_ID, {})
            held = env["contact.center.inbox.event"].browse(event_id)
            self.assertEqual(held.pairing_hold_status, "verified")
            with self.registry.cursor() as callback_cr:
                callback_env = api.Environment(callback_cr, SUPERUSER_ID, {})
                connection = callback_env["contact.center.provider.connection"].browse(
                    fixture["connection"]
                )
                connection._contact_center_lock_ingress_admission()
                callback_env["contact.center.inbox.event"].with_context(
                    contact_center_skip_enqueue=True
                ).create(
                    {
                        "provider_connection_id": connection.id,
                        "inbox_dedupe_key": "negative-held",
                        "provider_schema_version": "v1.0.8",
                        "raw_envelope_json": {"type": "LoggedOut"},
                        "metadata_json": {
                            "event_type": "LoggedOut",
                            "pairing_epoch_at_receipt": 1,
                            "pairing_phase_at_receipt": "qr",
                        },
                    }
                )
                callback_cr.commit()  # pylint: disable=invalid-commit
            with self.assertRaises(ValidationError):
                held._contact_center_confirm_pairing_origin()
            self.assertEqual(held.state, "blocked")
            self.assertFalse(held.metadata_json.get("pairing_origin_confirmed_by"))
            stale_cr.rollback()

    def test_reconnect_result_does_not_lock_connection_ahead_of_account_writer(self):
        from odoo.addons.queue_job.tests.common import trap_jobs

        fixture = self._fixture()
        with self.registry.cursor() as setup_cr:
            env = api.Environment(setup_cr, SUPERUSER_ID, {})
            actor = env["res.users"].browse(fixture["agent"])
            actor.groups_id += env.ref("contact_center_base.group_contact_center_admin")
            request = (
                env["contact.center.wuzapi.reconnect.wizard"]
                .with_user(actor)
                .create({"connection_id": fixture["connection"]})
            )
            with trap_jobs() as trap:
                request.action_start()
            wizard_id, revision, job_uuid = (
                request.id,
                request.request_revision,
                trap.enqueued_jobs[0].uuid,
            )
            setup_cr.commit()  # pylint: disable=invalid-commit
        account_locked, share_attempted = threading.Event(), threading.Event()
        errors, completed = [], []
        cursor_class = type(self.env.cr)
        execute = cursor_class.execute

        def observed_execute(cursor, query, *args, **kwargs):
            if (
                threading.current_thread().name == "cc-reconnect-result"
                and isinstance(query, str)
                and "contact_center_account" in query
                and "FOR SHARE" in query
            ):
                share_attempted.set()
            return execute(cursor, query, *args, **kwargs)

        def worker(kind):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '8s'")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    if kind == "writer":
                        cr.execute(
                            "SELECT id FROM contact_center_account WHERE id = %s FOR UPDATE",
                            [fixture["account"]],
                        )
                        account_locked.set()
                        if not share_attempted.wait(self.TIMEOUT):
                            raise TimeoutError(
                                "Reconnect did not reach account admission"
                            )
                        cr.execute(
                            "SELECT id FROM contact_center_provider_connection "
                            "WHERE id = %s FOR UPDATE",
                            [fixture["connection"]],
                        )
                    else:
                        if not account_locked.wait(self.TIMEOUT):
                            raise TimeoutError("Account writer did not start")
                        request = (
                            env["contact.center.wuzapi.reconnect.wizard"]
                            .browse(wizard_id)
                            .with_context(job_uuid=job_uuid)
                        )
                        connection = request._current(revision, lock=True)
                        self.assertTrue(connection)
                        connection._apply_health_result(
                            {"state": "connected", "identity_matches": True},
                            records_health_probe=True,
                        )
                    cr.commit()  # pylint: disable=invalid-commit
                    completed.append(kind)
            except Exception as error:
                errors.append(error)
                account_locked.set()
                share_attempted.set()

        threads = [
            threading.Thread(
                target=worker,
                args=(kind,),
                name="cc-reconnect-result"
                if kind == "reconnect"
                else "cc-account-writer",
                daemon=True,
            )
            for kind in ("writer", "reconnect")
        ]
        with mock.patch.object(cursor_class, "execute", new=observed_execute):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(self.TIMEOUT + 2)
        self.assertFalse([thread.name for thread in threads if thread.is_alive()])
        self.assertFalse(errors, [repr(error) for error in errors])
        self.assertEqual(set(completed), {"writer", "reconnect"})
