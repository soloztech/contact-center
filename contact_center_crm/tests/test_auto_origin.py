import datetime
from unittest.mock import patch

from psycopg2.errors import LockNotAvailable

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from .test_intake import CrmIntakeCase


@tagged("post_install", "-at_install")
class TestCrmAutoOrigin(CrmIntakeCase):
    def setUp(self):
        super().setUp()
        self.account.write({"crm_origin_auto_enabled": True})

    def _after_cutoff(self, account=None):
        account = account or self.account
        return max(
            super()._after_cutoff(account),
            account.crm_origin_enabled_at or fields.Datetime.now(),
        )

    def test_unsupported_first_entry_is_the_business_anchor(self):
        binding = self._new()
        first = self._message(binding, content_type="unsupported")
        self.assertFalse(binding.crm_intake_state)
        later = self._message(
            binding, date=first.message_id.date + datetime.timedelta(seconds=5)
        )
        lead = self._run(binding)
        link = lead._conversation_links()
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertFalse(lead.create_uid)
        self.assertEqual(binding.crm_intake_source_id, later.id)
        self.assertEqual(link.scope_start, first.message_id.date)
        self.assertEqual(link.scope_decision_mode, "automatic_intake")
        self.assertTrue(link.automatic_lineage)
        self.assertFalse(link.scope_end)
        self.assertNotIn(
            "Confirme", binding.with_user(self.agent)._crm_intake_projection()["label"]
        )

    def test_period_refusal_keeps_created_business_and_context(self):
        binding = self._new()
        self._message(binding)
        before = self.env["crm.lead"].search_count([])
        with patch.object(
            type(self.env["contact.center.crm.conversation.link"]),
            "_confirm_scope",
            side_effect=ValidationError("synthetic overlap"),
        ):
            lead = self._run(binding)
        self.assertTrue(lead.exists())
        self.assertEqual(self.env["crm.lead"].search_count([]), before + 1)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertEqual(lead._conversation_links().scope_state, "context")
        self.assertEqual(
            lead._conversation_links().origin_review_reason, "scope_refused"
        )
        self._run(binding)
        self.assertEqual(self.env["crm.lead"].search_count([]), before + 1)

    def test_policy_rearm_never_adopts_the_queued_business(self):
        binding = self._new()
        self._message(binding)
        intake_revision = self.account.crm_intake_revision
        self.account.write({"crm_origin_auto_enabled": False})
        self.account.write({"crm_origin_auto_enabled": True})
        self.assertEqual(self.account.crm_intake_revision, intake_revision)
        lead = self._run(binding)
        self.assertEqual(lead._conversation_links().scope_state, "context")
        self.assertEqual(
            lead._conversation_links().origin_review_reason, "policy_rearmed"
        )

    def test_recovery_creates_context_without_automatic_period(self):
        binding = self._new()
        self._message(binding)
        with trap_jobs():
            binding.action_recover_crm_intake()
        lead = self._run(binding)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertEqual(lead._conversation_links().scope_state, "context")
        self.assertEqual(lead._conversation_links().origin_review_reason, "recovery")

    def test_probability_closure_keeps_first_boundary_after_reopen(self):
        binding = self._new()
        self._message(binding)
        lead = self._run(binding)
        with trap_jobs():
            lead.write({"probability": 100})
        link = lead._conversation_links()
        boundary = link.origin_first_closed_at
        self.assertTrue(boundary)
        with trap_jobs():
            lead.write({"probability": 10})
            lead.write({"active": False})
        self.assertEqual(link.origin_first_closed_at, boundary)

    def test_human_revision_has_distinct_provenance_and_noop_preserves_it(self):
        binding = self._new()
        self._message(binding)
        link = self._run(binding)._conversation_links().with_user(self.agent)
        with trap_jobs():
            self.assertEqual(link._confirm_scope(link.scope_start), link)
            changed = link._confirm_scope(
                link.scope_start - datetime.timedelta(seconds=1)
            )
        self.assertEqual(changed.scope_decision_mode, "human")
        self.assertTrue(changed.automatic_lineage)
        self.assertEqual(link.state, "unlinked")

    def test_origin_audit_and_first_message_cannot_be_forged(self):
        binding = self._new()
        with self.assertRaises(AccessError):
            self.account.write({"crm_origin_enabled_at": fields.Datetime.now()})
        with self.assertRaises(AccessError):
            binding.write({"crm_origin_first_source_id": 123})

    def test_private_comparison_normalizes_national_and_international_phone(self):
        gate = self.env["contact.center.crm.intake.gate"]._acquire_company(
            self.env.company
        )
        local = gate._comparison_keys("(11) 99876-5432", "BR")
        international = gate._comparison_keys("+5511998765432", "BR")
        legacy = gate._comparison_keys("+551198765432", "BR")
        self.assertEqual(local["exact"], international["exact"])
        self.assertNotEqual(local["exact"], legacy["exact"])
        self.assertEqual(local["variant"], legacy["variant"])
        self.assertEqual(len(local["exact"]), 64)
        self.assertFalse(gate._comparison_keys("1234567", "BR")["exact"])

    def test_optional_marker_propagates_host_flush_and_skips_disabled_policy(self):
        binding = self._new()
        with patch.object(
            type(self.env), "flush_all", side_effect=ValidationError("host flush")
        ):
            with self.assertRaisesRegex(ValidationError, "host flush"):
                self._message(binding)
        self.account.write(
            {"crm_origin_auto_enabled": False, "crm_intake_enabled": False}
        )
        binding = self._new(phone="5511988887777")
        with patch.object(
            type(binding),
            "_crm_origin_note_first",
            side_effect=AssertionError("unexpected marker"),
        ) as saved:
            self._message(binding)
        self.assertEqual(saved.call_count, 0)
        self.assertFalse(binding.crm_origin_first_source_id)

    def test_recovery_without_eligible_message_does_not_mark_later_normal_admission(
        self,
    ):
        binding = self._new()
        self._message(binding, content_type="unsupported")
        with trap_jobs():
            binding.action_recover_crm_intake()
        self.assertFalse(binding.crm_origin_recovered)
        self._message(binding)
        self.assertEqual(
            self._run(binding)._conversation_links().scope_decision_mode,
            "automatic_intake",
        )

    def test_enabling_origin_on_inactive_or_noncommercial_inbox_is_refused(self):
        self.account.write(
            {"crm_origin_auto_enabled": False, "crm_intake_enabled": False}
        )
        with self.assertRaises(ValidationError):
            self.account.write({"crm_origin_auto_enabled": True})

    def test_bulk_lost_archive_and_stage_closure_only_stamp_lineage(self):
        bindings = [
            self._new(phone=number) for number in ("5511990011001", "5511990011002")
        ]
        leads = self.env["crm.lead"].browse()
        for binding in bindings:
            self._message(binding)
            leads |= self._run(binding)
        plain = self.env["crm.lead"].create(
            {"name": "Unrelated plain business", "company_id": self.env.company.id}
        )
        links = leads._conversation_links()
        self.assertEqual(len(links), 2)
        with trap_jobs():
            (leads | plain).action_set_lost()
        first = {row.id: row.origin_first_closed_at for row in links}
        self.assertTrue(all(first.values()))
        with trap_jobs():
            (leads | plain).write({"active": True, "probability": 10})
            (leads | plain).write({"active": False})
        self.assertEqual({row.id: row.origin_first_closed_at for row in links}, first)
        self.assertFalse(plain._conversation_links())
        with trap_jobs():
            (leads | plain).write({"active": True, "probability": 10})
            stage = self.env["crm.stage"].create(
                {"name": "Mixed shared stage", "is_won": False}
            )
            (leads | plain).write({"stage_id": stage.id})
            stage.write({"is_won": True})
        self.assertEqual({row.id: row.origin_first_closed_at for row in links}, first)
        self.assertFalse(plain._conversation_links())

    def test_link_provenance_cannot_be_forged_by_rpc_defaults(self):
        binding = self._new()
        lead = self.env["crm.lead"].create(
            {"name": "Manual business", "company_id": self.env.company.id}
        )
        for key, value in [
            ("automatic_lineage", True),
            ("origin_first_closed_at", "2000-01-01 00:00:00"),
            ("origin_policy_revision", 44),
        ]:
            with self.assertRaises(AccessError):
                self.env["contact.center.ui.api"].with_user(self.agent).with_context(
                    **{"default_" + key: value}
                ).link_crm_opportunity(binding.channel_id.id, lead.id)

    def test_busy_optional_marker_preserves_received_message_and_context(self):
        binding = self._new()
        with patch.object(
            type(binding),
            "_crm_origin_note_first",
            side_effect=LockNotAvailable("busy"),
        ):
            message = self._message(binding)
        self.assertTrue(message.exists())
        self.assertFalse(binding.crm_origin_first_source_id)
        lead = self._run(binding)
        self.assertTrue(lead.exists())
        self.assertEqual(lead._conversation_links().scope_state, "context")
        self.assertEqual(
            lead._conversation_links().origin_review_reason, "anchor_unavailable"
        )
