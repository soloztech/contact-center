import json
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.dto import AttributionDTO, DTOValidationError, EventDTO


class TestContactCenterAttribution(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = cls._create_user("Attribution Agent", agent_group)
        cls.admin = cls._create_user("Attribution Admin", admin_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Attribution %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Attribution Inbox %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "attribution-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Attribution Fake Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "attribution-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )

    @classmethod
    def _create_user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": "cc-attribution-%s" % uuid.uuid4(),
                    "email": "cc-attribution-%s@example.invalid" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _attribution(self, **overrides):
        values = {
            "touchpoint_type": "paid_ad_click",
            "evidence_level": "provider_asserted",
            "network": "whatsapp",
            "source_url": "https://example.invalid/private-campaign",
            "external_identifiers": [
                {
                    "namespace": "meta.source_id",
                    "role": "ad_source",
                    "value": "secret-ad-id",
                    "source_field": "contextInfo.externalAdReply.sourceID",
                }
            ],
            "entry_point": {"source": "ctwa_ad"},
            "flags": {"show_ad_attribution": True},
            "provider_extensions": {
                "provider.wuzapi": {"opaque_fingerprint": "secret-extension"}
            },
        }
        values.update(overrides)
        return values

    def _event(
        self,
        *,
        attribution=None,
        event_type="message.created",
        event_id=None,
        external_message_id=None,
        include_message=True,
        person_address=None,
        conversation_ref=None,
        text="Attribution fixture",
    ):
        person_address = person_address or (
            "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        )
        values = {
            "schema_version": 1,
            "provider_schema_version": "fixture-v1",
            "event_id": event_id or "attribution-event-%s" % uuid.uuid4(),
            "event_type": event_type,
            "occurred_at": "2026-08-25T13:00:00Z",
            "account_ref": self.account.external_ref,
            "connection_ref": self.connection.external_ref,
            "conversation_ref": conversation_ref or "conversation-%s" % person_address,
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {
                "display_name": "Attributed Person",
                "addresses": [
                    {
                        "namespace": "whatsapp.pn",
                        "value": person_address,
                        "value_normalized": person_address,
                        "role": "primary",
                        "confidence": "protocol",
                        "resolution_scope": "company",
                    }
                ],
            },
            "conversation": {
                "conversation_type": "direct",
                "addresses": [
                    {
                        "namespace": "whatsapp.pn",
                        "value": person_address,
                        "value_normalized": person_address,
                        "role": "primary",
                        "confidence": "protocol",
                        "resolution_scope": "company",
                    }
                ],
            },
            "attribution": [attribution or self._attribution()],
        }
        if include_message:
            values["message"] = {
                "external_message_id": external_message_id
                or "attribution-message-%s" % uuid.uuid4(),
                "content_type": "text",
                "text": text,
            }
        return EventDTO.from_dict(values)

    def _inbox(self, event, suffix=None):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "attribution:%s" % (suffix or uuid.uuid4()),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

    def _process_job(self, event):
        inbox = self._inbox(event)
        self._run_inbox_job(inbox)
        return inbox

    def _run_inbox_job(self, inbox, job_uuid=None):
        job_uuid = job_uuid or inbox.queue_job_uuid or str(uuid.uuid4())
        if not inbox.queue_job_uuid:
            inbox.sudo().write({"queue_job_uuid": job_uuid})
        return inbox.with_context(job_uuid=job_uuid)._job_process()

    def test_dto_roundtrip_and_bounded_contract(self):
        event = self._event()
        roundtrip = EventDTO.from_dict(event.to_dict())

        self.assertEqual(roundtrip, event)
        self.assertIsInstance(roundtrip.attribution[0], AttributionDTO)
        identifier = roundtrip.attribution[0].external_identifiers[0]
        self.assertEqual(len(identifier.comparison_hash), 64)
        self.assertNotEqual(identifier.comparison_hash, identifier.value)

        invalid = self._attribution(utm={"not_allowed": "value"})
        with self.assertRaises(DTOValidationError):
            AttributionDTO.from_dict(invalid)
        invalid = self._attribution(source_url="data:text/html,unsafe")
        with self.assertRaises(DTOValidationError):
            AttributionDTO.from_dict(invalid)
        invalid = self._attribution(provider_extensions={"raw": {"id": "secret"}})
        with self.assertRaises(DTOValidationError):
            AttributionDTO.from_dict(invalid)
        invalid = self._attribution(
            entry_point={"source": "ctwa_ad", "delay_seconds": 2**63}
        )
        with self.assertRaises(DTOValidationError):
            AttributionDTO.from_dict(invalid)
        duplicate_types = event.to_dict()
        duplicate_types["attribution"] = [
            self._attribution(),
            self._attribution(source_platform="meta"),
        ]
        with self.assertRaises(DTOValidationError):
            EventDTO.from_dict(duplicate_types)

    def test_capture_is_idempotent_and_monotonically_enriches(self):
        external_message_id = "stable-attribution-message-%s" % uuid.uuid4()
        first_event = self._event(external_message_id=external_message_id)
        first_inbox = self._inbox(first_event, "first-%s" % uuid.uuid4())
        touchpoints = self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, first_event, first_inbox
        )
        duplicate = self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, first_event, first_inbox
        )

        self.assertEqual(touchpoints, duplicate)
        self.assertEqual(len(touchpoints), 1)
        self.assertEqual(len(touchpoints.identifier_ids), 1)
        self.assertEqual(touchpoints.enrichment_state, "initial")

        enriched_attribution = self._attribution(
            source_platform="meta",
            source_type="ad",
            utm={
                "source": "facebook",
                "medium": "paid_social",
                "campaign": "solar-2026",
            },
            creative={"media_type": "image"},
            external_identifiers=[
                {
                    "namespace": "meta.source_id",
                    "role": "ad_source",
                    "value": "secret-ad-id",
                    "source_field": "contextInfo.externalAdReply.sourceID",
                },
                {
                    "namespace": "meta.ctwa_clid",
                    "role": "click",
                    "value": "secret-click-id",
                    "source_field": "contextInfo.externalAdReply.ctwaClid",
                },
            ],
        )
        second_values = first_event.to_dict()
        second_values["event_id"] = "attribution-event-%s" % uuid.uuid4()
        second_values["occurred_at"] = "2026-08-25T14:00:00Z"
        second_values["attribution"] = [enriched_attribution]
        second_event = EventDTO.from_dict(second_values)
        second_inbox = self._inbox(second_event, "second-%s" % uuid.uuid4())
        enriched = self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, second_event, second_inbox
        )

        touchpoints.invalidate_recordset()
        self.assertEqual(enriched, touchpoints)
        self.assertEqual(
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search_count([("id", "=", touchpoints.id)]),
            1,
        )
        self.assertEqual(touchpoints.enrichment_state, "enriched")
        self.assertEqual(touchpoints.source_platform, "meta")
        self.assertEqual(touchpoints.utm_campaign, "solar-2026")
        self.assertEqual(len(touchpoints.evidence_inbox_event_ids), 2)
        self.assertEqual(len(touchpoints.identifier_ids), 2)
        new_identifier = touchpoints.identifier_ids.filtered(
            lambda item: item.namespace == "meta.ctwa_clid"
        )
        self.assertEqual(len(new_identifier), 1)
        self.assertEqual(
            fields.Datetime.to_string(new_identifier.observed_at),
            "2026-08-25 14:00:00",
        )

    def test_capture_defers_projection_foreign_keys_until_after_message_locking(self):
        person_address = "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        first = self._process_job(self._event(person_address=person_address))
        first_touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", first.id)], limit=1)
        )
        second_event = self._event(person_address=person_address)
        second_inbox = self._inbox(second_event)

        touchpoint = second_inbox._capture_attribution(second_event)

        self.assertFalse(touchpoint.message_binding_id)
        self.assertFalse(touchpoint.channel_binding_id)
        self.assertFalse(touchpoint.identity_id)

        second_inbox._process_normalized(second_event, touchpoint)
        touchpoint.invalidate_recordset()
        self.assertEqual(
            touchpoint.channel_binding_id, first_touchpoint.channel_binding_id
        )
        self.assertTrue(touchpoint.message_binding_id)
        self.assertEqual(touchpoint.identity_id, first_touchpoint.identity_id)

    def test_attribution_observation_is_done_without_creating_chat_records(self):
        event = self._event(
            event_type="attribution.observed",
            include_message=False,
        )
        before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "mail.channel",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
            )
        }

        inbox = self._process_job(event)

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "done")
        for model, count in before.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count, model)
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertTrue(touchpoint)
        self.assertEqual(touchpoint.source_key_kind, "event")
        self.assertFalse(touchpoint.message_binding_id)
        self.assertFalse(touchpoint.channel_binding_id)
        self.assertFalse(touchpoint.identity_id)

    def test_attribution_observation_links_an_existing_conversation_without_message(
        self,
    ):
        person_address = "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        first = self._process_job(self._event(person_address=person_address))
        first_touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", first.id)], limit=1)
        )
        message_count = self.env["mail.message"].sudo().search_count([])

        observation = self._event(
            event_type="attribution.observed",
            include_message=False,
            person_address=person_address,
        )
        inbox = self._process_job(observation)
        observed = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )

        self.assertEqual(inbox.state, "done")
        self.assertEqual(
            self.env["mail.message"].sudo().search_count([]), message_count
        )
        self.assertEqual(
            observed.channel_binding_id, first_touchpoint.channel_binding_id
        )
        self.assertEqual(observed.identity_id, first_touchpoint.identity_id)
        self.assertFalse(observed.message_binding_id)

    def test_observation_uses_explicit_channel_projection_when_reference_changes(self):
        person_address = "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        first = self._process_job(self._event(person_address=person_address))
        first_touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", first.id)], limit=1)
        )
        observation = self._event(
            event_type="attribution.observed",
            include_message=False,
            person_address=person_address,
            conversation_ref="rotated-reference-%s" % uuid.uuid4(),
        )

        with trap_jobs() as trap:
            inbox = self._process_job(observation)
            reconciliation_jobs = [
                job
                for job in trap.enqueued_jobs
                if job.channel == "root.contact_center.attribution"
            ]
            self.assertFalse(reconciliation_jobs)

        observed = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertEqual(
            observed.channel_binding_id, first_touchpoint.channel_binding_id
        )
        self.assertEqual(observed.identity_id, first_touchpoint.identity_id)
        self.assertFalse(observed.message_binding_id)

    def test_pending_observation_matches_exact_fingerprint_not_conversation_reference(
        self,
    ):
        person_address = "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        observation = self._event(
            event_type="attribution.observed",
            include_message=False,
            person_address=person_address,
        )
        first = self._process_job(observation)
        pending = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", first.id)], limit=1)
        )
        self.assertFalse(pending.channel_binding_id)

        second = self._process_job(
            self._event(
                person_address=person_address,
                conversation_ref="later-reference-%s" % uuid.uuid4(),
            )
        )
        projected = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", second.id)], limit=1)
        )

        pending.invalidate_recordset()
        self.assertEqual(pending.channel_binding_id, projected.channel_binding_id)
        self.assertEqual(pending.identity_id, projected.identity_id)
        self.assertFalse(pending.message_binding_id)

    def test_standalone_capture_failure_is_dead_and_can_be_requeued(self):
        event = self._event(
            event_type="attribution.observed",
            include_message=False,
        )
        inbox = self._inbox(event)
        touchpoint_model_type = type(self.env["contact.center.attribution.touchpoint"])

        with mock.patch.object(
            touchpoint_model_type,
            "_capture_event",
            side_effect=ValidationError("invalid primary attribution"),
        ):
            self.assertTrue(self._run_inbox_job(inbox))

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.last_error_class, "ValidationError")
        self.assertNotIn("attribution_capture_failure", inbox.metadata_json or {})
        self.assertFalse(
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )

        with trap_jobs() as trap:
            inbox.with_user(self.admin).action_requeue()
            trap.assert_jobs_count(1)
        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "pending")

    def test_reconciliation_job_repairs_referral_first_message_interleaving(self):
        person_address = "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]
        observation = self._event(
            event_type="attribution.observed",
            include_message=False,
            person_address=person_address,
        )

        with trap_jobs() as trap:
            inbox = self._process_job(observation)
            reconciliation_jobs = [
                job
                for job in trap.enqueued_jobs
                if job.channel == "root.contact_center.attribution"
            ]
            self.assertEqual(len(reconciliation_jobs), 1)
            queued_job = reconciliation_jobs[0]

        pending = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertFalse(pending.channel_binding_id)
        self.assertEqual(queued_job.channel, "root.contact_center.attribution")
        self.assertEqual(queued_job.priority, 40)
        self.assertEqual(
            queued_job.identity_key,
            "contact_center:attribution_reconcile:%s" % pending.id,
        )
        with self.assertRaises(RetryableJobError) as raised:
            pending._job_reconcile_projection()
        self.assertIsNone(raised.exception.seconds)

        # Simulate the first message committing in a concurrent transaction: the
        # application projection exists, while the message-side ledger linker did not
        # see this touchpoint in its older REPEATABLE READ snapshot.
        self.env["contact.center.application"]._process_event(
            self.connection,
            self._event(
                person_address=person_address,
                conversation_ref="concurrent-message-%s" % uuid.uuid4(),
            ),
        )

        self.assertTrue(pending._job_reconcile_projection())
        pending.invalidate_recordset(["channel_binding_id", "identity_id"])
        self.assertTrue(pending.channel_binding_id)
        self.assertEqual(pending.identity_id, pending.channel_binding_id.identity_id)
        self.assertFalse(pending.message_binding_id)
        linked_binding = pending.channel_binding_id

        self.assertTrue(pending._job_reconcile_projection())
        pending.invalidate_recordset(["channel_binding_id"])
        self.assertEqual(pending.channel_binding_id, linked_binding)

    def test_reconciliation_stops_after_bounded_absence(self):
        event = self._event(
            event_type="attribution.observed",
            include_message=False,
        )
        with trap_jobs():
            inbox = self._process_job(event)
        pending = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )

        with mock.patch.object(
            type(pending),
            "_reconciliation_job_attempt",
            return_value=3,
        ):
            self.assertFalse(pending._job_reconcile_projection())
        self.assertFalse(pending.channel_binding_id)

    def test_attribution_observation_rejects_a_message_payload(self):
        event = self._event(event_type="attribution.observed")

        with self.assertRaisesRegex(
            ValidationError,
            "only attribution evidence",
        ):
            self.env["contact.center.application"]._process_event(
                self.connection,
                event,
            )

    def test_invalid_ledger_capture_is_durable_but_does_not_drop_message(self):
        event = self._event()
        inbox = self._inbox(event)
        touchpoint_model_type = type(self.env["contact.center.attribution.touchpoint"])

        with mock.patch.object(
            touchpoint_model_type,
            "_capture_event",
            side_effect=ValidationError("invalid optional attribution"),
        ):
            self.assertTrue(self._run_inbox_job(inbox))

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "done")
        self.assertEqual(
            inbox.metadata_json["attribution_capture_failure"]["error_class"],
            "ValidationError",
        )
        self.assertTrue(
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("external_message_id", "=", event.message.external_message_id)])
        )

    def test_conflicting_network_or_evidence_hides_the_touchpoint(self):
        external_message_id = "semantic-conflict-message-%s" % uuid.uuid4()
        first_event = self._event(
            external_message_id=external_message_id,
            attribution=self._attribution(
                flags={
                    "show_ad_attribution": True,
                    "always_show_ad_attribution": True,
                }
            ),
        )
        first_inbox = self._inbox(first_event, "semantic-first-%s" % uuid.uuid4())
        touchpoint = self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, first_event, first_inbox
        )
        self.assertTrue(touchpoint.always_show_ad_attribution)

        second_values = first_event.to_dict()
        second_values["event_id"] = "attribution-event-%s" % uuid.uuid4()
        second_values["attribution"] = [
            self._attribution(network="another_network", evidence_level="observed")
        ]
        second_event = EventDTO.from_dict(second_values)
        second_inbox = self._inbox(second_event, "semantic-second-%s" % uuid.uuid4())
        self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.connection, second_event, second_inbox
        )

        touchpoint.invalidate_recordset()
        self.assertEqual(touchpoint.conflict_state, "conflict")
        self.assertEqual(touchpoint.network, "whatsapp")
        self.assertEqual(touchpoint.evidence_level, "provider_asserted")

    def test_unsupported_projection_keeps_attribution_evidence(self):
        event = self._event(event_type="provider.future-event")
        inbox = self._process_job(event)
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )

        self.assertEqual(inbox.state, "unsupported")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(len(touchpoint), 1)
        self.assertEqual(touchpoint.inbox_event_id, inbox)
        self.assertFalse(touchpoint.message_binding_id)
        self.assertFalse(touchpoint.channel_binding_id)

    def test_opt_in_ui_is_safe_and_scoped_to_exact_binding(self):
        first_event = self._event()
        first_inbox = self._process_job(first_event)
        first_touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", first_inbox.id)])
        )
        first_binding = first_touchpoint.channel_binding_id
        self.assertTrue(first_binding)
        api = self.env["contact.center.ui.api"].with_user(self.agent)

        disabled = api.get_attribution(first_binding.channel_id.id)
        self.assertFalse(disabled["enabled"])
        self.assertFalse(disabled["items"])

        self.account.sudo().write({"attribution_ui_enabled": True})
        second_event = self._event(
            attribution=self._attribution(
                source_platform="meta",
                source_type="ad",
                utm={"campaign": "must-not-leak-into-first-binding"},
            )
        )
        second_inbox = self._process_job(second_event)
        second_touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", second_inbox.id)])
        )
        self.assertNotEqual(
            first_touchpoint.channel_binding_id,
            second_touchpoint.channel_binding_id,
        )

        payload = api.get_attribution(first_binding.channel_id.id, limit=20)
        self.assertTrue(payload["enabled"])
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["public_ref"], first_touchpoint.public_ref)
        self.assertNotEqual(item["public_ref"], second_touchpoint.public_ref)
        self.assertEqual(
            set(item),
            {
                "public_ref",
                "touchpoint_type",
                "evidence_level",
                "network",
                "source_platform",
                "source_type",
                "entry_point_source",
                "entry_point_app",
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "creative_media_type",
                "show_ad_attribution",
                "occurred_at",
            },
        )
        serialized = json.dumps(payload, sort_keys=True)
        for secret in (
            "secret-ad-id",
            "secret-click-id",
            "https://example.invalid/private-campaign",
            "secret-extension",
            "source_url",
            "provider_extensions",
            "external_identifiers",
        ):
            self.assertNotIn(secret, serialized)

    def test_admin_can_view_attribution_when_agent_flag_is_disabled(self):
        self.account.write({"access_user_ids": [(4, self.admin.id)]})
        inbox = self._process_job(self._event())
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )
        channel_id = touchpoint.channel_binding_id.channel_id.id
        self.assertFalse(self.account.attribution_ui_enabled)
        admin_api = self.env["contact.center.ui.api"].with_user(self.admin)
        self.assertTrue(admin_api.get_attribution(channel_id)["enabled"])
        agent_api = self.env["contact.center.ui.api"].with_user(self.agent)
        self.assertFalse(agent_api.get_attribution(channel_id)["enabled"])
        # Projection code uses sudo to read technical flags, but the original
        # actor must still determine whether the administrator bypass applies.
        self.assertFalse(
            self.account.with_user(self.agent)
            .sudo()
            ._contact_center_user_can_view_attribution()
        )

    def test_conversation_action_policies_are_admin_or_explicit_agent_opt_in(self):
        account = self.account.with_user(self.agent)
        for action in ("delete", "ignore"):
            self.assertFalse(
                account._contact_center_user_can_manage_conversation(action)
            )
            self.assertFalse(
                account.sudo()._contact_center_user_can_manage_conversation(action)
            )
            self.assertTrue(
                self.account.with_user(
                    self.admin
                )._contact_center_user_can_manage_conversation(action)
            )
        self.account.write({"conversation_delete_enabled": True})
        self.assertTrue(account._contact_center_user_can_manage_conversation("delete"))
        self.assertFalse(account._contact_center_user_can_manage_conversation("ignore"))
        self.account.write({"conversation_ignore_enabled": True})
        self.assertTrue(account._contact_center_user_can_manage_conversation("ignore"))
        with self.assertRaises(AccessError):
            account.write({"conversation_delete_enabled": False})
        with self.assertRaises(ValidationError):
            account._contact_center_user_can_manage_conversation("unknown")

    def test_ledger_is_admin_read_only_and_hidden_from_agents(self):
        inbox = self._process_job(self._event())
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )
        identifier = touchpoint.identifier_ids

        with self.assertRaises(AccessError):
            self.env["contact.center.attribution.touchpoint"].with_user(
                self.agent
            ).search([])
        with self.assertRaises(AccessError):
            self.env["contact.center.attribution.identifier"].with_user(
                self.agent
            ).search([])
        self.assertEqual(
            self.env["contact.center.attribution.touchpoint"]
            .with_user(self.admin)
            .search([("id", "=", touchpoint.id)]),
            touchpoint,
        )
        self.assertEqual(
            self.env["contact.center.attribution.identifier"]
            .with_user(self.admin)
            .search([("id", "=", identifier.id)]),
            identifier,
        )
        with self.assertRaises(AccessError):
            touchpoint.with_user(self.admin).write({"network": "tampered"})
        with self.assertRaises(AccessError):
            touchpoint.sudo().write({"network": "tampered"})
        with self.assertRaises(AccessError):
            touchpoint.sudo().unlink()
