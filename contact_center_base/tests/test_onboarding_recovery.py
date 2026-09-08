import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..models import account as account_model, onboarding as onboarding_model


class TestOnboardingRecovery(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref("base.user_admin")

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            onboarding_model._ONBOARDING_RECOVERY_CURSOR_PARAMETER,
            "0",
        )

    def _eligible_connection(self, name):
        token = uuid.uuid4().hex
        account = self.env["contact.center.account"].create(
            {
                "name": "Onboarding recovery %s" % name,
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": "onboarding-recovery-account-%s" % token,
                "access_user_ids": [fields.Command.set(self.admin.ids)],
            }
        )
        onboarding_ref = str(uuid.uuid4())
        connection = (
            self.env["contact.center.provider.connection"]
            .with_context(
                **{
                    onboarding_model._ONBOARDING_INTERNAL_CONTEXT: (
                        onboarding_model._ONBOARDING_INTERNAL_TOKEN
                    )
                }
            )
            .create(
                {
                    "name": "Onboarding recovery %s" % name,
                    "account_id": account.id,
                    "adapter_key": "test.connection.health",
                    "external_ref": "onboarding-recovery-connection-%s" % token,
                    "provider_schema_version": "onboarding-recovery-v1",
                    "state": "connected",
                    "active": True,
                    "role": "migration",
                    "inbound_active": False,
                    "outbound_active": False,
                    "onboarding_ref": onboarding_ref,
                    "onboarding_activated_at": fields.Datetime.now(),
                }
            )
        )
        connection.with_context(
            **{
                onboarding_model._ONBOARDING_ACTIVATION_CONTEXT: (
                    onboarding_model._ONBOARDING_ACTIVATION_TOKEN
                ),
                "contact_center_connection_role_switch_token": (
                    account_model._CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
                ),
            }
        ).write(
            {
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        event = self._blocked_event(connection, 1)
        return connection, event

    def _blocked_event(self, connection, sequence):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "onboarding-recovery:%s:%s"
                    % (connection.id, sequence),
                    "provider_schema_version": "onboarding-recovery-v1",
                    "raw_envelope_json": {"fixture": sequence},
                    "metadata_json": {
                        "blocked_reason": "onboarding_not_activated",
                        "onboarding_ref": connection.onboarding_ref,
                    },
                    "state": "blocked",
                    "last_error_class": "OnboardingNotActivated",
                }
            )
        )

    def _cron_selection(self, limit):
        with trap_jobs() as trap:
            self.assertEqual(
                self.env[
                    "contact.center.provider.connection"
                ]._cron_recover_onboarding_inbox_releases(limit=limit),
                limit,
            )
        trap.assert_jobs_count(limit)
        return [job.recordset.id for job in trap.enqueued_jobs]

    def test_recovery_cron_rotates_past_still_eligible_low_ids(self):
        connections = self.env["contact.center.provider.connection"]
        for sequence in range(3):
            connection, _event = self._eligible_connection(str(sequence))
            connections |= connection

        selected = [self._cron_selection(1)[0] for _iteration in range(4)]

        ordered_ids = connections.sorted("id").ids
        self.assertEqual(selected, ordered_ids + ordered_ids[:1])

    def test_recovery_cursor_rolls_back_with_the_enqueue_transaction(self):
        connections = self.env["contact.center.provider.connection"]
        for sequence in range(3):
            connection, _event = self._eligible_connection("rollback-%s" % sequence)
            connections |= connection

        first_id = self._cron_selection(1)[0]
        rolled_back_ids = []
        with self.assertRaises(RuntimeError):
            with self.env.cr.savepoint():
                rolled_back_ids.extend(self._cron_selection(1))
                raise RuntimeError("rollback onboarding scheduler")
        repeated_ids = self._cron_selection(1)

        ordered_ids = connections.sorted("id").ids
        self.assertEqual(first_id, ordered_ids[0])
        self.assertEqual(rolled_back_ids, [ordered_ids[1]])
        self.assertEqual(repeated_ids, rolled_back_ids)

    def test_release_job_processes_bounded_pages_and_chains(self):
        connection, first_event = self._eligible_connection("event-pages")
        events = first_event
        events |= self._blocked_event(connection, 2)
        events |= self._blocked_event(connection, 3)

        with mock.patch.object(
            onboarding_model,
            "_ONBOARDING_EVENT_RELEASE_BATCH_SIZE",
            2,
        ):
            with trap_jobs() as initial_trap:
                connection._contact_center_enqueue_onboarding_release(
                    connection.onboarding_ref,
                    0,
                )
                initial_trap.assert_jobs_count(1)
                release_job = initial_trap.enqueued_jobs[0]

            with trap_jobs() as first_page_trap:
                self.assertEqual(release_job.perform(), 2)
            inbox_jobs = [
                job
                for job in first_page_trap.enqueued_jobs
                if job.identity_key.startswith("contact_center:inbox:")
            ]
            continuations = [
                job
                for job in first_page_trap.enqueued_jobs
                if job.identity_key.startswith("contact_center:onboarding_release:")
            ]
            self.assertEqual(len(inbox_jobs), 2)
            self.assertEqual(len(continuations), 1)

            with trap_jobs() as second_page_trap:
                self.assertEqual(continuations[0].perform(), 1)
            self.assertEqual(
                len(
                    [
                        job
                        for job in second_page_trap.enqueued_jobs
                        if job.identity_key.startswith("contact_center:inbox:")
                    ]
                ),
                1,
            )
            self.assertFalse(
                [
                    job
                    for job in second_page_trap.enqueued_jobs
                    if job.identity_key.startswith("contact_center:onboarding_release:")
                ]
            )

        events.invalidate_recordset(["state", "queue_job_uuid"])
        self.assertEqual(set(events.mapped("state")), {"pending"})
        self.assertTrue(all(events.mapped("queue_job_uuid")))

    def test_recovery_limits_reject_unbounded_or_boolean_values(self):
        model = self.env["contact.center.provider.connection"]
        for invalid_limit in (0, True, 1001):
            with self.assertRaises(ValidationError):
                model._contact_center_onboarding_recovery_batch(invalid_limit)
