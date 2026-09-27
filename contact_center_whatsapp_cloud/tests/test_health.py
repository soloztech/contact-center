import datetime
import os
from unittest import mock

from odoo import fields

from odoo.addons.contact_center_base.services.adapter import ProviderPausedError
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import GRAPH_PATCH, FakeResponse, WhatsAppCloudCase
from .test_status import WhatsAppCloudOutboundMixin


class TestWhatsAppCloudHealth(WhatsAppCloudOutboundMixin, WhatsAppCloudCase):
    def _phone(self, **values):
        payload = {
            "id": self.PHONE_ID,
            "display_phone_number": "+55 11 99999-0000",
            "verified_name": "Soloz Industrial",
            "quality_rating": "GREEN",
            "status": "CONNECTED",
            "throughput": {"level": "STANDARD"},
        }
        payload.update(values)
        return FakeResponse(payload)

    def _run_health_cron(self, response):
        self.connection.sudo().write({"next_health_check_at": False})
        with trap_jobs() as trap:
            self.env[
                "contact.center.provider.connection"
            ]._cron_schedule_health_checks()
        self.assertTrue(
            any(
                job.method_name == "_job_check_health"
                and job.recordset == self.connection.sudo()
                for job in trap.enqueued_jobs
            )
        )
        self.connection.invalidate_recordset()
        job_uuid = self.connection.sudo().health_job_uuid
        self.assertTrue(job_uuid)
        with mock.patch(GRAPH_PATCH, **response) as request, trap_jobs(), mock.patch(
            "odoo.addons.contact_center_base.models.account.fields.Datetime.now",
            return_value=self._probe_clock(),
        ):
            self.connection.sudo().with_context(job_uuid=job_uuid)._job_check_health()
        self.connection.invalidate_recordset()
        return request

    def _probe_clock(self):
        """Date the probe after the fixture's observation, as a real clock would.

        The core orders state observations by ``last_state_observed_at`` and
        ignores an older one. A backwards host clock step (seen on WSL) between
        the fixture and the probe would make this read look stale; the probe is
        therefore dated no earlier than one second after the latest observation.
        """

        now = fields.Datetime.now()
        latest = self.connection.sudo().last_state_observed_at
        if latest and now <= latest:
            return latest + datetime.timedelta(seconds=1)
        return now

    def test_health_cron_reads_the_phone_number(self):
        request = self._run_health_cron({"return_value": self._phone()})
        method, url = request.call_args.args[:2]
        self.assertEqual(
            (method, url),
            ("GET", "https://graph.facebook.com/v26.0/%s" % self.PHONE_ID),
        )
        self.assertIn("quality_rating", request.call_args.kwargs["params"]["fields"])
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            "Bearer %s" % self.WABA_TOKEN,
        )
        connection = self.connection.sudo()
        self.assertEqual(connection.state, "connected")
        self.assertEqual(connection.health_detail, "healthy")
        self.assertEqual(connection.wa_display_phone_number, "+55 11 99999-0000")
        self.assertEqual(connection.wa_quality_rating, "GREEN")
        self.assertEqual(connection.wa_throughput_level, "STANDARD")
        self.assertTrue(connection.wa_observed_at)

    def test_missing_token_makes_the_connection_unavailable(self):
        previous = os.environ.pop(self.WABA_TOKEN_REF)
        self.addCleanup(os.environ.__setitem__, self.WABA_TOKEN_REF, previous)
        request = self._run_health_cron({"return_value": self._phone()})
        request.assert_not_called()
        self.assertEqual(self.connection.state, "authentication_required")
        self.assertFalse(self.connection._contact_center_outbound_is_available())

    def test_expired_token_makes_the_connection_unavailable(self):
        expired = FakeResponse(
            {"error": {"code": 190, "message": "Session has expired"}}, status=401
        )
        self._run_health_cron({"return_value": expired})
        self.assertEqual(self.connection.state, "authentication_required")
        self.assertFalse(self.connection._contact_center_outbound_is_available())

    def test_other_phone_identity_is_a_mismatch(self):
        self._run_health_cron({"return_value": self._phone(id="100000000000999")})
        self.assertEqual(self.connection.health_detail, "identity_mismatch")
        self.assertFalse(self.connection._contact_center_outbound_is_available())

    def test_restricted_phone_number_pauses_outbound(self):
        self._run_health_cron({"return_value": self._phone(status="RESTRICTED")})
        self.assertEqual(self.connection.state, "degraded")
        self.assertFalse(self.connection._contact_center_outbound_is_available())

    def test_send_after_the_observation_expires_requires_a_new_read(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel, "Depois da leitura")
        stale = fields.Datetime.now() - datetime.timedelta(minutes=10)
        self.env.cr.execute(
            "UPDATE contact_center_provider_connection "
            "SET last_health_at = %s, last_state_observed_at = %s WHERE id = %s",
            [stale, stale, self.connection.id],
        )
        self.connection.invalidate_recordset()
        self.assertFalse(self.connection._contact_center_outbound_is_available())
        with mock.patch(GRAPH_PATCH) as request:
            with self.assertRaises(ProviderPausedError):
                outbox._prepare_dispatch()
        request.assert_not_called()
        # No webhook renews the observation: only a real (simulated) read does.
        self.deliver(self.envelope(self.value(messages=[self.text_message()])))
        self.connection.invalidate_recordset()
        self.assertFalse(self.connection._contact_center_outbound_is_available())
        self._run_health_cron({"return_value": self._phone()})
        self.assertTrue(self.connection._contact_center_outbound_is_available())
        _connection, _command, _adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(snapshot["method"], "POST")

    def test_probe_after_a_backwards_clock_step_still_applies(self):
        # The fixture observed the connection "later" than the host clock now
        # reads, as after a backwards step of the WSL clock.
        ahead = fields.Datetime.now() + datetime.timedelta(seconds=30)
        self.env.cr.execute(
            "UPDATE contact_center_provider_connection "
            "SET last_state_observed_at = %s WHERE id = %s",
            [ahead, self.connection.id],
        )
        self.connection.invalidate_recordset()
        self._run_health_cron({"return_value": self._phone(status="RESTRICTED")})
        self.assertEqual(self.connection.state, "degraded")
        self.assertGreater(self.connection.last_state_observed_at, ahead)
