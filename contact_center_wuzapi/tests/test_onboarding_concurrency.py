import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from ..controllers.webhook import _find_or_create_inbox


@tagged("-at_install", "post_install")
class TestWuzapiOnboardingConcurrency(TransactionCase):
    """Prove the MVCC fence between buffered ingress and activation."""

    def _setup_committed_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env["contact.center.account"].create(
                {
                    "name": "Onboarding fence %s" % token,
                    "company_id": env.company.id,
                    "platform": "whatsapp",
                    "external_ref": "onboarding-fence-%s" % token,
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Onboarding fence %s" % token,
                    "account_id": account.id,
                    "adapter_key": "wuzapi",
                    "active": True,
                    "role": "migration",
                    "inbound_active": False,
                    "outbound_active": False,
                    "onboarding_ref": str(uuid.uuid4()),
                    "last_health_at": fields.Datetime.now(),
                    "wuzapi_base_url": "https://onboarding-fence.invalid",
                    "wuzapi_api_token": "onboarding-fence-token-%s" % token,
                    "wuzapi_hmac_secret": "hmac-%s" % token,
                }
            )
            env.flush_all()
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "connection_id": connection.id,
                "onboarding_ref": connection.onboarding_ref,
            }

    def _persist_blocked_callback(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            connection = env["contact.center.provider.connection"].browse(
                fixture["connection_id"]
            )
            connection._contact_center_lock_topology([fixture["account_id"]])
            event = (
                env["contact.center.inbox.event"]
                .sudo()
                .with_context(contact_center_skip_enqueue=True)
                .create(
                    {
                        "provider_connection_id": connection.id,
                        "inbox_dedupe_key": "wuzapi:onboarding-fence:%s"
                        % fixture["onboarding_ref"],
                        "provider_schema_version": "v1.0.8",
                        "raw_envelope_json": {
                            "type": "Disconnected",
                            "event": {},
                        },
                        "metadata_json": {
                            "blocked_reason": "onboarding_not_activated",
                            "onboarding_ref": fixture["onboarding_ref"],
                            "event_type": "Disconnected",
                        },
                        "state": "blocked",
                        "last_error_class": "OnboardingNotActivated",
                    }
                )
            )
            self.assertEqual(
                connection._contact_center_record_onboarding_ingress(
                    fixture["onboarding_ref"]
                ),
                1,
            )
            env.flush_all()
            event_id = event.id
            cr.commit()  # pylint: disable=invalid-commit
            return event_id

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.inbox.event"].sudo().search(
                [("provider_connection_id", "=", fixture["connection_id"])]
            ).unlink()
            env["contact.center.provider.connection"].sudo().browse(
                fixture["connection_id"]
            ).exists().unlink()
            env["contact.center.account"].sudo().browse(
                fixture["account_id"]
            ).exists().unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def test_duplicate_callback_requires_fresh_repeatable_read_snapshot(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex)
        values = {
            "provider_connection_id": fixture["connection_id"],
            "inbox_dedupe_key": "wuzapi:snapshot:%s" % fixture["onboarding_ref"],
            "provider_schema_version": "v1.0.8",
            "raw_envelope_json": {"type": "Disconnected", "event": {}},
            "state": "blocked",
        }
        domain = [
            ("provider_connection_id", "=", fixture["connection_id"]),
            ("inbox_dedupe_key", "=", values["inbox_dedupe_key"]),
        ]
        try:
            with self.registry.cursor() as stale_cr:
                stale_env = api.Environment(stale_cr, SUPERUSER_ID, {})
                stale_model = stale_env["contact.center.inbox.event"].with_context(
                    contact_center_skip_enqueue=True
                )
                self.assertFalse(stale_model.search(domain))
                with self.registry.cursor() as winner_cr:
                    winner_env = api.Environment(winner_cr, SUPERUSER_ID, {})
                    winner_model = winner_env[
                        "contact.center.inbox.event"
                    ].with_context(contact_center_skip_enqueue=True)
                    winner, duplicate = _find_or_create_inbox(
                        winner_model, domain, values
                    )
                    self.assertFalse(duplicate)
                    winner_id = winner.id
                    winner_cr.commit()  # pylint: disable=invalid-commit

                # PostgreSQL uniqueness sees the committed winner, while this
                # older MVCC snapshot cannot see it in an ORM search.
                self.assertFalse(stale_model.search(domain))
                with mute_logger("odoo.sql_db"), self.assertRaises(
                    SerializationFailure
                ) as caught:
                    _find_or_create_inbox(stale_model, domain, values)
                self.assertEqual(caught.exception.pgcode, "40001")

            with self.registry.cursor() as fresh_cr:
                fresh_env = api.Environment(fresh_cr, SUPERUSER_ID, {})
                fresh_model = fresh_env["contact.center.inbox.event"].with_context(
                    contact_center_skip_enqueue=True
                )
                duplicate, already_received = _find_or_create_inbox(
                    fresh_model, domain, values
                )
                self.assertTrue(already_received)
                self.assertEqual(duplicate.id, winner_id)
                self.assertEqual(fresh_model.search_count(domain), 1)
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_buffered_callback_forces_old_activation_snapshot_to_retry(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            stale_snapshot_error = None
            try:
                with self.registry.cursor() as stale_cr:
                    stale_env = api.Environment(stale_cr, SUPERUSER_ID, {})
                    stale_connection = stale_env[
                        "contact.center.provider.connection"
                    ].browse(fixture["connection_id"])
                    # Establish the REPEATABLE READ snapshot before ingress wins.
                    stale_cr.execute(
                        "SELECT onboarding_ingress_revision "
                        "FROM contact_center_provider_connection WHERE id = %s",
                        [fixture["connection_id"]],
                    )
                    self.assertEqual(stale_cr.fetchone()[0], 0)

                    event_id = self._persist_blocked_callback(fixture)
                    stale_connection._contact_center_lock_topology(
                        [fixture["account_id"]]
                    )
            except SerializationFailure as error:
                stale_snapshot_error = error

            self.assertIsInstance(stale_snapshot_error, SerializationFailure)

            # The normal transaction retry starts with a fresh snapshot and
            # sees both sides of the fence: the revision and its durable hold.
            with self.registry.cursor() as fresh_cr:
                fresh_env = api.Environment(fresh_cr, SUPERUSER_ID, {})
                connection = fresh_env["contact.center.provider.connection"].browse(
                    fixture["connection_id"]
                )
                event = fresh_env["contact.center.inbox.event"].sudo().browse(event_id)
                self.assertEqual(connection.onboarding_ingress_revision, 1)
                self.assertTrue(event.exists())
                self.assertEqual(event.state, "blocked")
                self.assertTrue(
                    any(
                        "newer provider disconnection" in reason
                        for reason in connection._contact_center_activation_blockers()
                    )
                )
        finally:
            if fixture:
                self._cleanup_committed_fixture(fixture)
