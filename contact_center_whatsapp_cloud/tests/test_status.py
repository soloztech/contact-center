import uuid
from unittest import mock

import requests

from odoo.exceptions import AccessError

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import GRAPH_PATCH, FakeResponse, WhatsAppCloudCase


class WhatsAppCloudOutboundMixin:
    def _conversation(self, body="Pergunta do cliente"):
        inbound = self.text_message(body=body)
        self.deliver(self.envelope(self.value(messages=[inbound])))
        binding = self.binding_for(inbound["id"])
        return binding.channel_binding_id.channel_id, binding

    def _send(self, channel, text="Resposta do atendente", **kwargs):
        with trap_jobs():
            result = (
                self.env["contact.center.ui.api"]
                .with_user(self.agent)
                .send_message(channel.id, text, **kwargs)
            )
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id.message_id", "=", result["message_id"])])
            .ensure_one()
        )

    @staticmethod
    def _accepted(wamid, wa_id=None):
        return FakeResponse(
            {
                "messaging_product": "whatsapp",
                "contacts": [{"input": wa_id or "", "wa_id": wa_id or ""}],
                "messages": [{"id": wamid, "message_status": "accepted"}],
            }
        )

    def _run_job(self, outbox):
        """Run the queue worker once, keeping its committed boundary in-test."""

        outbox = outbox.sudo()
        if not outbox.queue_job_uuid:
            outbox.write({"queue_job_uuid": str(uuid.uuid4())})
        job = outbox.with_context(job_uuid=outbox.queue_job_uuid)
        with mock.patch.object(
            type(outbox),
            "_commit_job_transaction",
            autospec=True,
            side_effect=lambda record: record.env.flush_all(),
        ):
            try:
                with trap_jobs():
                    return job._job_process()
            except RetryableJobError as error:
                return error


