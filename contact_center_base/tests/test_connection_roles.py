import datetime
import threading
import uuid

from psycopg2 import IntegrityError
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase
from odoo.tools import mute_logger

from ..services.adapter import AdapterResult, ProviderAdapter, adapter_registry
from ..services.dto import EventDTO


@adapter_registry.register("test.connection.roles")
class ConnectionRoleTestAdapter(ProviderAdapter):
    display_name = "Connection Role Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(provider_response={"accepted": True})

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/connection-role-test",
            "payload": {"command_id": command.command_id},
        }

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected", "detail": "healthy"}


class TestProviderConnectionRoles(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Connection Role Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "role-account-%s" % uuid.uuid4(),
                # Test transactions run as the inactive technical superuser.
                # Ownership is an operational grant, so use the active admin.
                "access_user_ids": [(6, 0, cls.env.ref("base.user_admin").ids)],
            }
        )

    def _connection(self, name, role="primary", **overrides):
        values = {
            "name": name,
            "account_id": self.account.id,
            "adapter_key": "test.connection.roles",
            "external_ref": "role-connection-%s" % uuid.uuid4(),
            "provider_schema_version": "role-fixture-v1",
            "state": "connected",
            "active": role != "historical",
            "inbound_active": role == "primary",
            "outbound_active": False,
        }
        if role:
            values["role"] = role
        values.update(overrides)
        return self.env["contact.center.provider.connection"].create(values)

    def test_create_without_role_is_fail_closed_standby(self):
        connection = self._connection("Fail-closed default", role=None)

        self.assertEqual(connection.role, "standby")
        self.assertTrue(connection.active)
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)
        self.assertFalse(connection._contact_center_inbound_is_available())
        self.assertFalse(self.account.mark_read_enabled)

        self.account.mark_read_enabled = True
        self.assertTrue(self.account.mark_read_enabled)

    def test_outbound_admission_revision_is_internal_only(self):
        connection = self._connection("Protected admission revision")

        with self.assertRaises(AccessError), self.env.cr.savepoint():
            connection.write({"outbound_admission_revision": 99})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._connection(
                "Forged admission revision", outbound_admission_revision=99
            )

        self.assertEqual(connection.outbound_admission_revision, 0)

    def test_onboarding_ingress_revision_is_internal_and_monotonic(self):
        onboarding_ref = str(uuid.uuid4())
        connection = self._connection(
            "Protected onboarding ingress revision",
            role="migration",
            inbound_active=False,
            outbound_active=False,
            onboarding_ref=onboarding_ref,
        )

        with self.assertRaises(AccessError), self.env.cr.savepoint():
            connection.write({"onboarding_ingress_revision": 99})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._connection(
                "Forged onboarding ingress revision",
                role="migration",
                inbound_active=False,
                onboarding_ref=str(uuid.uuid4()),
                onboarding_ingress_revision=99,
            )

        self.assertEqual(connection.onboarding_ingress_revision, 0)
        self.assertEqual(
            connection._contact_center_record_onboarding_ingress(onboarding_ref),
            1,
        )
        self.assertEqual(connection.onboarding_ingress_revision, 1)

    def test_health_item_counts_unsupported_inbox_events(self):
        connection = self._connection("Unsupported telemetry")
        event_model = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
        )
        recent = event_model.create(
            {
                "provider_connection_id": connection.id,
                "inbox_dedupe_key": "unsupported-%s" % uuid.uuid4(),
                "provider_schema_version": "role-fixture-v1",
                "raw_envelope_json": {"event": "fixture"},
                "state": "unsupported",
            }
        )
        old = event_model.create(
            {
                "provider_connection_id": connection.id,
                "inbox_dedupe_key": "old-unsupported-%s" % uuid.uuid4(),
                "provider_schema_version": "role-fixture-v1",
                "raw_envelope_json": {"event": "old-fixture"},
                "state": "unsupported",
            }
        )
        self.env.cr.execute(
            """
            UPDATE contact_center_inbox_event
               SET create_date = %s
             WHERE id = %s
            """,
            [fields.Datetime.now() - datetime.timedelta(days=2), old.id],
        )

        self.assertTrue(recent)
        self.assertEqual(connection.inbox_unsupported_count, 2)
        self.assertEqual(connection.inbox_unsupported_recent_count, 1)
        self.assertEqual(
            connection._contact_center_health_item()["unsupported_count"], 1
        )

    def test_create_without_role_rejects_traffic_flags(self):
        with self.assertRaisesRegex(
            ValidationError, "without an explicit role"
        ), self.env.cr.savepoint():
            self._connection("Implicit live route", role=None, outbound_active=True)

    def test_standby_outbound_toggle_requires_controlled_switch(self):
        connection = self._connection("Explicit standby", role="standby")

        with self.assertRaisesRegex(
            ValidationError, "controlled primary switch"
        ), self.env.cr.savepoint():
            connection.write({"outbound_active": True})

        self.assertEqual(connection.role, "standby")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

    def test_primary_can_receive_while_outbound_is_disabled(self):
        connection = self._connection("Inbound-only primary")

        self.assertTrue(connection.inbound_active)
        self.assertFalse(connection.outbound_active)
        self.assertTrue(connection._contact_center_inbound_is_available())
        self.assertFalse(connection._contact_center_outbound_is_available())

    def test_standby_outbound_toggle_cannot_replace_existing_primary(self):
        self._connection("Existing primary", outbound_active=True)
        standby = self._connection("Blocked standby", role="standby")

        with self.assertRaisesRegex(
            ValidationError, "controlled primary switch"
        ), self.env.cr.savepoint():
            standby.write({"outbound_active": True})

    def test_historical_connection_requires_explicit_coherent_values(self):
        connection = self._connection("Imported history", role="historical")

        self.assertEqual(connection.role, "historical")
        self.assertFalse(connection.active)
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

    def test_copy_of_primary_is_fail_closed_standby(self):
        primary = self._connection("Primary to copy", outbound_active=True)

        duplicate = primary.copy(
            {
                "name": "Copied provider",
                "external_ref": "role-copy-%s" % uuid.uuid4(),
            }
        )

        self.assertEqual(duplicate.role, "standby")
        self.assertTrue(duplicate.active)
        self.assertFalse(duplicate.inbound_active)
        self.assertFalse(duplicate.outbound_active)

    def test_primary_switch_is_atomic_and_demotes_previous_connection(self):
        previous = self._connection("Previous primary", outbound_active=True)
        replacement = self._connection("Replacement standby", role="standby")

        replacement.action_use_as_primary()
        (previous | replacement).invalidate_recordset(
            ["active", "role", "inbound_active", "outbound_active"]
        )

        self.assertEqual(previous.role, "standby")
        self.assertTrue(previous.active)
        self.assertFalse(previous.inbound_active)
        self.assertFalse(previous.outbound_active)
        self.assertEqual(replacement.role, "primary")
        self.assertTrue(replacement.active)
        self.assertTrue(replacement.inbound_active)
        self.assertFalse(replacement.outbound_active)

    def test_migration_and_historical_roles_are_fail_closed(self):
        connection = self._connection("Migration candidate")

        connection.action_set_migration()
        self.assertEqual(connection.role, "migration")
        self.assertTrue(connection.active)
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            connection.write({"inbound_active": True})

        connection.action_set_historical()
        self.assertEqual(connection.role, "historical")
        self.assertFalse(connection.active)
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

        connection.action_set_standby()
        self.assertEqual(connection.role, "standby")
        self.assertTrue(connection.active)

    def test_standard_unarchive_restores_historical_connection_as_standby(self):
        connection = self._connection("Standard archive toggle")
        connection.toggle_active()

        self.assertFalse(connection.active)
        self.assertEqual(connection.role, "historical")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

        connection.toggle_active()

        self.assertTrue(connection.active)
        self.assertEqual(connection.role, "standby")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

    def test_database_rejects_two_active_primary_connections(self):
        primary = self._connection("Unique primary", outbound_active=True)
        standby = self._connection("Unique standby", role="standby")
        self.assertEqual(primary.role, "primary")

        with mute_logger("odoo.sql_db"), self.assertRaises(
            IntegrityError
        ), self.env.cr.savepoint():
            self.env.cr.execute(
                """
                UPDATE contact_center_provider_connection
                   SET role = 'primary',
                       inbound_active = TRUE
                 WHERE id = %s
                """,
                [standby.id],
            )

    def test_primary_replacement_requires_controlled_switch(self):
        previous = self._connection("Released primary", outbound_active=True)
        previous.outbound_active = False

        # Disabling outbound does not relinquish ingress. A second explicit
        # primary cannot move the webhook route as a create side effect.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._connection("Implicit replacement", outbound_active=True)

        replacement = self._connection("Controlled replacement", role="standby")
        replacement.action_use_as_primary()
        replacement.outbound_active = True
        (previous | replacement).invalidate_recordset(
            ["role", "inbound_active", "outbound_active"]
        )

        self.assertEqual(previous.role, "standby")
        self.assertFalse(previous.inbound_active)
        self.assertEqual(replacement.role, "primary")
        self.assertTrue(replacement.inbound_active)

    def test_explicit_primary_create_requires_complete_topology_values(self):
        complete = {
            "name": "Incomplete explicit primary",
            "account_id": self.account.id,
            "adapter_key": "test.connection.roles",
            "external_ref": "role-incomplete-%s" % uuid.uuid4(),
            "active": True,
            "role": "primary",
            "inbound_active": True,
            "outbound_active": False,
        }
        for missing_field in ("active", "inbound_active", "outbound_active"):
            values = dict(complete)
            values.pop(missing_field)
            values["external_ref"] = "role-incomplete-%s" % uuid.uuid4()
            with self.assertRaisesRegex(
                ValidationError, "requires explicit"
            ), self.env.cr.savepoint():
                self.env["contact.center.provider.connection"].create(values)

    def test_inactive_create_without_historical_role_is_rejected(self):
        with self.assertRaisesRegex(
            ValidationError, "archived provider connection"
        ), self.env.cr.savepoint():
            self._connection("Implicit historical", role=None, active=False)


