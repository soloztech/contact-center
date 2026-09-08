import threading
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
)


@tagged("-at_install", "post_install")
class TestCrmStageSyncConcurrency(TransactionCase):
    """Prove that CRM stage projection has one cross-model lock order."""

    WORKER_TIMEOUT_SECONDS = 15

    def _setup_committed_fixture(self, *, link_second):
        token = uuid.uuid4().hex
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            groups = env.ref(
                "contact_center_base.group_contact_center_agent"
            ) | env.ref("sales_team.group_sale_salesman")
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "CRM stage concurrency %s" % token,
                        "login": "cc-crm-stage-concurrency-%s" % token,
                        "email": "cc-crm-stage-concurrency-%s@example.invalid" % token,
                        "company_id": company.id,
                        "company_ids": [(6, 0, company.ids)],
                        "groups_id": [(6, 0, groups.ids)],
                    }
                )
            )
            crm_team = env["crm.team"].create(
                {
                    "name": "CRM stage concurrency %s" % token,
                    "company_id": company.id,
                    "user_id": agent.id,
                }
            )
            crm_stage_a = env["crm.stage"].create(
                {
                    "name": "CRM concurrency A %s" % token,
                    "sequence": 900,
                    "team_id": crm_team.id,
                }
            )
            crm_stage_b = env["crm.stage"].create(
                {
                    "name": "CRM concurrency B %s" % token,
                    "sequence": 901,
                    "team_id": crm_team.id,
                }
            )
            pipeline = env["contact.center.pipeline"].create(
                {
                    "name": "CRM concurrency %s" % token,
                    "code": "crm-concurrency-%s" % token,
                    "company_id": company.id,
                }
            )
            env["contact.center.pipeline.stage"].create(
                {
                    "name": "Temporary initial",
                    "code": "temporary-initial",
                    "pipeline_id": pipeline.id,
                    "is_initial": True,
                }
            )
            pipeline_binding = env["contact.center.crm.pipeline.binding"].create(
                {"pipeline_id": pipeline.id, "crm_team_id": crm_team.id}
            )
            stage_binding_a = pipeline_binding.stage_binding_ids.filtered(
                lambda item: item.crm_stage_id == crm_stage_a
            )
            stage_binding_b = pipeline_binding.stage_binding_ids.filtered(
                lambda item: item.crm_stage_id == crm_stage_b
            )
            team = env["contact.center.team"].create(
                {
                    "name": "CRM concurrency %s" % token,
                    "company_id": company.id,
                    "agent_ids": [(6, 0, agent.ids)],
                    "pipeline_ids": [(6, 0, pipeline.ids)],
                    "default_pipeline_id": pipeline.id,
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "CRM concurrency %s" % token,
                    "company_id": company.id,
                    "platform": "whatsapp",
                    "external_ref": "crm-concurrency-%s" % token,
                    "access_team_ids": [(6, 0, team.ids)],
                    "default_pipeline_id": pipeline.id,
                }
            )
            guest = (
                env["mail.guest"]
                .sudo()
                .create({"name": "CRM concurrency guest %s" % token})
            )
            identity = (
                env["contact.center.identity"]
                .sudo()
                .create(
                    {
                        "name": guest.name,
                        "company_id": company.id,
                        "mail_guest_id": guest.id,
                    }
                )
            )
            channel = env["mail.channel"]._contact_center_create_channel(
                account=account,
                identity=identity,
                conversation_type="direct",
                name="CRM concurrency %s" % token,
                teams=team,
                guest_ids=guest.ids,
            )
            channel_binding = (
                env["contact.center.channel.binding"]
                .sudo()
                .create(
                    {
                        "channel_id": channel.id,
                        "account_id": account.id,
                        "identity_id": identity.id,
                        "conversation_type": "direct",
                        "conversation_ref": "crm-concurrency-%s" % token,
                    }
                )
            )
            case_a = channel.contact_center_case_ids.filtered("is_default")
            case_a.with_user(agent).action_transition(stage_binding_a.stage_id.id)
            case_b = (
                env["contact.center.case"]
                .with_user(agent)
                .create(
                    {
                        "name": "Second CRM concurrency case",
                        "channel_id": channel.id,
                        "pipeline_id": pipeline.id,
                        "stage_id": stage_binding_a.stage_id.id,
                    }
                )
            )
            case_a.with_user(agent).action_create_crm_lead()
            lead = (
                env["contact.center.crm.case.link"]
                .sudo()
                .search([("case_id", "=", case_a.id)])
                .lead_id
            )
            if link_second:
                case_b.with_user(agent).action_link_crm_lead(lead.id)
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "agent_id": agent.id,
                "case_a_id": case_a.id,
                "case_b_id": case_b.id,
                "channel_binding_id": channel_binding.id,
                "channel_id": channel.id,
                "crm_stage_a_id": crm_stage_a.id,
                "crm_stage_b_id": crm_stage_b.id,
                "crm_team_id": crm_team.id,
                "guest_id": guest.id,
                "identity_id": identity.id,
                "lead_id": lead.id,
                "pipeline_binding_id": pipeline_binding.id,
                "pipeline_id": pipeline.id,
                "stage_b_id": stage_binding_b.stage_id.id,
                "team_id": team.id,
            }

    def _execute_operation(self, env, fixture, operation, record_id):
        if operation.startswith("transition"):
            env["contact.center.case"].with_user(fixture["agent_id"]).browse(
                record_id
            ).action_transition(fixture["stage_b_id"])
        elif operation == "link":
            env["contact.center.case"].with_user(fixture["agent_id"]).browse(
                record_id
            ).action_link_crm_lead(fixture["lead_id"])
        elif operation == "lead_stage":
            env["crm.lead"].with_user(fixture["agent_id"]).browse(
                fixture["lead_id"]
            ).write({"stage_id": fixture["crm_stage_b_id"]})
        elif operation == "merge":
            leads = (
                env["crm.lead"]
                .browse([fixture["lead_id"], fixture["merge_lead_id"]])
                .exists()
            )
            if len(leads) > 1:
                leads._merge_opportunity()
        else:
            raise AssertionError("Unsupported concurrency operation")

    def _run_transaction(self, fixture, operation, record_id):
        for attempt in range(3):
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '7s'")
                cr.execute("SET LOCAL statement_timeout = '12s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    self._execute_operation(env, fixture, operation, record_id)
                    cr.commit()  # pylint: disable=invalid-commit
                    return {"outcome": "done", "retries": attempt}
                except SerializationFailure:
                    cr.rollback()
                except ValidationError as error:
                    cr.rollback()
                    return {
                        "outcome": "validation",
                        "retries": attempt,
                        "message": str(error),
                    }
        raise AssertionError("CRM stage synchronization did not converge")

    def _worker(self, fixture, operation, record_id, barrier, results):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '7s'")
                cr.execute("SET LOCAL statement_timeout = '12s'")
                # Pin both workers to a real REPEATABLE READ snapshot before
                # either enters the canonical lead -> cases lock graph.
                cr.execute(
                    "SELECT id FROM contact_center_case WHERE id = %s", [record_id]
                )
                barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    self._execute_operation(env, fixture, operation, record_id)
                    cr.commit()  # pylint: disable=invalid-commit
                    result = {"outcome": "done", "retries": 0}
                except SerializationFailure:
                    cr.rollback()
                    result = self._run_transaction(fixture, operation, record_id)
                    result["retries"] += 1
                except ValidationError as error:
                    cr.rollback()
                    result = {
                        "outcome": "validation",
                        "retries": 0,
                        "message": str(error),
                    }
            results[operation] = result
        except Exception as error:  # pragma: no cover - asserted in the parent thread
            results[operation] = {
                "outcome": "error",
                "class": error.__class__.__name__,
                "message": str(error),
            }

    def _run_workers(self, fixture, operations, *, require_all_done=True):
        barrier = threading.Barrier(len(operations))
        results = {}
        workers = [
            threading.Thread(
                target=self._worker,
                args=(fixture, operation, record_id, barrier, results),
            )
            for operation, record_id in operations
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(self.WORKER_TIMEOUT_SECONDS)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(set(results), {item[0] for item in operations})
        if require_all_done:
            self.assertEqual(
                {result["outcome"] for result in results.values()},
                {"done"},
                results,
            )
        return results

    def _add_compatible_merge_lead(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            lead = env["crm.lead"].create(
                {
                    "name": "CRM topology merge concurrency %s" % uuid.uuid4().hex,
                    "company_id": env.company.id,
                    "user_id": False,
                    "team_id": fixture["crm_team_id"],
                    "stage_id": fixture["crm_stage_a_id"],
                }
            )
            source_lead = env["crm.lead"].browse(fixture["lead_id"])
            self.assertFalse(source_lead.user_id)
            self.assertFalse(lead.user_id)
            self.assertEqual(source_lead.team_id, lead.team_id)
            self.assertEqual(source_lead.stage_id, lead.stage_id)
            cr.commit()  # pylint: disable=invalid-commit
            fixture["merge_lead_id"] = lead.id
        return fixture

    def _assert_graph_at_stage_b(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            lead = env["crm.lead"].browse(fixture["lead_id"])
            cases = env["contact.center.case"].browse(
                [fixture["case_a_id"], fixture["case_b_id"]]
            )
            self.assertEqual(lead.stage_id.id, fixture["crm_stage_b_id"])
            self.assertEqual(set(cases.mapped("stage_id").ids), {fixture["stage_b_id"]})

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            channel = (
                env["mail.channel"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["channel_id"])
                .exists()
            )
            cases = (
                env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search([("channel_id", "=", fixture["channel_id"])])
            )
            env["contact.center.crm.case.link"].sudo().search(
                [("case_id", "in", cases.ids)]
            ).with_context(module_uninstall=True).unlink()
            env["mail.activity"].sudo().search(
                [("res_model", "=", "contact.center.case"), ("res_id", "in", cases.ids)]
            ).unlink()
            env["mail.message"].sudo().search(
                [
                    "|",
                    "&",
                    ("model", "=", "mail.channel"),
                    ("res_id", "=", fixture["channel_id"]),
                    "&",
                    ("model", "=", "contact.center.case"),
                    ("res_id", "in", cases.ids),
                ]
            ).with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN).unlink()
            env["contact.center.channel.binding"].sudo().with_context(
                active_test=False
            ).browse(fixture["channel_binding_id"]).unlink()
            channel.with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN,
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
            ).unlink()
            env["contact.center.identity"].sudo().browse(
                fixture["identity_id"]
            ).unlink()
            env["mail.guest"].sudo().browse(fixture["guest_id"]).with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).unlink()
            env["contact.center.account"].sudo().with_context(active_test=False).browse(
                fixture["account_id"]
            ).unlink()
            env["contact.center.team"].sudo().with_context(active_test=False).browse(
                fixture["team_id"]
            ).unlink()
            pipeline_binding = (
                env["contact.center.crm.pipeline.binding"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["pipeline_binding_id"])
            )
            pipeline_binding.with_context(module_uninstall=True).unlink()
            pipeline = (
                env["contact.center.pipeline"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["pipeline_id"])
            )
            pipeline.with_context(active_test=False).stage_ids.unlink()
            pipeline.unlink()
            env["crm.lead"].sudo().with_context(active_test=False).browse(
                [
                    lead_id
                    for lead_id in (
                        fixture["lead_id"],
                        fixture.get("merge_lead_id"),
                    )
                    if lead_id
                ]
            ).exists().unlink()
            env["crm.stage"].sudo().with_context(active_test=False).browse(
                [fixture["crm_stage_a_id"], fixture["crm_stage_b_id"]]
            ).unlink()
            env["crm.team"].sudo().with_context(active_test=False).browse(
                fixture["crm_team_id"]
            ).unlink()
            user = (
                env["res.users"]
                .sudo()
                .with_context(active_test=False)
                .browse(fixture["agent_id"])
            )
            partner = user.partner_id
            user.unlink()
            partner.exists().unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def test_two_case_transitions_converge_without_case_lead_lock_cycle(self):
        fixture = self._setup_committed_fixture(link_second=True)
        try:
            self._run_workers(
                fixture,
                [
                    ("transition-a", fixture["case_a_id"]),
                    ("transition-b", fixture["case_b_id"]),
                ],
            )
            self._assert_graph_at_stage_b(fixture)
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_link_and_lead_stage_change_converge_from_one_locked_snapshot(self):
        fixture = self._setup_committed_fixture(link_second=False)
        try:
            self._run_workers(
                fixture,
                [
                    ("link", fixture["case_b_id"]),
                    ("lead_stage", fixture["case_a_id"]),
                ],
            )
            self._assert_graph_at_stage_b(fixture)
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_native_merge_and_case_link_converge_without_dangling_bridge(self):
        fixture = self._add_compatible_merge_lead(
            self._setup_committed_fixture(link_second=False)
        )
        try:
            results = self._run_workers(
                fixture,
                [
                    ("merge", fixture["case_a_id"]),
                    ("link", fixture["case_b_id"]),
                ],
                require_all_done=False,
            )
            self.assertEqual(results["merge"]["outcome"], "done", results)
            self.assertIn(results["link"]["outcome"], ("done", "validation"), results)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                surviving_leads = (
                    env["crm.lead"]
                    .with_context(active_test=False)
                    .browse([fixture["lead_id"], fixture["merge_lead_id"]])
                    .exists()
                )
                self.assertEqual(len(surviving_leads), 1)
                links = (
                    env["contact.center.crm.case.link"]
                    .sudo()
                    .search(
                        [
                            (
                                "case_id",
                                "in",
                                [fixture["case_a_id"], fixture["case_b_id"]],
                            ),
                            ("state", "=", "active"),
                        ]
                    )
                )
                self.assertTrue(links)
                self.assertEqual(links.mapped("lead_id"), surviving_leads)
                self.assertEqual(
                    links.filtered(lambda link: not link.lead_id),
                    env["contact.center.crm.case.link"],
                )
                if results["link"]["outcome"] == "done":
                    self.assertIn(fixture["case_b_id"], links.mapped("case_id").ids)
        finally:
            self._cleanup_committed_fixture(fixture)
