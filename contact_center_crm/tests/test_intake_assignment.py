"""Native intake metadata, editable initial ownership and lifecycle queue proofs."""

import uuid
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import Form

from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.intake import PROVENANCE_FIELDS
from ..models.intake_policy import INTAKE_TOKEN
from .test_intake import CrmIntakeCase


@tagged("post_install", "-at_install")
class TestCrmIntakeAssignment(CrmIntakeCase):
    def setUp(self):
        super().setUp()
        self.agent.groups_id |= self.env.ref("sales_team.group_sale_salesman_all_leads")
        self.agent.groups_id |= self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        self.account.access_user_ids |= self.other | self.non_sales

    def _waiting(self, **values):
        values.setdefault(
            "phone", "55119%08d" % (int(uuid.uuid4().hex[:8], 16) % 10**8)
        )
        binding = self._new(**values)
        self._message(binding)
        lead = self._run(binding)
        self.assertEqual(binding.crm_intake_state, "created")
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        return binding, lead

    def _claim(self, binding, user=None):
        return (
            self.env["contact.center.ui.api"]
            .with_user(user or self.other)
            .claim_conversation(binding.channel_id.id)
        )

    def _transfer(self, binding, user=None):
        return self.api.update_conversation(
            binding.channel_id.id, {"responsible_id": user.id if user else False}
        )

    def test_claim_initializes_once_and_transfers_are_independent(self):
        binding, lead = self._waiting()
        self.other.write(
            {"notification_type": "email", "email": "quiet@fixture.example"}
        )
        before_mail = self.env["mail.mail"].search_count([])
        before_outbox = self.env["contact.center.outbox.command"].search_count([])
        with trap_jobs() as trap:
            self._claim(binding)
        trap.assert_jobs_count(1, only=binding._job_crm_intake_assignment)
        sequence = binding.channel_id.contact_center_lifecycle_seq
        trap.assert_enqueued_job(
            binding._job_crm_intake_assignment,
            properties={
                "identity_key": "contact_center:crm_intake_assignment:%s:%s"
                % (binding.id, sequence)
            },
        )
        binding._job_crm_intake_assignment()
        lead.invalidate_recordset()
        self.assertEqual(lead.user_id, self.other)
        self.assertFalse(lead.contact_center_intake_assignment_pending)
        self.assertFalse(lead.create_uid)
        self.assertIn(self.other.partner_id, lead.message_partner_ids)
        self.assertNotIn(self.agent.partner_id, lead.message_partner_ids)
        with trap_jobs() as trap:
            self._transfer(binding, self.agent)
            self._transfer(binding)
        trap.assert_jobs_count(0, only=binding._job_crm_intake_assignment)
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)
        lead.write({"user_id": self.agent.id})
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.agent)
        self.assertEqual(self.env["mail.mail"].search_count([]), before_mail)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), before_outbox
        )

    def test_out_of_order_jobs_choose_first_valid_assignment_event(self):
        binding, lead = self._waiting()
        with trap_jobs() as trap:
            self._claim(binding, self.non_sales)
            invalid_seq = binding.channel_id.contact_center_lifecycle_seq
            self._transfer(binding, self.other)
            first_seq = binding.channel_id.contact_center_lifecycle_seq
            self._transfer(binding, self.agent)
            last_seq = binding.channel_id.contact_center_lifecycle_seq
        self.assertLess(invalid_seq, first_seq)
        self.assertLess(first_seq, last_seq)
        trap.assert_jobs_count(3, only=binding._job_crm_intake_assignment)
        for sequence in (invalid_seq, first_seq, last_seq):
            trap.assert_enqueued_job(
                binding._job_crm_intake_assignment,
                properties={
                    "identity_key": "contact_center:crm_intake_assignment:%s:%s"
                    % (binding.id, sequence)
                },
            )
        # A job for the latest event may run first; it resolves the same history.
        binding._job_crm_intake_assignment()
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_manual_seller_choice_cancels_pending_even_before_claim(self):
        binding, lead = self._waiting()
        lead.with_user(self.agent).write({"user_id": self.agent.id})
        self.assertFalse(lead.contact_center_intake_assignment_pending)
        lead.with_user(self.agent).write({"user_id": False})
        with trap_jobs():
            self._claim(binding)
        binding._job_crm_intake_assignment()
        self.assertFalse(lead.user_id)
        self.assertFalse(lead.contact_center_intake_assignment_pending)

    def test_empty_seller_noop_keeps_pending(self):
        binding, lead = self._waiting()
        lead.with_user(self.agent).write({"user_id": False})
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        with trap_jobs():
            self._claim(binding)
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_manual_personal_documents_executor_can_change_seller(self):
        binding, lead = self._waiting()
        # Keep a valid native team for both sellers. This isolates the protected
        # bookkeeping after visibility changes from native Properties recompute
        # when a personal-documents user also implicitly changes sales team.
        team = self.env["crm.team"].create(
            {
                "name": "Shared personal-documents team",
                "company_id": self.env.company.id,
                "member_ids": [(6, 0, (self.agent | self.other).ids)],
            }
        )
        lead.team_id = team
        self.env.flush_all()
        self.agent.groups_id -= self.env.ref("sales_team.group_sale_salesman_all_leads")
        lead.with_user(self.agent).with_context(
            mail_auto_subscribe_no_notify=True
        ).write(
            {
                "user_id": self.other.id,
                "team_id": lead.team_id.id,
                "company_id": lead.company_id.id,
                # Native Properties reads its old value during a deferred
                # recompute. A full editable form payload keeps that value
                # explicit when the original seller loses personal access.
                "lead_properties": lead.lead_properties,
            }
        )
        lead.with_user(self.agent).env.flush_all()
        self.assertEqual(lead.user_id, self.other)
        self.assertFalse(lead.contact_center_intake_assignment_pending)
        with self.assertRaises(AccessError):
            lead.with_user(self.agent).check_access_rule("write")
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertEqual(lead.user_id, self.other)

    def test_automatic_personal_documents_executor_has_no_post_write_read(self):
        binding, lead = self._waiting()
        self.agent.groups_id -= self.env.ref("sales_team.group_sale_salesman_all_leads")
        with trap_jobs():
            self._claim(binding)
        self.assertTrue(binding._job_crm_intake_assignment())
        self.assertEqual(lead.user_id, self.other)
        self.assertFalse(lead.contact_center_intake_assignment_pending)

    def test_existing_current_assignee_creates_blank_creator_and_closed_marker(self):
        binding = self._new()
        with trap_jobs():
            self._claim(binding)
        self._message(binding)
        lead = self._run(binding)
        lead.flush_recordset()
        lead.invalidate_recordset()
        self.assertEqual(lead.user_id, self.other)
        self.assertFalse(lead.create_uid)
        self.assertTrue(lead.create_date)
        self.assertFalse(lead.contact_center_intake_assignment_pending)
        self.assertEqual(lead._conversation_links().linked_by_id, self.agent)

    def test_native_form_partner_edit_keeps_company_and_assignment(self):
        binding, lead = self._waiting()
        partner = self.env["res.partner"].create({"name": "Global customer"})
        lead.type = "opportunity"
        form = Form(lead, view="crm.crm_lead_view_form")
        self.assertIn("company_id", form._view["fields"])
        form.partner_id = partner
        self.assertEqual(form.company_id, self.env.company)
        form.save()
        self.assertEqual(lead.company_id, self.env.company)
        with trap_jobs():
            self._claim(binding)
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_onchange_explicit_company_clear_remains_clear(self):
        _binding, lead = self._waiting()
        partner = self.env["res.partner"].create({"name": "Companyless customer"})
        # Native Form cannot edit the duplicate invisible company field. Exercise
        # its actual onchange record with a deliberately cleared current value;
        # _origin still has a company and must not restore this explicit choice.
        onchange = self.env["crm.lead"].new(
            {
                "company_id": False,
                "user_id": False,
                "team_id": False,
                "partner_id": partner.id,
            },
            origin=lead,
        )
        self.assertTrue(onchange._origin.company_id)
        onchange._compute_company_id()
        self.assertFalse(onchange.company_id)
        lead.write({"company_id": False, "partner_id": partner.id})
        self.assertFalse(lead.company_id)

    def test_raw_partner_edit_and_late_assignee_keep_current_team_company(self):
        binding, lead = self._waiting()
        partner = self.env["res.partner"].create({"name": "Global customer"})
        lead.write({"partner_id": partner.id})
        self.assertEqual(lead.company_id, self.env.company)
        team = self.env["crm.team"].create(
            {
                "name": "User selected team",
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
                "member_ids": [(6, 0, self.agent.ids)],
            }
        )
        lead.write({"team_id": team.id})
        with trap_jobs():
            self._claim(binding)
        binding._job_crm_intake_assignment()
        self.assertEqual(
            (lead.user_id, lead.team_id, lead.company_id),
            (self.other, team, self.env.company),
        )

    def test_native_single_and_mass_conversion_seller_intent(self):
        for mass in (False, True):
            for choose_seller in (False, True):
                with self.subTest(
                    mass=mass, choose_seller=choose_seller
                ), self.env.cr.savepoint():
                    binding, lead = self._waiting()
                    model = "crm.lead2opportunity.partner" + (".mass" if mass else "")
                    values = {"name": "convert", "action": "nothing", "team_id": False}
                    if mass:
                        values.update(
                            deduplicate=False,
                            user_ids=[(6, 0, self.agent.ids if choose_seller else [])],
                        )
                    else:
                        values.update(
                            lead_id=lead.id,
                            user_id=self.agent.id if choose_seller else False,
                        )
                    wizard = (
                        self.env[model]
                        .with_context(active_ids=lead.ids, active_id=lead.id)
                        .create(values)
                    )
                    if mass:
                        wizard.action_mass_convert()
                    else:
                        wizard.action_apply()
                    self.assertEqual(lead.type, "opportunity")
                    self.assertEqual(
                        lead.contact_center_intake_assignment_pending, not choose_seller
                    )
                    with trap_jobs():
                        self._claim(binding)
                    binding._job_crm_intake_assignment()
                    self.assertEqual(
                        lead.user_id, self.agent if choose_seller else self.other
                    )
                    # Keep the next iteration independent of dedup candidates.
                    lead.active = False

    def test_protected_assignment_fields_and_context_are_not_forgeable(self):
        for field in PROVENANCE_FIELDS:
            for method in ("create", "write", "default"):
                with self.subTest(field=field, method=method), self.assertRaises(
                    AccessError
                ):
                    if method == "create":
                        self.env["crm.lead"].with_context(
                            crm_intake_service=True
                        ).create(
                            [{"name": "Ordinary"}, {"name": "Forged", field: False}]
                        )
                    elif method == "write":
                        self.lead.with_context(crm_intake_service=True).write(
                            {field: False}
                        )
                    else:
                        self.env["crm.lead"].with_context(
                            **{"default_" + field: False}
                        ).create({"name": "Forged default"})

    def test_manual_batch_and_copy_keep_native_creator_without_pending(self):
        _binding, lead = self._waiting()
        ordinary = (
            self.env["crm.lead"]
            .with_user(self.agent)
            .create(
                [{"name": "Manual one"}, {"name": "Manual two", "create_uid": False}]
            )
        )
        copied = lead.with_user(self.agent).copy()
        records = ordinary | copied
        records.flush_recordset()
        records.invalidate_recordset()
        self.assertEqual(records.mapped("create_uid"), self.agent)
        self.assertFalse(any(records.mapped("contact_center_intake_created")))
        self.assertFalse(
            any(records.mapped("contact_center_intake_assignment_pending"))
        )

    def test_mixed_record_write_cancels_only_waiting_lead(self):
        _binding, lead = self._waiting()
        (lead | self.lead).with_user(self.agent).with_context(
            mail_auto_subscribe_no_notify=True
        ).write({"user_id": self.other.id})
        self.assertEqual((lead | self.lead).mapped("user_id"), self.other)
        self.assertFalse(lead.contact_center_intake_assignment_pending)
        self.assertFalse(self.lead.contact_center_intake_created)

    def test_merge_closes_pending_on_all_participants_without_deleting_them(self):
        first_binding, first = self._waiting()
        second_binding, second = self._waiting(phone="5511998765999")
        survivor = (self.lead | first | second)._merge_opportunity(auto_unlink=False)
        self.assertFalse(
            any(
                (self.lead | first | second).mapped(
                    "contact_center_intake_assignment_pending"
                )
            )
        )
        self.assertTrue(survivor.contact_center_intake_created)
        seller = survivor.user_id
        with trap_jobs():
            self._claim(first_binding, self.agent)
            self._claim(second_binding, self.agent)
        first_binding._job_crm_intake_assignment()
        second_binding._job_crm_intake_assignment()
        self.assertEqual(survivor.user_id, seller)

    def test_merge_two_waiting_leads_closes_pending_on_head(self):
        first_binding, first = self._waiting()
        second_binding, second = self._waiting()
        participants = first | second
        head = participants._sort_by_confidence_level(reverse=True)[0]
        self.assertTrue(head.contact_center_intake_assignment_pending)
        survivor = participants._merge_opportunity(auto_unlink=False)
        self.assertEqual(survivor, head)
        self.assertFalse(
            any(participants.mapped("contact_center_intake_assignment_pending"))
        )
        self.assertFalse(survivor.user_id)
        with trap_jobs():
            self._claim(first_binding, self.agent)
            self._claim(second_binding, self.agent)
        first_binding._job_crm_intake_assignment()
        second_binding._job_crm_intake_assignment()
        self.assertFalse(survivor.user_id)

    def test_waiting_merge_head_keeps_seller_from_ordinary_tail(self):
        binding, head = self._waiting()
        head.type = "opportunity"
        self.assertTrue(head.contact_center_intake_assignment_pending)
        tail = self.env["crm.lead"].create(
            {
                "name": "Ordinary merge tail with salesperson",
                "type": "lead",
                "company_id": self.env.company.id,
                "user_id": self.other.id,
            }
        )
        survivor = (head | tail)._merge_opportunity(auto_unlink=False)
        self.assertEqual(survivor, head)
        self.assertEqual(survivor.user_id, self.other)
        self.assertFalse(survivor.contact_center_intake_assignment_pending)
        with trap_jobs():
            self._claim(binding, self.agent)
        binding._job_crm_intake_assignment()
        self.assertEqual(survivor.user_id, self.other)

    def test_reused_business_is_never_an_assignment_candidate(self):
        binding = self._new(self.person, phone=None)
        self._message(binding)
        reused = self._run(binding)
        self.assertEqual(binding.crm_intake_state, "reused")
        before = reused.read(["user_id", "team_id", "write_date"])
        with trap_jobs() as trap:
            self._claim(binding)
        trap.assert_jobs_count(0, only=binding._job_crm_intake_assignment)
        binding._job_crm_intake_assignment()
        self.assertEqual(reused.read(["user_id", "team_id", "write_date"]), before)

    def test_unlinked_deleted_archived_or_companyless_lead_is_not_assigned(self):
        for reason in ("unlinked", "deleted", "archived", "companyless"):
            with self.subTest(reason=reason), self.env.cr.savepoint():
                binding, lead = self._waiting()
                with trap_jobs():
                    self._claim(binding)
                if reason == "unlinked":
                    self.api.unlink_crm_opportunity(binding.channel_id.id, lead.id)
                elif reason == "deleted":
                    lead.unlink()
                elif reason == "archived":
                    lead.active = False
                else:
                    lead.write({"company_id": False})
                self.assertFalse(binding._job_crm_intake_assignment())
                if lead.exists():
                    self.assertFalse(lead.user_id)
                    lead.active = False

    def test_invalid_current_assignee_does_not_create_fallback_seller(self):
        binding = self._new()
        with trap_jobs():
            self._claim(binding, self.non_sales)
        self._message(binding)
        lead = self._run(binding)
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        self.assertFalse(binding._job_crm_intake_assignment())

    def test_archived_foreign_and_nonmember_users_are_ineligible(self):
        self.account.access_user_ids -= self.other
        self.assertFalse(self.account._crm_intake_valid_user(self.other))
        self.other.active = False
        self.assertFalse(self.account._crm_intake_valid_user(self.other))
        self.other.active = True
        foreign = self.env["res.company"].create({"name": "Foreign salesperson"})
        self.other.write(
            {"company_id": foreign.id, "company_ids": [(6, 0, foreign.ids)]}
        )
        self.assertFalse(self.account._crm_intake_valid_user(self.other))
        # Native inbox validation also prevents granting the foreign user.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.account.access_user_ids |= self.other

    def test_restored_access_does_not_resurrect_consumed_assignment(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        sequence = binding.channel_id.contact_center_lifecycle_seq
        self.account.access_user_ids -= self.other
        self.assertFalse(binding._job_crm_intake_assignment())
        watermark = lead.contact_center_intake_assignment_after_seq
        self.assertEqual(watermark, sequence)
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        self.account.access_user_ids |= self.other
        with trap_jobs() as trap:
            binding.action_recover_crm_intake()
        trap.assert_enqueued_job(binding._job_crm_intake_assignment)
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertEqual(lead.contact_center_intake_assignment_after_seq, watermark)
        with trap_jobs():
            self._transfer(binding, self.other)
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_sales_grant_does_not_resurrect_consumed_assignment(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding, self.non_sales)
        sequence = binding.channel_id.contact_center_lifecycle_seq
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertEqual(lead.contact_center_intake_assignment_after_seq, sequence)
        self.non_sales.groups_id |= self.env.ref(
            "sales_team.group_sale_salesman_all_leads"
        )
        self.assertTrue(self.account._crm_intake_valid_user(self.non_sales))
        with trap_jobs() as trap:
            binding.action_recover_crm_intake()
        trap.assert_enqueued_job(binding._job_crm_intake_assignment)
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        with trap_jobs():
            self._transfer(binding, self.other)
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_foreign_portal_past_assignee_does_not_block_later_seller(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding, self.non_sales)
        self.account.access_user_ids -= self.non_sales
        foreign = self.env["res.company"].create({"name": "Past assignee company"})
        self.non_sales.write(
            {
                "groups_id": [(6, 0, self.env.ref("base.group_portal").ids)],
                "company_id": foreign.id,
                "company_ids": [(6, 0, foreign.ids)],
            }
        )
        self.assertTrue(self.non_sales.share)
        with self.assertRaises(AccessError):
            self.non_sales.with_user(self.agent).with_context(
                allowed_company_ids=self.env.company.ids
            ).check_access_rule("read")
        with trap_jobs():
            self._transfer(binding, self.other)
        self.assertTrue(binding._job_crm_intake_assignment())
        self.assertEqual(lead.user_id, self.other)
        self.assertFalse(lead.contact_center_intake_assignment_pending)

    def test_optional_enqueue_failure_keeps_claim_and_can_recover(self):
        binding, lead = self._waiting()
        with trap_jobs(), patch.object(
            type(binding),
            "_crm_intake_enqueue_assignments",
            side_effect=ValidationError("Synthetic enqueue failure"),
        ):
            self._claim(binding)
        self.assertEqual(binding.channel_id.contact_center_responsible_id, self.other)
        self.assertFalse(lead.user_id)
        with trap_jobs() as trap:
            binding.action_recover_crm_intake()
        trap.assert_jobs_count(1, only=binding._job_crm_intake_assignment)
        calls = [
            call
            for call in trap.calls
            if call.method.__name__ == "_job_crm_intake_assignment"
        ]
        self.assertFalse(calls[0].properties.get("identity_key"))
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)
        with trap_jobs() as trap:
            binding.action_recover_crm_intake()
        trap.assert_jobs_count(0, only=binding._job_crm_intake_assignment)

    def test_enqueue_operational_failure_propagates(self):
        binding, _lead = self._waiting()
        with self.assertRaises(
            SerializationFailure
        ), self.env.cr.savepoint(), trap_jobs(), patch.object(
            type(binding),
            "_crm_intake_enqueue_assignments",
            side_effect=SerializationFailure("Synthetic contention"),
        ):
            self._claim(binding)
        binding.channel_id.invalidate_recordset()
        self.assertFalse(binding.channel_id.contact_center_responsible_id)

    def test_non_intake_assignment_does_not_enqueue(self):
        with trap_jobs() as trap, patch.object(
            type(self.env["contact.center.channel.binding"]),
            "_crm_intake_enqueue_assignments",
        ) as enqueue:
            self.api.claim_conversation(self.channel.id)
        enqueue.assert_not_called()
        trap.assert_jobs_count(
            0,
            only=self.env["contact.center.channel.binding"]._job_crm_intake_assignment,
        )

    def test_terminal_usererror_keeps_pending_and_does_not_fail_job(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        with patch.object(
            type(lead),
            "write",
            side_effect=UserError("Synthetic native company mismatch"),
        ):
            self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)

    def test_native_company_mismatch_usererror_keeps_pending(self):
        partner = self.env["res.partner"].create({"name": "Customer moved company"})
        binding, lead = self._waiting()
        lead.partner_id = partner
        foreign = self.env["res.company"].create({"name": "Foreign partner company"})
        partner.company_id = foreign
        with trap_jobs():
            self._claim(binding)
        # A native _check_company error is UserError, not ValidationError.
        with self.assertRaises(UserError), self.env.cr.savepoint():
            lead.write({"user_id": self.other.id, "company_id": self.env.company.id})
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)

    def test_mixed_company_partner_write_preserves_each_intake_company(self):
        _binding, first = self._waiting()
        second_company = self.env["res.company"].create(
            {"name": "Second intake company"}
        )
        self.agent.company_ids |= second_company
        account = self.env["contact.center.account"].create(
            {
                "name": "Second company commercial",
                "platform": "whatsapp",
                "external_ref": uuid.uuid4().hex,
                "company_id": second_company.id,
                "access_user_ids": [(6, 0, self.agent.ids)],
                "crm_intake_user_id": self.agent.id,
                "crm_intake_enabled": True,
            }
        )
        _second_binding, second = self._waiting(account=account)
        global_partner = self.env["res.partner"].create({"name": "Global partner"})
        (first | second).write({"partner_id": global_partner.id})
        self.assertEqual(first.company_id, self.env.company)
        self.assertEqual(second.company_id, second_company)
        # Ordinary CRM retains its native company computation.
        ordinary = self.env["crm.lead"].create(
            {
                "name": "Ordinary global prospect",
                "user_id": False,
                "team_id": False,
                "company_id": self.env.company.id,
            }
        )
        ordinary.write({"partner_id": global_partner.id})
        self.assertFalse(ordinary.company_id)

    def test_non_intake_transition_skips_optional_savepoint(self):
        original = type(self.channel)._contact_center_record_transitions
        observed = []

        def observe_hook(channel, channels, plans):
            # Count only the lifecycle hook, not other native claim savepoints.
            # Raising here would be swallowed by optional enqueue admission.
            with patch.object(type(self.env.cr), "savepoint") as savepoint:
                events = original(channel, channels, plans)
            savepoint.assert_not_called()
            assigned = events.filtered(lambda event: event.event_type == "assigned")
            observed.extend(assigned.ids)
            self.assertFalse(
                channel.env[
                    "contact.center.channel.binding"
                ]._crm_intake_assignment_candidates(events)
            )
            return events

        with trap_jobs(), patch.object(
            type(self.channel), "_contact_center_record_transitions", observe_hook
        ), patch.object(
            type(self.env["contact.center.channel.binding"]),
            "_crm_intake_enqueue_assignments",
        ) as enqueue:
            self.api.claim_conversation(self.channel.id)
        self.assertTrue(observed)
        enqueue.assert_not_called()

    def test_protected_mixed_creation_blanks_only_the_automatic_row(self):
        leads = (
            self.env["crm.lead"]
            .with_user(self.agent)
            .with_context(
                crm_intake_service=INTAKE_TOKEN,
                mail_create_nosubscribe=True,
                mail_auto_subscribe_no_notify=True,
            )
            .create(
                [
                    {
                        "name": "Internal intake row",
                        "contact_center_intake_created": True,
                        "user_id": False,
                        "team_id": False,
                        "company_id": self.env.company.id,
                    },
                    {"name": "Ordinary row in the same batch"},
                ]
            )
        )
        leads.flush_recordset()
        leads.invalidate_recordset()
        self.assertFalse(leads[0].create_uid)
        self.assertEqual(leads[1].create_uid, self.agent)
        self.assertTrue(all(leads.mapped("create_date")))
        self.assertTrue(all(leads.mapped("write_uid")))

    def test_removed_executor_leaves_initial_assignment_pending(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        self.account.write({"crm_intake_enabled": False, "crm_intake_user_id": False})
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)

    def test_disabled_intake_preserves_pending_with_executor_configured(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        watermark = lead.contact_center_intake_assignment_after_seq
        self.account.crm_intake_enabled = False
        self.assertEqual(self.account.crm_intake_user_id, self.agent)
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        self.assertEqual(lead.contact_center_intake_assignment_after_seq, watermark)
        self.account.crm_intake_enabled = True
        with trap_jobs():
            binding.action_recover_crm_intake()
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_missing_automation_guard_preserves_pending_and_watermark(self):
        if "base.automation" not in self.env.registry:
            self.skipTest("No native automation engine installed in this graph")
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        watermark = lead.contact_center_intake_assignment_after_seq
        before_mail = self.env["mail.mail"].search_count([])
        before_outbox = self.env["contact.center.outbox.command"].search_count([])
        with patch.object(
            type(self.env["base.automation"]),
            "_contact_center_intake_guard",
            0,
            create=True,
        ):
            self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
        self.assertEqual(lead.contact_center_intake_assignment_after_seq, watermark)
        self.assertEqual(self.env["mail.mail"].search_count([]), before_mail)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), before_outbox
        )
        binding._job_crm_intake_assignment()
        self.assertEqual(lead.user_id, self.other)

    def test_archived_inbox_leaves_initial_assignment_pending(self):
        binding, lead = self._waiting()
        with trap_jobs():
            self._claim(binding)
        self.account.active = False
        self.assertFalse(binding._job_crm_intake_assignment())
        self.assertFalse(lead.user_id)
        self.assertTrue(lead.contact_center_intake_assignment_pending)