class TestWhatsAppCloudStatus(WhatsAppCloudOutboundMixin, WhatsAppCloudCase):
    def _sent_message(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel)
        wamid = self.wamid("sent")
        with mock.patch(GRAPH_PATCH, return_value=self._accepted(wamid, self.CUSTOMER)):
            outbox._process_one()
        binding = outbox.message_binding_id
        binding.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        self.assertEqual(binding.delivery_state, "sent")
        return outbox, binding, wamid

    def _status(self, wamid, state, **kwargs):
        _delivery, inboxes = self.deliver(
            self.envelope(self.value(statuses=[self.status(wamid, state, **kwargs)]))
        )
        return inboxes

    def _delivery_states(self, binding):
        return (
            self.env["contact.center.delivery.event"]
            .sudo()
            .search([("message_binding_id", "=", binding.id)], order="id")
            .mapped("state")
        )

    # -- 7 ----------------------------------------------------------------------

    def test_sent_delivered_read_advance_and_never_regress(self):
        outbox, binding, wamid = self._sent_message()
        callback = binding.client_message_id
        self.assertTrue(callback.startswith("wac:"))
        inbox = self._status(wamid, "delivered", callback=callback)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(binding.delivery_state, "delivered")
        now = self.now()
        self._status(wamid, "read", callback=callback, timestamp=now + 5)
        self.assertEqual(binding.delivery_state, "read")
        # A late delivered after read (and a repeated read) never regresses.
        self._status(wamid, "delivered", callback=callback, timestamp=now + 1)
        self._status(wamid, "sent", timestamp=now)
        self.assertEqual(binding.delivery_state, "read")
        self.assertEqual(outbox.state, "done")

    def test_read_before_delivered_does_not_regress(self):
        _outbox, binding, wamid = self._sent_message()
        self._status(wamid, "read")
        self._status(wamid, "delivered")
        self.assertEqual(binding.delivery_state, "read")

    def test_played_voice_message_is_read(self):
        _outbox, binding, wamid = self._sent_message()
        inbox = self._status(wamid, "played")
        self.assertEqual(inbox.normalized_dto_json["delivery"]["state"], "read")
        self.assertEqual(binding.delivery_state, "read")

    def test_failed_status_records_a_local_row_and_keeps_the_message_sent(self):
        _outbox, binding, wamid = self._sent_message()
        errors = [
            {
                "code": 131026,
                "title": "Message undeliverable",
                "message": "texto livre",
                "error_data": {"details": "detalhe do destinatário"},
            }
        ]
        delivery, inboxes = self.deliver(
            self.envelope(
                self.value(
                    statuses=[
                        self.status(
                            wamid,
                            "failed",
                            callback=binding.client_message_id,
                            errors=errors,
                        )
                    ]
                )
            )
        )
        self.assertFalse(inboxes, "a failed status never becomes an inbox event")
        self.assertNotIn("texto livre", str(delivery.item_ids.payload_json))
        failure = (
            self.env["contact.center.whatsapp.cloud.delivery.failure"]
            .sudo()
            .search([("wamid", "=", wamid)])
        )
        self.assertEqual(len(failure), 1)
        self.assertEqual(failure.kind, "status_failed")
        self.assertEqual(failure.code, 131026)
        self.assertEqual(failure.title, "Message undeliverable")
        self.assertEqual(failure.message_binding_id, binding)
        binding.invalidate_recordset()
        self.assertEqual(binding.delivery_state, "sent")
        # Idempotent on redispatch.
        for dispatch in self.fanout(delivery):
            self.assertEqual(
                self.dispatch_one(dispatch)["result_ref"], "wac-failure:%s" % failure.id
            )

    def test_failure_rows_are_admin_read_only_and_company_scoped(self):
        _outbox, binding, wamid = self._sent_message()
        self.deliver(self.envelope(self.value(statuses=[self.status(wamid, "failed")])))
        failure_model = self.env["contact.center.whatsapp.cloud.delivery.failure"]
        failure = failure_model.sudo().search([("wamid", "=", wamid)]).ensure_one()
        as_admin = failure_model.with_user(self.admin)
        self.assertEqual(as_admin.search([("id", "=", failure.id)]), failure)
        self.assertEqual(failure.with_user(self.admin).code, 0)
        with self.assertRaises(AccessError):
            failure_model.with_user(self.agent).search([])
        with self.assertRaises(AccessError):
            failure.with_user(self.agent).read(["code"])
        with self.assertRaises(AccessError):
            failure.with_user(self.admin).write({"code": 1})
        with self.assertRaises(AccessError):
            failure.with_user(self.admin).unlink()
        with self.assertRaises(AccessError):
            as_admin.create(
                {
                    "kind": "webhook_error",
                    "connection_id": self.connection.id,
                    "occurred_at": "2026-09-27 00:00:00",
                    "occurrence_sha256": "0" * 64,
                }
            )
        with self.assertRaises(AccessError):
            failure.sudo().write({"code": 1})
        # The company rule hides another company's diagnostics.
        other_company = self.env["res.company"].create({"name": "WAC Failure Co"})
        self.env.cr.execute(
            "UPDATE contact_center_whatsapp_cloud_delivery_failure "
            "SET company_id = %s WHERE id = %s",
            [other_company.id, failure.id],
        )
        failure.invalidate_recordset()
        self.assertFalse(as_admin.search([("id", "=", failure.id)]))

    # -- 7b -----------------------------------------------------------------------

    def test_uncertain_send_is_correlated_by_its_callback_status(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel)
        binding = outbox.message_binding_id
        with mock.patch(
            GRAPH_PATCH, side_effect=requests.Timeout("synthetic timeout")
        ) as request:
            self._run_job(outbox)
            self.assertEqual(outbox.state, "uncertain")
            self.assertFalse(binding.external_message_id)
            # The worker never re-sends an uncertain command.
            self.assertFalse(self._run_job(outbox))
            self.assertEqual(request.call_count, 1)
        sent_payload = outbox.provider_request_json["payload"]
        self.assertEqual(
            sent_payload["biz_opaque_callback_data"], binding.client_message_id
        )
        wamid = self.wamid("uncertain")
        inbox = self._status(wamid, "delivered", callback=binding.client_message_id)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        binding.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        self.assertEqual(binding.delivery_state, "delivered")
        outbox.invalidate_recordset()
        self.assertEqual(outbox.state, "done")
        self.assertEqual(
            outbox.provider_response_json["reconciled_by"], "delivery_receipt"
        )
        # A later status by wamid only still correlates with the same message.
        self._status(wamid, "read")
        self.assertEqual(binding.delivery_state, "read")

    def test_status_racing_dispatch_finalization_is_retried_not_duplicated(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel)
        binding = outbox.message_binding_id
        with mock.patch(GRAPH_PATCH, side_effect=requests.Timeout("timeout")):
            self._run_job(outbox)
        self.assertEqual(outbox.state, "uncertain")
        # The worker still owns the durable boundary.
        outbox.sudo().write({"state": "processing"})
        wamid = self.wamid("race")
        delivery = self.create_delivery(
            self.envelope(
                self.value(
                    statuses=[
                        self.status(
                            wamid, "delivered", callback=binding.client_message_id
                        )
                    ]
                )
            )
        )
        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        inbox.sudo().write({"queue_job_uuid": str(uuid.uuid4())})
        with self.assertRaises(RetryableJobError):
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        binding.invalidate_recordset()
        self.assertFalse(binding.external_message_id)
        # Finalization completes; the retried receipt applies exactly once.
        outbox.sudo().write({"state": "uncertain"})
        self.process_inbox(inbox)
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        binding.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        self.assertEqual(self._delivery_states(binding).count("delivered"), 1)
        self.assertEqual(self._delivery_states(binding).count("sent"), 1)

    # -- statuses of our own reactions (L11-STATUS-01) ----------------------------

    def _reaction(self):
        channel, inbound = self._conversation()
        with trap_jobs():
            self.env["contact.center.ui.api"].with_user(self.agent).react_message(
                channel.id, inbound.message_id.id, "👍", "add", str(uuid.uuid4())
            )
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("command_type", "=", "react")], order="id desc", limit=1)
        )

    def _send_reaction(self, outbox):
        wamid = self.wamid("reaction")
        with mock.patch(GRAPH_PATCH, return_value=self._accepted(wamid, self.CUSTOMER)):
            outbox._process_one()
        outbox.invalidate_recordset()
        self.assertEqual(outbox.state, "done", outbox.last_error_message)
        return wamid

    def test_status_of_our_reaction_is_acknowledged_without_an_inbox_event(self):
        outbox = self._reaction()
        wamid = self._send_reaction(outbox)
        self.assertEqual(outbox.provider_response_json["message_id"], wamid)
        self.assertEqual(outbox.wac_reaction_message_id, wamid)
        inboxes = self.env["contact.center.inbox.event"].sudo()
        before = inboxes.search_count([])
        delivery = self.create_delivery(
            self.envelope(self.value(statuses=[self.status(wamid, "sent")]))
        )
        (result,) = self.dispatch_all(delivery)
        self.assertEqual(result["result_ref"], "wac-reaction:%s" % outbox.id)
        self.assertEqual(inboxes.search_count([]), before)

    def test_reaction_status_ahead_of_its_send_record_is_retried_then_done(self):
        outbox = self._reaction()
        wamid = self.wamid("early")
        delivery = self.create_delivery(
            self.envelope(self.value(statuses=[self.status(wamid, "sent")]))
        )
        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        inbox.sudo().write({"queue_job_uuid": str(uuid.uuid4())})
        with self.assertRaises(RetryableJobError):
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        with mock.patch(GRAPH_PATCH, return_value=self._accepted(wamid, self.CUSTOMER)):
            outbox._process_one()
        self.assertEqual(outbox.wac_reaction_message_id, wamid)
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)

    def test_contradictory_callback_status_never_rewrites_the_message_id(self):
        _outbox, binding, wamid = self._sent_message()
        inbox = self._status(
            self.wamid("other"), "delivered", callback=binding.client_message_id
        )
        self.assertEqual(inbox.state, "dead")
        binding.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        self.assertEqual(binding.delivery_state, "sent")

    # -- CC-WAC-04: the status recipient must be the message's conversation -------

    def _uncertain_send(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel)
        with mock.patch(GRAPH_PATCH, side_effect=requests.Timeout("timeout")):
            self._run_job(outbox)
        self.assertEqual(outbox.state, "uncertain")
        return outbox, outbox.message_binding_id

    def _aliases(self, binding):
        return set(
            binding.channel_binding_id.alias_ids.mapped(
                lambda alias: (alias.namespace, alias.value_normalized)
            )
        )

    def _foreign_recipient_channels(self):
        return (
            self.env["contact.center.channel.alias"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    (
                        "value_normalized",
                        "=",
                        "%s@s.whatsapp.net" % self.OTHER_CUSTOMER,
                    ),
                ]
            )
        )

    def test_callback_status_of_an_unknown_recipient_is_refused(self):
        outbox, binding = self._uncertain_send()
        aliases = self._aliases(binding)
        state = binding.delivery_state
        inbox = self._status(
            self.wamid("foreign"),
            "delivered",
            callback=binding.client_message_id,
            recipient=self.OTHER_CUSTOMER,
        )
        self.assertEqual(inbox.state, "dead")
        binding.invalidate_recordset()
        outbox.invalidate_recordset()
        self.assertFalse(binding.external_message_id)
        self.assertEqual(binding.delivery_state, state)
        self.assertEqual(outbox.state, "uncertain")
        self.assertEqual(self._aliases(binding), aliases)
        self.assertFalse(self._foreign_recipient_channels())

    def test_known_message_status_of_an_unknown_recipient_is_refused(self):
        _outbox, binding, wamid = self._sent_message()
        aliases = self._aliases(binding)
        by_callback = self._status(
            wamid,
            "delivered",
            callback=binding.client_message_id,
            recipient=self.OTHER_CUSTOMER,
        )
        by_wamid = self._status(wamid, "read", recipient=self.OTHER_CUSTOMER)
        self.assertEqual((by_callback | by_wamid).mapped("state"), ["dead", "dead"])
        binding.invalidate_recordset()
        self.assertEqual(binding.delivery_state, "sent")
        self.assertEqual(self._aliases(binding), aliases)
        self.assertFalse(self._foreign_recipient_channels())

    def test_failed_callback_status_of_an_unknown_recipient_stays_unlinked(self):
        outbox, binding = self._uncertain_send()
        wamid = self.wamid("foreign-failed")
        _delivery, inboxes = self.deliver(
            self.envelope(
                self.value(
                    statuses=[
                        self.status(
                            wamid,
                            "failed",
                            callback=binding.client_message_id,
                            recipient=self.OTHER_CUSTOMER,
                        )
                    ]
                )
            )
        )
        self.assertFalse(inboxes)
        failure = (
            self.env["contact.center.whatsapp.cloud.delivery.failure"]
            .sudo()
            .search([("wamid", "=", wamid)])
            .ensure_one()
        )
        self.assertFalse(failure.message_binding_id)
        binding.invalidate_recordset()
        self.assertFalse(binding.external_message_id)
        outbox.invalidate_recordset()
        self.assertEqual(outbox.state, "uncertain")

    # -- CC-WAC-05: a timeout followed only by ``failed`` is reconciled -----------

    def test_uncertain_send_followed_only_by_failed_is_reconciled(self):
        outbox, binding = self._uncertain_send()
        wamid = self.wamid("only-failed")
        status = self.status(
            wamid,
            "failed",
            callback=binding.client_message_id,
            errors=[{"code": 131026, "title": "Message undeliverable"}],
        )
        delivery, inboxes = self.deliver(self.envelope(self.value(statuses=[status])))
        self.assertFalse(inboxes, "a failed status never becomes an inbox event")
        failures = self.env["contact.center.whatsapp.cloud.delivery.failure"].sudo()
        failure = failures.search([("wamid", "=", wamid)]).ensure_one()
        self.assertEqual(failure.message_binding_id, binding)
        binding.invalidate_recordset()
        outbox.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        # Accepted by the provider; ``failed`` stays a local diagnostic.
        self.assertEqual(binding.delivery_state, "sent")
        self.assertEqual(outbox.state, "done")
        self.assertEqual(
            outbox.provider_response_json["reconciled_by"], "delivery_receipt"
        )
        # Repeated dispatch and a redelivery in another body are idempotent.
        for dispatch in self.fanout(delivery):
            self.assertEqual(
                self.dispatch_one(dispatch)["result_ref"], "wac-failure:%s" % failure.id
            )
        envelope = self.envelope(self.value(statuses=[status]))
        envelope["entry"][0]["time"] = 1_900_000_000
        self.deliver(envelope)
        self.assertEqual(failures.search_count([("wamid", "=", wamid)]), 1)
        self.assertEqual(self._delivery_states(binding).count("sent"), 1)
        binding.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        # A later status of the same message still correlates with it.
        self._status(wamid, "read", callback=binding.client_message_id)
        self.assertEqual(binding.delivery_state, "read")

    def test_failed_status_racing_dispatch_finalization_is_retried(self):
        outbox, binding = self._uncertain_send()
        outbox.sudo().write({"state": "processing"})
        wamid = self.wamid("failed-race")
        delivery = self.create_delivery(
            self.envelope(
                self.value(
                    statuses=[
                        self.status(wamid, "failed", callback=binding.client_message_id)
                    ]
                )
            )
        )
        dispatch = self.fanout(delivery).ensure_one()
        with self.assertRaises(RetryableJobError):
            self.dispatch_one(dispatch)
        binding.invalidate_recordset()
        self.assertFalse(binding.external_message_id)
        outbox.sudo().write({"state": "uncertain"})
        self.assertTrue(self.dispatch_one(dispatch)["handled"])
        binding.invalidate_recordset()
        outbox.invalidate_recordset()
        self.assertEqual(binding.external_message_id, wamid)
        self.assertEqual(outbox.state, "done")

    # -- CC-WAC-15: statuses after the sending connection was replaced --------

    STATUS_AT = 1_800_000_000

    def _replace_sending_connection(self):
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        replacement = self._make_connected(
            self._create_connection(self.account, self.waba_asset)
        )
        replacement.action_whatsapp_cloud_configure_webhook()
        return replacement

    def test_late_statuses_after_replacement_reach_the_original_message(self):
        _outbox, binding, wamid = self._sent_message()
        callback = binding.client_message_id
        replacement = self._replace_sending_connection()
        inbox = self._status(
            wamid, "delivered", callback=callback, timestamp=self.STATUS_AT
        )
        self.assertEqual(inbox.provider_connection_id, replacement)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        binding.invalidate_recordset()
        self.assertEqual(binding.provider_connection_id, self.connection)
        self.assertEqual(binding.delivery_state, "delivered")
        # A status by ``wamid`` only.
        inbox = self._status(wamid, "read", timestamp=self.STATUS_AT + 1)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        binding.invalidate_recordset()
        self.assertEqual(binding.delivery_state, "read")
        # ``failed`` is linked to the original message too.
        self.deliver(
            self.envelope(
                self.value(
                    statuses=[
                        self.status(
                            wamid,
                            "failed",
                            callback=callback,
                            timestamp=self.STATUS_AT + 2,
                        )
                    ]
                )
            )
        )
        failure = (
            self.env["contact.center.whatsapp.cloud.delivery.failure"]
            .sudo()
            .search([("wamid", "=", wamid)])
            .ensure_one()
        )
        self.assertEqual(failure.connection_id, replacement)
        self.assertEqual(failure.message_binding_id, binding)
        binding.invalidate_recordset()
        self.assertEqual(binding.delivery_state, "read")

    def test_statuses_after_replacement_keep_the_recipient_check(self):
        _outbox, binding, wamid = self._sent_message()
        aliases = self._aliases(binding)
        self._replace_sending_connection()
        inbox = self._status(
            wamid,
            "delivered",
            callback=binding.client_message_id,
            recipient=self.OTHER_CUSTOMER,
            timestamp=self.STATUS_AT,
        )
        self.assertEqual(inbox.state, "dead")
        binding.invalidate_recordset()
        self.assertEqual(binding.delivery_state, "sent")
        self.assertEqual(self._aliases(binding), aliases)
        self.assertFalse(self._foreign_recipient_channels())

    # -- CC-WAC-19: reactions to a message of a replaced connection -------------

    def _reaction_message(self, target, emoji, **values):
        return self.typed_message(
            "reaction", {"message_id": target, "emoji": emoji}, **values
        )

    def _mutations(self, binding):
        return (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search([("target_message_binding_id", "=", binding.id)], order="id")
        )

    def _shown_reactions(self, binding):
        binding.message_id.invalidate_recordset(["reaction_ids"])
        return binding.message_id.sudo().reaction_ids.mapped("content")

    def test_reactions_after_replacement_reach_the_original_message(self):
        _outbox, binding, wamid = self._sent_message()
        replacement = self._replace_sending_connection()
        # Reactions are ordered by their own timestamps: keep them explicit.
        add = self._reaction_message(wamid, "👍", timestamp=self.STATUS_AT)
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[add])))
        self.assertEqual(inbox.provider_connection_id, replacement)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        added = self._mutations(binding).ensure_one()
        self.assertEqual(added.provider_connection_id, self.connection)
        self.assertEqual((added.reaction_operation, added.reaction_emoji), ("add", "👍"))
        self.assertEqual(self._shown_reactions(binding), ["👍"])
        # A duplicate in another body, and a reprocess, apply nothing more.
        envelope = self.envelope(self.value(messages=[add]))
        envelope["entry"][0]["time"] = 1_900_000_020
        _delivery, again = self.deliver(envelope)
        self.assertEqual(again, inbox)
        inbox.sudo().write({"state": "pending"})
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(len(self._mutations(binding)), 1)
        # The removal empties the reaction shown on the original message.
        remove = self._reaction_message(wamid, "", timestamp=self.STATUS_AT + 5)
        _delivery, removal = self.deliver(self.envelope(self.value(messages=[remove])))
        self.assertEqual(removal.state, "done", removal.last_error_message)
        self.assertEqual(
            self._mutations(binding).mapped("reaction_operation"), ["add", "remove"]
        )
        self.assertEqual(self._shown_reactions(binding), [])

    def test_reactions_never_reach_another_route_or_come_from_another_contact(self):
        # A message of another inbox's number is not on this stable route.
        other = self._create_connection(
            self._create_account(team=self.team), self.waba_asset, self.OTHER_PHONE_ID
        )
        foreign = self.text_message(body="Outro número")
        self.deliver(
            self.envelope(self.value(messages=[foreign], phone_id=self.OTHER_PHONE_ID))
        )
        foreign_binding = self.binding_for(foreign["id"], other)
        self.assertTrue(foreign_binding)
        _outbox, binding, wamid = self._sent_message()
        aliases = self._aliases(binding)
        self._replace_sending_connection()
        delivery = self.create_delivery(
            self.envelope(
                self.value(
                    messages=[
                        self._reaction_message(
                            foreign["id"], "👍", timestamp=self.STATUS_AT
                        )
                    ]
                )
            )
        )
        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        inbox.sudo().write({"queue_job_uuid": str(uuid.uuid4())})
        with self.assertRaises(RetryableJobError):
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        self.assertFalse(self._mutations(foreign_binding))
        # A reaction from another contact to this route's message is refused.
        stranger = self._reaction_message(
            wamid,
            "👎",
            sender=self.OTHER_CUSTOMER,
            user_id=self.OTHER_BSUID,
            timestamp=self.STATUS_AT,
        )
        _delivery, refused = self.deliver(
            self.envelope(
                self.value(
                    messages=[stranger],
                    contacts=[
                        self.contact_block(
                            wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID
                        )
                    ],
                )
            )
        )
        self.assertEqual(refused.state, "dead")
        self.assertFalse(self._mutations(binding))
        self.assertEqual(self._aliases(binding), aliases)
        self.assertFalse(self._foreign_recipient_channels())

    # -- CC-WAC-20: one diagnostic per failed status across a replacement -------

    def test_repeated_failed_status_after_replacement_is_recorded_once(self):
        _outbox, binding, wamid = self._sent_message()
        failed = self.status(
            wamid,
            "failed",
            callback=binding.client_message_id,
            timestamp=self.STATUS_AT,
            errors=[{"code": 131026, "title": "Message undeliverable"}],
        )
        self.deliver(self.envelope(self.value(statuses=[failed])))
        failures = self.env["contact.center.whatsapp.cloud.delivery.failure"].sudo()
        first = failures.search([("wamid", "=", wamid)]).ensure_one()
        self.assertEqual(first.connection_id, self.connection)
        self._replace_sending_connection()
        # Meta repeats the unchanged status in a differently batched body.
        envelope = self.envelope(
            self.value(statuses=[failed, self.status(self.wamid("other"), "sent")])
        )
        envelope["entry"][0]["time"] = 1_900_000_030
        delivery = self.create_delivery(envelope)
        results = [self.dispatch_one(row) for row in self.fanout(delivery)]
        self.assertEqual(results[0]["result_ref"], "wac-failure:%s" % first.id)
        self.assertEqual(failures.search([("wamid", "=", wamid)]), first)
        self.assertEqual(first.message_binding_id, binding)
