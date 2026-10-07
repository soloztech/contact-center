import datetime
import uuid
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
)
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.intake import EXCLUDED_CONTENT_TYPES, HUMAN_CONTENT_TYPES
from ..models.intake_policy import automation_guards_available
from .test_conversation_crm import ConversationCrmCase


@tagged("post_install", "-at_install")
class TestCrmIntake(ConversationCrmCase):
    def setUp(self):
        super().setUp()
        self.env.company.country_id = self.env.ref("base.br")
        self.account.write(
            {"crm_intake_user_id": self.agent.id, "crm_intake_enabled": True}
        )

    def _new(self, partner=None, phone="5511998765432", account=None):
        account = account or self.account
        # Model a new request rather than TransactionCase's old audit clock.
        with patch.object(
            type(self.env.cr), "now", return_value=self._after_cutoff(account)
        ):
            channel = self._channel(account, partner)
        binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", channel.id)], limit=1
        )
        if phone:
            self.env["contact.center.identity.alias"].create(
                {
                    "account_id": account.id,
                    "identity_id": binding.identity_id.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": phone + "@s.whatsapp.net",
                    "value_normalized": phone + "@s.whatsapp.net",
                    "confidence": "protocol",
                }
            )
        return binding

    def _message(self, binding, **values):
        date = values.pop("date", self._after_cutoff(binding.account_id))
        message = (
            self.env["mail.message"]
            .with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN)
            .create(
                {
                    "model": "mail.channel",
                    "res_id": binding.channel_id.id,
                    "message_type": "comment",
                    "subtype_id": self.env.ref("mail.mt_comment").id,
                    "body": "Synthetic private chat body",
                    "date": date,
                }
            )
        )
        with trap_jobs():
            return self.env["contact.center.message.binding"].create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "external_message_id": uuid.uuid4().hex,
                    **values,
                }
            )

    def _run(self, binding):
        with trap_jobs():
            binding._job_crm_intake(binding.crm_intake_revision)
        return self.env["crm.lead"].browse(binding.crm_intake_lead_snapshot)

    def _after_cutoff(self, account=None):
        account = account or self.account
        return max(
            fields.Datetime.now(),
            account.crm_intake_enabled_at or fields.Datetime.now(),
        )

    def _second_account(self):
        return self.env["contact.center.account"].create(
            {
                "name": "Second commercial",
                "platform": "whatsapp",
                "external_ref": uuid.uuid4().hex,
                "company_id": self.env.company.id,
                "access_user_ids": [(6, 0, self.agent.ids)],
                "crm_intake_user_id": self.agent.id,
                "crm_intake_enabled": True,
            }
        )

    def test_first_message_queues_then_creates_once_without_chat_copy(self):
        binding = self._new()
        before = self.env["crm.lead"].search_count([])
        self._message(binding)
        self.assertEqual(binding.crm_intake_state, "pending")
        self.assertEqual(self.env["crm.lead"].search_count([]), before)
        lead = self._run(binding)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertEqual(
            (lead.type, lead.company_id, lead.user_id),
            ("lead", self.env.company, self.agent),
        )
        self.assertTrue(lead.contact_center_intake_created)
        self.assertFalse(lead.partner_id)
        self.assertFalse(lead.team_id)
        self.assertNotIn("Synthetic private", lead.description or "")
        link = lead._conversation_links()
        self.assertEqual(
            (link.writer, link.origin, link.scope_state),
            ("intake", "created", "context"),
        )
        self._run(binding)
        self._message(binding)
        self.assertEqual(self.env["crm.lead"].search_count([]), before + 1)
        self.assertEqual(len(lead._conversation_links()), 1)

    def test_cross_inbox_phone_reuses_without_changing_existing_business(self):
        binding = self._new()
        self._message(binding)
        lead = self._run(binding)
        before = lead.read(
            [
                "user_id",
                "team_id",
                "stage_id",
                "type",
                "campaign_id",
                "source_id",
                "medium_id",
                "write_date",
            ]
        )
        second = self._new(account=self._second_account())
        self._message(second)
        reused = self._run(second)
        self.assertEqual(reused, lead)
        self.assertEqual(second.crm_intake_state, "reused")
        self.assertEqual(
            before,
            lead.read(
                [
                    "user_id",
                    "team_id",
                    "stage_id",
                    "type",
                    "campaign_id",
                    "source_id",
                    "medium_id",
                    "write_date",
                ]
            ),
        )
        self.assertEqual(
            lead._conversation_links()
            .filtered(lambda row: row.channel_id == second.channel_id)
            .origin,
            "linked",
        )

    def test_partner_master_data_is_immutable_and_mobile_dedupes(self):
        partner = self.env["res.partner"].create(
            {
                "name": "Synthetic exact partner",
                "phone": "+551132654321",
                "mobile": "+5511987654321",
                "email": "person@fixture.example",
            }
        )
        partner.flush_recordset()
        before = partner.read(["phone", "mobile", "email", "write_date"])
        binding = self._new(partner)
        self._message(binding)
        lead = self._run(binding)
        self.assertEqual(lead.partner_id, partner)
        self.assertEqual(lead.mobile, "+5511998765432")
        self.assertEqual(
            before, partner.read(["phone", "mobile", "email", "write_date"])
        )
        second = self._new(account=self._second_account())
        self._message(second)
        self.assertEqual(self._run(second), lead)
        self.assertEqual(
            before, partner.read(["phone", "mobile", "email", "write_date"])
        )

    def test_partner_exact_does_not_expand_to_colleagues_or_parent(self):
        binding = self._new(self.person, phone=None)
        self._message(binding)
        self.assertEqual(self._run(binding), self.lead)
        self.assertEqual(binding.crm_intake_state, "reused")

    def test_ambiguity_hidden_and_global_candidates_never_create(self):
        for kind in ("multiple", "hidden", "global"):
            with self.subTest(kind=kind), self.env.cr.savepoint():
                partner = self.env["res.partner"].create({"name": "Candidate " + kind})
                lead = self._lead("Candidate", partner)
                if kind == "multiple":
                    self._lead("Second", partner)
                elif kind == "hidden":
                    lead.user_id = self.other
                else:
                    lead.write({"company_id": False, "team_id": False})
                self.env.flush_all()
                count = self.env["crm.lead"].search_count([])
                binding = self._new(partner, phone=None)
                self._message(binding)
                self._run(binding)
                self.assertEqual(binding.crm_intake_state, "review")
                self.assertFalse(binding.crm_intake_lead_snapshot)
                self.assertEqual(self.env["crm.lead"].search_count([]), count)
                projection = self.api.get_customer_records(binding.channel_id.id)[
                    "intake"
                ]
                self.assertEqual(set(projection), {"state", "label"})
                self.assertNotIn("Candidate", projection["label"])

    def test_archived_won_and_foreign_records_are_not_reused(self):
        partner = self.env["res.partner"].create({"name": "Closed business customer"})
        archived = self._lead("Archived", partner, active=False)
        won = self._lead("Won", partner, probability=100)
        foreign_company = self.env["res.company"].create({"name": "Foreign"})
        foreign = self._lead(
            "Foreign", partner, company_id=foreign_company.id, user_id=False
        )
        binding = self._new(partner)
        self._message(binding)
        lead = self._run(binding)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertNotIn(lead, archived | won | foreign)

    def test_lid_and_unproven_alias_do_not_establish_phone(self):
        binding = self._new(phone=None)
        for namespace, value in (
            ("whatsapp.lid", "123@lid"),
            ("phone", "+5511998765432"),
        ):
            self.env["contact.center.identity.alias"].create(
                {
                    "account_id": self.account.id,
                    "identity_id": binding.identity_id.id,
                    "namespace": namespace,
                    "value_raw": value,
                    "value_normalized": value,
                    "confidence": "observed",
                }
            )
        self._message(binding)
        self._run(binding)
        self.assertEqual(
            (binding.crm_intake_state, binding.crm_intake_reason),
            ("review", "identity_unavailable"),
        )

    def test_nonhuman_types_control_and_outbound_never_admit(self):
        self.assertFalse(HUMAN_CONTENT_TYPES & EXCLUDED_CONTENT_TYPES)
        for content in sorted(EXCLUDED_CONTENT_TYPES):
            with self.subTest(content=content):
                binding = self._new(phone=None)
                self._message(binding, content_type=content)
                self.assertFalse(binding.crm_intake_state)
        for values in (
            {"direction": "outbound", "origin": "agent"},
            {"origin": "external_device"},
            {"external_message_id": "control:test"},
        ):
            binding = self._new(phone=None)
            self._message(binding, **values)
            self.assertFalse(binding.crm_intake_state)

    def test_every_supported_human_type_admits_after_control_card(self):
        for content in sorted(HUMAN_CONTENT_TYPES):
            with self.subTest(content=content):
                binding = self._new(phone=None)
                self._message(binding, content_type="identity.security.changed")
                self.assertFalse(binding.crm_intake_state)
                self._message(
                    binding,
                    content_type=content,
                    external_message_id="provider-control:" + uuid.uuid4().hex,
                )
                self.assertEqual(binding.crm_intake_state, "pending")

    def test_existing_conversation_is_excluded_even_with_new_message(self):
        binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", self.channel.id)]
        )
        self._message(binding)
        self.assertFalse(binding.crm_intake_state)

    def test_delayed_provider_first_message_prevents_later_admission(self):
        binding = self._new()
        old = self.account.crm_intake_enabled_at - datetime.timedelta(seconds=1)
        self._message(binding, date=old)
        self._message(binding)
        self.assertFalse(binding.crm_intake_state)

    def test_policy_change_cancels_old_job_and_fresh_message_rearms_pending(self):
        binding = self._new()
        self._message(binding)
        old_revision = binding.crm_intake_revision
        self.account.crm_intake_enabled = False
        self.assertEqual(binding._crm_intake_projection()["state"], "policy_changed")
        self._run(binding)
        self.assertEqual(binding.crm_intake_state, "policy_changed")
        self.account.crm_intake_enabled = True
        self._message(binding)
        revision = binding.crm_intake_revision
        self.assertGreater(revision, old_revision)
        binding._job_crm_intake(old_revision)
        self.assertEqual(binding.crm_intake_revision, revision)
        self.assertEqual(binding.crm_intake_state, "pending")
        self.assertTrue(self._run(binding))

    def test_conversation_born_while_disabled_is_excluded(self):
        self.account.crm_intake_enabled = False
        binding = self._new()
        self._message(binding)
        self.account.crm_intake_enabled = True
        self._message(binding)
        self.assertFalse(binding.crm_intake_state)

    def test_hook_failure_preserves_message_and_explicit_recovery_admits(self):
        binding = self._new()
        with patch.object(
            type(binding),
            "_crm_intake_admit",
            side_effect=ValidationError("Synthetic failure"),
        ):
            source = self._message(binding)
        self.assertTrue(source.exists())
        self.assertFalse(binding.crm_intake_state)
        with trap_jobs():
            binding.action_recover_crm_intake()
        self.assertEqual(binding.crm_intake_state, "pending")
        self.assertTrue(self._run(binding))

    def test_receipt_survives_history_expiry_and_no_relink_after_human_unlink(self):
        binding = self._new()
        source = self._message(binding)
        lead = self._run(binding)
        lead._conversation_links()._tombstone("manual")
        source.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).unlink()
        self.assertTrue(binding.exists())
        self._message(binding)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertFalse(lead._conversation_links())

    def test_missing_pending_source_enters_review(self):
        binding = self._new()
        source = self._message(binding)
        source.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).unlink()
        self._run(binding)
        self.assertEqual(binding.crm_intake_reason, "source_unavailable")

    def test_fields_and_recovery_are_not_rpc_escalations(self):
        binding = self._new()
        with self.assertRaises(AccessError):
            binding.with_user(self.agent).write({"crm_intake_state": "created"})
        with self.assertRaises(AccessError):
            binding.with_user(self.agent).action_recover_crm_intake()
        with self.assertRaises(AccessError):
            self.account.with_user(self.agent).write({"crm_intake_enabled": True})
        with self.assertRaises(AccessError):
            self.lead.write({"contact_center_intake_created": True})
        with self.assertRaises(AccessError):
            self.env["crm.lead"].with_context(
                default_contact_center_intake_created=True
            ).create({"name": "Forged"})
        with self.assertRaises(AccessError):
            binding.with_context(crm_intake_service=True).write(
                {"crm_intake_state": "created"}
            )
        with self.assertRaises(AccessError):
            self.api.with_user(self.other).get_customer_records(binding.channel_id.id)

    def test_unguarded_engine_blocks_activation_and_worker(self):
        if "base.automation" not in self.env.registry:
            self.skipTest("No native automation engine installed in this graph")
        binding = self._new()
        self._message(binding)
        with patch.object(
            type(self.env["base.automation"]),
            "_contact_center_intake_guard",
            0,
            create=True,
        ):
            self.assertFalse(automation_guards_available(self.env))
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self._second_account()
            self._run(binding)
        self.assertEqual(
            (binding.crm_intake_state, binding.crm_intake_reason),
            ("review", "automation_guard_missing"),
        )

    def test_salesperson_follows_quiet_creation_but_no_executor_or_team_email(self):
        self.agent.groups_id |= self.env.ref("sales_team.group_sale_salesman_all_leads")
        self.account.access_user_ids |= self.other
        team = self.env["crm.team"].create(
            {
                "name": "Intake team",
                "company_id": self.env.company.id,
                "user_id": self.other.id,
            }
        )
        self.other.notification_type = "email"
        self.other.email = "other@fixture.example"
        team.message_subscribe(partner_ids=self.other.partner_id.ids)
        self.account.crm_intake_team_id = team
        binding = self._new()
        binding.channel_id.with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_responsible_id": self.other.id})
        self._message(binding)
        mail_before = self.env["mail.mail"].search_count([])
        out_before = self.env["contact.center.outbox.command"].search_count([])
        lead = self._run(binding)
        self.assertEqual(lead.user_id, self.other)
        self.assertIn(self.other.partner_id, lead.message_partner_ids)
        self.assertNotIn(self.agent.partner_id, lead.message_partner_ids)
        self.assertEqual(self.env["mail.mail"].search_count([]), mail_before)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), out_before
        )
        self.assertTrue(lead.message_ids)

    def test_iap_enrichment_never_selects_intake_lead_or_mutates_partner(self):
        if "iap_enrich_done" not in self.env["crm.lead"]._fields:
            self.skipTest("IAP enrichment is not installed in this graph")
        partner = self.env["res.partner"].create(
            {
                "name": "Corporate",
                "email": "human@corporate.fixture",
                "phone": False,
                "mobile": False,
            }
        )
        partner.flush_recordset()
        before = partner.read(["phone", "mobile", "email", "write_date"])
        binding = self._new(partner)
        self._message(binding)
        lead = self._run(binding)
        self.assertTrue(lead.iap_enrich_done)
        requests = []

        def request(_self, emails):
            requests.append(emails)
            return {}

        with patch.object(
            type(self.env["iap.enrich.api"]), "_request_enrich", request
        ), patch.object(type(self.env.registry), "in_test_mode", return_value=True):
            self.env["crm.lead"]._iap_enrich_leads_cron()
        self.assertTrue(all(lead.id not in request for request in requests))
        self.assertEqual(
            before, partner.read(["phone", "mobile", "email", "write_date"])
        )

    def test_intake_writer_survives_confirmation_and_merge(self):
        binding = self._new()
        self._message(binding)
        lead = self._run(binding)
        row = (
            lead._conversation_links()
            .with_user(self.agent)
            ._confirm_scope(fields.Datetime.now())
        )
        self.assertEqual(row.writer, "intake")
        other = self._lead("Merge survivor", self.person)
        survivor = (lead | other)._merge_opportunity()
        self.assertEqual(survivor._conversation_links().writer, "intake")
        self.assertTrue(survivor.contact_center_intake_created)

    def _connection(self):
        return self.env["contact.center.provider.connection"].create(
            {
                "name": "Synthetic inbound",
                "account_id": self.account.id,
                "adapter_key": "test.fake",
                "external_ref": uuid.uuid4().hex,
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )

    def test_received_before_activation_is_excluded_even_if_provider_date_is_new(self):
        connection = self._connection()
        before = self.account.crm_intake_enabled_at - datetime.timedelta(seconds=1)
        with patch.object(type(self.env.cr), "now", return_value=before):
            event = (
                self.env["contact.center.inbox.event"]
                .with_context(contact_center_skip_enqueue=True)
                .create(
                    {
                        "provider_connection_id": connection.id,
                        "inbox_dedupe_key": uuid.uuid4().hex,
                        "provider_schema_version": "fixture-v1",
                        "raw_envelope_json": {"synthetic": True},
                    }
                )
            )
        binding = self._new()
        self._message(
            binding,
            source_inbox_event_id=event.id,
            provider_connection_id=connection.id,
        )
        self.assertFalse(binding.crm_intake_state)

    def test_native_ingress_replay_and_hook_failure_keep_customer_message(self):
        from odoo.addons.contact_center_base.services.dto import EventDTO

        connection = self._connection()
        token = uuid.uuid4().hex
        address = "5511987650000@s.whatsapp.net"
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": token,
                "event_type": "message.created",
                "occurred_at": self._after_cutoff().isoformat() + "Z",
                "account_ref": self.account.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": token,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Synthetic ingress",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": address,
                            "value_normalized": address,
                            "role": "primary",
                            "confidence": "protocol",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": address,
                            "value_normalized": address,
                            "role": "primary",
                            "confidence": "protocol",
                        }
                    ],
                },
                "message": {
                    "external_message_id": token,
                    "content_type": "text",
                    "text": "Synthetic ingress body",
                },
            }
        )
        application = self.env["contact.center.application"]
        model = self.env["contact.center.channel.binding"]
        with trap_jobs(), patch.object(
            type(self.env.cr), "now", return_value=self._after_cutoff()
        ), patch.object(
            type(model),
            "_crm_intake_admit",
            side_effect=ValidationError("Synthetic optional CRM failure"),
        ):
            message = application._process_event(connection, event)
        source = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)]
        )
        self.assertTrue(message.exists())
        self.assertTrue(source)
        self.assertFalse(source.channel_binding_id.crm_intake_state)
        with trap_jobs():
            source.channel_binding_id.action_recover_crm_intake()
            replay = application._process_event(connection, event)
        self.assertEqual(replay, message)
        self.assertEqual(
            self.env["contact.center.message.binding"].search_count(
                [("external_message_id", "=", token)]
            ),
            1,
        )
        lead = self._run(source.channel_binding_id)
        self.assertTrue(lead)
        self.assertEqual(source.channel_binding_id.crm_intake_state, "created")

    def test_same_second_pre_activation_webhook_is_excluded(self):
        self.account.crm_intake_enabled = False
        second = fields.Datetime.now()
        with patch.object(fields.Datetime, "now", return_value=second):
            self.account.crm_intake_enabled = True
        self.assertEqual(
            self.account.crm_intake_enabled_at, second + datetime.timedelta(seconds=1)
        )
        connection = self._connection()
        with patch.object(
            type(self.env.cr),
            "now",
            return_value=second + datetime.timedelta(microseconds=300000),
        ):
            event = (
                self.env["contact.center.inbox.event"]
                .with_context(contact_center_skip_enqueue=True)
                .create(
                    {
                        "provider_connection_id": connection.id,
                        "inbox_dedupe_key": uuid.uuid4().hex,
                        "provider_schema_version": "fixture-v1",
                        "raw_envelope_json": {"synthetic": True},
                    }
                )
            )
        binding = self._new()
        self._message(
            binding,
            source_inbox_event_id=event.id,
            provider_connection_id=connection.id,
        )
        self.assertFalse(binding.crm_intake_state)

    def test_supervisor_archive_disables_intake_and_unarchive_keeps_it_off(self):
        supervisor = self._user(
            "Supervisor",
            self.env.ref("contact_center_base.group_contact_center_supervisor"),
        )
        self.assertFalse(
            supervisor.has_group("contact_center_base.group_contact_center_admin")
        )
        self.account.write({"access_user_ids": [(4, supervisor.id)]})
        binding = self._new()
        self._message(binding)
        revision = self.account.crm_intake_revision
        self.account.with_user(supervisor).write({"active": False})
        self.assertFalse(self.account.active)
        self.assertFalse(self.account.crm_intake_enabled)
        self.assertGreater(self.account.crm_intake_revision, revision)
        self._run(binding)
        self.assertEqual(binding.crm_intake_state, "policy_changed")
        self.account.with_user(supervisor).write({"active": True})
        self.assertFalse(self.account.crm_intake_enabled)
        with self.assertRaises(AccessError):
            self.account.with_user(supervisor).write({"crm_intake_enabled": True})

    def test_manual_unlink_before_first_customer_reply_requires_review(self):
        binding = self._new(partner=self.person)
        self._message(binding, direction="outbound", origin="agent")
        link = self.env["contact.center.crm.conversation.link"]._link(
            binding.channel_id, self.lead, writer="manual"
        )
        old = fields.Datetime.now() - datetime.timedelta(seconds=2)
        with patch.object(fields.Datetime, "now", return_value=old):
            link._tombstone("manual")
        self._message(binding)
        self.assertEqual(binding.crm_intake_state, "pending")
        self.assertLess(link.unlinked_at, binding.crm_intake_admitted_at)
        self._run(binding)
        self.assertEqual(binding.crm_intake_state, "review")
        self.assertEqual(binding.crm_intake_reason, "manual_unlink")
        self.assertFalse(self.lead._conversation_links())

    def test_old_id_watermark_excludes_even_when_dates_are_new(self):
        binding = self._new()
        self.account.crm_intake_enabled = False
        # Pin policy time instead of writing create_date (ignored by native ORM).
        policy_time = binding.create_date - datetime.timedelta(seconds=2)
        with patch.object(fields.Datetime, "now", return_value=policy_time):
            self.account.crm_intake_enabled = True
        self.assertGreaterEqual(binding.create_date, self.account.crm_intake_enabled_at)
        self.assertLessEqual(binding.id, self.account.crm_intake_binding_watermark)
        self._message(binding)
        self.assertFalse(binding.crm_intake_state)

    def test_group_binding_is_excluded_and_operational_failure_propagates(self):
        binding = self._new(phone=None)
        binding.write({"conversation_type": "group", "identity_id": False})
        self._message(binding)
        self.assertFalse(binding.crm_intake_state)
        binding = self._new()
        with patch.object(
            type(binding),
            "_crm_intake_admit",
            side_effect=OperationalError("Synthetic transient ingress failure"),
        ):
            with self.assertRaises(OperationalError):
                self._message(binding)
