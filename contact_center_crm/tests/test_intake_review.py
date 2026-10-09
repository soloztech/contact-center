from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from .test_intake import CrmIntakeCase


@tagged("post_install", "-at_install")
class TestCrmIntakeReview(CrmIntakeCase):
    def _review(self):
        leads = self.env["crm.lead"].create(
            [
                {
                    "name": "Synthetic candidate A",
                    "user_id": False,
                    "phone": "+5511998765432",
                    "company_id": self.env.company.id,
                },
                {
                    "name": "Synthetic candidate B",
                    "user_id": False,
                    "phone": "+5511998765432",
                    "company_id": self.env.company.id,
                },
            ]
        )
        binding = self._new()
        self._message(binding)
        self.assertFalse(self._run(binding))
        self.assertEqual(binding.crm_intake_state, "review")
        return binding, leads

    def _assert_terminal(self, binding):
        self._message(binding)
        with trap_jobs():
            binding.action_recover_crm_intake()
        self._run(binding)
        self.assertEqual(binding.crm_intake_state, "resolved")

    def test_dismissal_records_actor_and_survives_messages_recovery_and_replay(self):
        binding, _leads = self._review()
        self.env["contact.center.ui.api"].with_user(
            self.agent
        ).resolve_crm_intake_review(
            binding.channel_id.id, binding.crm_intake_revision, False, False
        )
        self.env.flush_all()
        self.assertEqual(binding.crm_intake_reason, "human_dismissed")
        self.assertFalse(binding.crm_intake_lead_snapshot)
        decision = (
            self.env["contact.center.crm.review.decision"]
            .sudo()
            .search([("binding_id", "=", binding.id)])
        )
        self.assertEqual(
            (decision.kind, decision.decision, decision.actor_ref),
            ("intake_resolution", "dismiss", self.agent.id),
        )
        self._assert_terminal(binding)

    def test_common_link_endpoint_resolves_without_confirming_period(self):
        binding, leads = self._review()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        with trap_jobs():
            api.link_crm_opportunity(binding.channel_id.id, leads[0].id)
        self.assertEqual(binding.crm_intake_reason, "human_resolved")
        self.assertEqual(binding.crm_intake_lead_snapshot, leads[0].id)
        self.assertEqual(leads[0]._conversation_links().scope_state, "context")
        with trap_jobs():
            api.unlink_crm_opportunity(binding.channel_id.id, leads[0].id)
        self._assert_terminal(binding)
        self.assertFalse(leads[0]._conversation_links())

    def test_standalone_candidates_stale_revision_and_foreign_lead(self):
        binding, leads = self._review()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        page = api.get_customer_records(binding.channel_id.id)
        self.assertEqual(
            {x["id"] for x in page["intake"]["review_candidates"]}, set(leads.ids)
        )
        with self.assertRaises(ValidationError):
            api.resolve_crm_intake_review(
                binding.channel_id.id,
                binding.crm_intake_revision + 1,
                leads[0].id,
                True,
            )
        foreign = self.env["crm.lead"].create(
            {
                "name": "Not this business",
                "company_id": self.env["res.company"]
                .create({"name": "Synthetic other review company"})
                .id,
            }
        )
        with self.assertRaises(ValidationError):
            api.resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, foreign.id, True
            )
        with trap_jobs():
            api.resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, leads[0].id, True
            )
        self.assertEqual(binding.crm_intake_lead_snapshot, leads[0].id)

    def test_review_requires_native_write_and_inbox_access(self):
        binding, leads = self._review()
        self.env["ir.rule"].create(
            {
                "name": "Synthetic review cannot write candidate",
                "model_id": self.env["ir.model"]._get_id("crm.lead"),
                "domain_force": "[('id', 'not in', %s)]" % leads.ids,
                "perm_read": False,
                "perm_write": True,
                "perm_create": False,
                "perm_unlink": False,
            }
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(
                self.agent
            ).resolve_crm_intake_review(
                binding.channel_id.id, binding.crm_intake_revision, leads[0].id, True
            )