@tagged("-at_install", "post_install")
class TestProviderConnectionRoleConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 12

    def _create_primary(self, account_id, token, suffix, barrier, results):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            # Establish the same pre-create snapshot in both workers.
            env["contact.center.account"].browse(account_id).read(["name"])
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            try:
                connection = env["contact.center.provider.connection"].create(
                    {
                        "name": "Concurrent primary %s %s" % (suffix, token),
                        "account_id": account_id,
                        "adapter_key": "test.connection.roles",
                        "external_ref": "concurrent-role-%s-%s" % (suffix, token),
                        "active": True,
                        "role": "primary",
                        "inbound_active": True,
                        "outbound_active": False,
                    }
                )
                cr.commit()  # pylint: disable=invalid-commit
                results.append(("committed", connection.id))
            except ValidationError:
                cr.rollback()
                results.append(("validation", False))
            except SerializationFailure:
                cr.rollback()
                results.append(("serialization", False))
            except IntegrityError:
                cr.rollback()
                results.append(("integrity", False))

    def test_concurrent_explicit_primary_creates_never_split_brain(self):
        token = uuid.uuid4().hex
        fixture = {}
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                account = env["contact.center.account"].create(
                    {
                        "name": "Concurrent role account %s" % token,
                        "company_id": env.company.id,
                        "platform": "whatsapp",
                        "external_ref": "concurrent-role-account-%s" % token,
                        "access_user_ids": [(6, 0, env.ref("base.user_admin").ids)],
                    }
                )
                fixture["account_id"] = account.id
                cr.commit()  # pylint: disable=invalid-commit

            barrier = threading.Barrier(2)
            results = []
            workers = [
                threading.Thread(
                    target=self._create_primary,
                    args=(fixture["account_id"], token, suffix, barrier, results),
                    name="cc-role-create-%s" % suffix,
                )
                for suffix in ("a", "b")
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=self.WORKER_TIMEOUT_SECONDS)
            self.assertFalse([worker.name for worker in workers if worker.is_alive()])
            self.assertEqual(
                len([result for result in results if result[0] == "committed"]),
                1,
            )
            self.assertEqual(len(results), 2)

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                connections = (
                    env["contact.center.provider.connection"]
                    .sudo()
                    .with_context(active_test=False)
                    .search([("account_id", "=", fixture["account_id"])])
                )
                self.assertEqual(len(connections), 1)
                self.assertEqual(connections.role, "primary")
        finally:
            if fixture.get("account_id"):
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["contact.center.provider.connection"].sudo().with_context(
                        active_test=False
                    ).search([("account_id", "=", fixture["account_id"])]).unlink()
                    env["contact.center.account"].sudo().with_context(
                        active_test=False
                    ).browse(fixture["account_id"]).unlink()
                    cr.commit()  # pylint: disable=invalid-commit
