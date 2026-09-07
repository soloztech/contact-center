import uuid

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
)


class TestContactCenterPipeline(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Pipeline Agent",
                    "login": "cc-pipeline-agent-%s" % uuid.uuid4(),
                    "email": "cc-pipeline-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Pipeline Team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Pipeline Inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "default_team_id": cls.team.id,
            }
        )
        cls.channel = cls._create_channel("Pipeline Customer")

    @classmethod
    def _create_channel(cls, guest_name, account=None):
        account = account or cls.account
        guest = cls.env["mail.guest"].sudo().create({"name": guest_name})
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": guest_name,
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        return channel

    def test_default_provisioning_is_idempotent_and_channel_gets_one_case(self):
        pipeline_model = self.env["contact.center.pipeline"]
        first = pipeline_model._contact_center_provision_defaults(
            companies=self.env.company,
            backfill_channels=True,
        )
        stage_ids_after_first = set(first.stage_ids.ids)
        second = pipeline_model._contact_center_provision_defaults(
            companies=self.env.company,
            backfill_channels=True,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.code, "default-service")
        self.assertTrue(
            {"new", "in-progress", "waiting", "done"}.issubset(
                set(first.stage_ids.mapped("code"))
            )
        )
        self.assertEqual(stage_ids_after_first, set(second.stage_ids.ids))
        self.assertEqual(len(first.stage_ids.filtered("is_initial")), 1)
        default_cases = self.channel.contact_center_case_ids.filtered("is_default")
        self.assertEqual(len(default_cases), 1)
        self.assertEqual(default_cases.pipeline_id, self.account.default_pipeline_id)

    def test_explicit_empty_company_scope_is_a_noop(self):
        result = self.env["contact.center.pipeline"]._contact_center_provision_defaults(
            companies=self.env["res.company"].browse(),
            assign_accounts=False,
            assign_teams=False,
            backfill_channels=False,
        )

        self.assertFalse(result)

    def test_two_cases_move_independently_from_operational_state(self):
        pipeline = self.account.default_pipeline_id
        new_stage = pipeline.stage_ids.filtered(lambda stage: stage.code == "new")
        progress_stage = pipeline.stage_ids.filtered(
            lambda stage: stage.code == "in-progress"
        )
        waiting_stage = pipeline.stage_ids.filtered(
            lambda stage: stage.code == "waiting"
        )
        primary_case = self.channel.contact_center_case_ids.filtered("is_default")
        secondary_case = (
            self.env["contact.center.case"]
            .with_user(self.agent)
            .create(
                {
                    "name": "Second proposal",
                    "channel_id": self.channel.id,
                    "pipeline_id": pipeline.id,
                    "stage_id": waiting_stage.id,
                    "responsible_user_id": self.agent.id,
                }
            )
        )
        self.assertEqual(primary_case.stage_id, new_stage)
        self.assertEqual(secondary_case.stage_id, waiting_stage)
        self.assertEqual(secondary_case.team_id, self.team)
        with self.assertRaises(AccessError):
            self.env["contact.center.case"].with_user(self.agent).create(
                {
                    "name": "Invalid teamless projection",
                    "channel_id": self.channel.id,
                    "team_id": False,
                    "pipeline_id": pipeline.id,
                    "stage_id": new_stage.id,
                }
            )

        primary_case.with_user(self.agent).write({"stage_id": progress_stage.id})
        self.assertEqual(primary_case.stage_id, progress_stage)
        self.assertEqual(primary_case.stage_revision, 1)
        self.assertEqual(secondary_case.stage_id, waiting_stage)
        self.assertEqual(self.channel.contact_center_state, "open")

        self.channel.with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_state": "resolved"})
        secondary_case.with_user(self.agent).write({"stage_id": new_stage.id})
        self.assertEqual(self.channel.contact_center_state, "resolved")
        self.assertEqual(primary_case.stage_id, progress_stage)

    def test_owner_only_conversation_creates_teamless_case(self):
        owner_account = self.env["contact.center.account"].create(
            {
                "name": "Owner-only Pipeline Inbox",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "owner_user_id": self.agent.id,
                "default_team_id": False,
            }
        )
        channel = self._create_channel("Owner-only Customer", account=owner_account)
        case = channel.contact_center_case_ids.filtered("is_default")
        self.assertFalse(channel.contact_center_team_id)
        self.assertEqual(channel.contact_center_owner_user_id, self.agent)
        self.assertFalse(case.team_id)
        self.assertEqual(case.responsible_user_id, self.agent)
        self.assertEqual(case.pipeline_id, owner_account.default_pipeline_id)

        replacement = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Replacement Pipeline Owner",
                    "login": "cc-pipeline-owner-%s" % uuid.uuid4(),
                    "email": "cc-pipeline-owner@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).ids,
                        )
                    ],
                }
            )
        )
        owner_account.write({"owner_user_id": replacement.id})
        self.assertEqual(channel.contact_center_owner_user_id, replacement)
        self.assertFalse(case.responsible_user_id)
        self.assertFalse(case.team_id)

    def test_team_change_requires_case_pipelines_to_be_enabled_first(self):
        custom_pipeline = self.env["contact.center.pipeline"].create(
            {
                "name": "Specialized Service",
                "code": "specialized-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        custom_stage = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Analysis",
                "code": "analysis",
                "pipeline_id": custom_pipeline.id,
                "is_initial": True,
            }
        )
        self.team.write({"pipeline_ids": [(4, custom_pipeline.id)]})
        self.env["contact.center.case"].with_user(self.agent).create(
            {
                "name": "Specialized case",
                "channel_id": self.channel.id,
                "pipeline_id": custom_pipeline.id,
                "stage_id": custom_stage.id,
            }
        )
        replacement_team = self.env["contact.center.team"].create(
            {
                "name": "Replacement Team %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, self.agent.ids)],
            }
        )
        self.assertNotIn(custom_pipeline, replacement_team.pipeline_ids)

        with self.assertRaisesRegex(ValidationError, "Enable pipelines"):
            with self.env.cr.savepoint():
                self.account.write({"default_team_id": replacement_team.id})

        self.account.invalidate_recordset(["default_team_id"])
        self.channel.invalidate_recordset(["contact_center_team_id"])
        replacement_team.invalidate_recordset(["pipeline_ids"])
        self.assertEqual(self.account.default_team_id, self.team)
        self.assertEqual(self.channel.contact_center_team_id, self.team)
        self.assertNotIn(custom_pipeline, replacement_team.pipeline_ids)

    def test_transition_is_idempotent_revision_guarded_and_history_immutable(self):
        case = self.channel.contact_center_case_ids.filtered("is_default")
        target = case.pipeline_id.stage_ids.filtered(
            lambda stage: stage.code == "in-progress"
        )
        request_uuid = str(uuid.uuid4())
        first = case.with_user(self.agent).action_transition(
            target.id,
            expected_revision=0,
            request_uuid=request_uuid,
        )
        repeated = case.with_user(self.agent).action_transition(
            target.id,
            expected_revision=0,
            request_uuid=request_uuid,
        )
        self.assertFalse(first["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["transition_id"], repeated["transition_id"])
        with self.assertRaises(ValidationError):
            case.with_user(self.agent).action_transition(
                case.pipeline_id._contact_center_initial_stage().id,
                expected_revision=0,
                request_uuid=str(uuid.uuid4()),
            )
        with self.assertRaises(AccessError):
            case.with_user(self.agent).action_transition(
                target.id,
                request_uuid=str(uuid.uuid4()),
                source="integration",
            )

        transition = self.env["contact.center.case.transition"].browse(
            first["transition_id"]
        )
        with self.assertRaises(AccessError):
            transition.write({"is_noop": True})
        with self.assertRaises(AccessError):
            transition.unlink()
        with self.assertRaises(AccessError):
            self.env["contact.center.case.transition"].create(
                {
                    "case_id": case.id,
                    "to_stage_id": target.id,
                    "from_revision": 1,
                    "to_revision": 1,
                    "request_uuid": str(uuid.uuid4()),
                    "source": "manual",
                    "changed_by_id": self.agent.id,
                    "occurred_at": case.stage_changed_at,
                }
            )

    def test_server_owned_case_metadata_cannot_be_forged_over_rpc_context(self):
        pipeline = self.account.default_pipeline_id
        stage = pipeline._contact_center_initial_stage()
        values = {
            "name": "Forged case metadata",
            "channel_id": self.channel.id,
            "pipeline_id": pipeline.id,
            "stage_id": stage.id,
            "company_id": self.env.company.id,
            "is_default": False,
        }

        with self.assertRaisesRegex(AccessError, "company_id, is_default"):
            self.env["contact.center.case"].with_user(self.agent).with_context(
                contact_center_case_service_token="forged-rpc-token",
                contact_center_case_account_id=self.account.id,
            ).create(values)

        case = self.channel.contact_center_case_ids.filtered("is_default")
        original_opened_at = case.opened_at
        for context in ({}, {"contact_center_case_service_token": "forged-rpc-token"}):
            with self.subTest(context=context), self.assertRaises(AccessError):
                case.with_user(self.agent).with_context(**context).write(
                    {"opened_at": "2000-01-01 00:00:00"}
                )
        case.invalidate_recordset(["opened_at"])
        self.assertEqual(case.opened_at, original_opened_at)

        created = (
            self.env["contact.center.case"]
            .with_user(self.agent)
            .with_context(
                default_active=False,
                default_is_default=True,
            )
            .create(
                {
                    "name": "Untrusted context defaults",
                    "channel_id": self.channel.id,
                    "pipeline_id": pipeline.id,
                    "stage_id": stage.id,
                }
            )
        )
        self.assertTrue(created.active)
        self.assertFalse(created.is_default)

    def test_initial_stage_is_replaced_only_by_atomic_service_action(self):
        pipeline = self.account.default_pipeline_id
        previous = pipeline._contact_center_initial_stage()
        replacement = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Qualified",
                "code": "qualified-%s" % uuid.uuid4().hex[:8],
                "pipeline_id": pipeline.id,
            }
        )

        with self.assertRaisesRegex(AccessError, "Set as Initial"):
            replacement.write({"is_initial": True})
        replacement.action_set_initial()

        previous.invalidate_recordset(["is_initial"])
        replacement.invalidate_recordset(["is_initial"])
        self.assertFalse(previous.is_initial)
        self.assertTrue(replacement.is_initial)
        self.assertEqual(pipeline._contact_center_initial_stage(), replacement)

    def test_followups_only_target_conversations_and_do_not_block_secondary_archive(
        self,
    ):
        default_case = self.channel.contact_center_case_ids.filtered("is_default")
        pipeline = default_case.pipeline_id
        secondary = (
            self.env["contact.center.case"]
            .with_user(self.agent)
            .create(
                {
                    "name": "Archivable secondary case",
                    "channel_id": self.channel.id,
                    "pipeline_id": pipeline.id,
                    "stage_id": pipeline._contact_center_initial_stage().id,
                }
            )
        )
        activity_model = self.env["mail.activity"].with_user(self.agent)
        case_model_id = self.env["ir.model"]._get_id("contact.center.case")
        case_values = {
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "res_model_id": case_model_id,
            "res_id": secondary.id,
            "summary": "Use the conversation",
            "user_id": self.agent.id,
        }
        with self.assertRaisesRegex(ValidationError, "Contact Center conversation"):
            activity_model.create(case_values)
        with self.assertRaisesRegex(ValidationError, "Contact Center conversation"):
            activity_model.with_context(default_res_model_id=case_model_id).create(
                {
                    key: value
                    for key, value in case_values.items()
                    if key != "res_model_id"
                }
            )
        with self.assertRaisesRegex(ValidationError, "Contact Center conversation"):
            activity_model.with_context(default_res_model_id=case_model_id).create(
                dict(
                    {
                        key: value
                        for key, value in case_values.items()
                        if key != "res_model_id"
                    },
                    res_model="mail.channel",
                )
            )
        activity = activity_model.with_context(
            default_res_model_id=case_model_id, default_res_model="contact.center.case"
        ).create(
            dict(
                case_values,
                res_model_id=self.env["ir.model"]._get_id("mail.channel"),
                res_id=self.channel.id,
            )
        )
        with self.assertRaisesRegex(ValidationError, "Contact Center conversation"):
            activity.write({"res_model_id": case_model_id, "res_id": secondary.id})
        with self.assertRaises(AccessError):
            secondary.with_user(self.agent).write({"active": False})
        with self.assertRaisesRegex(ValidationError, "canonical conversation case"):
            default_case.with_user(self.agent).action_archive()
        secondary.with_user(self.agent).action_archive()
        self.assertFalse(secondary.active)
        self.assertTrue(activity.exists())
        self.assertEqual(activity.res_model, "mail.channel")
        self.assertEqual(activity.res_id, self.channel.id)
        with self.assertRaisesRegex(ValidationError, "Contact Center conversation"):
            activity_model.create(case_values)
        secondary.with_user(self.agent).action_unarchive()
        self.assertTrue(secondary.active)

    def test_default_pipeline_never_archives_and_custom_pipeline_uses_action(self):
        default_pipeline = self.account.default_pipeline_id
        custom = self.env["contact.center.pipeline"].create(
            {
                "name": "Disposable custom pipeline",
                "code": "disposable-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )

        with self.assertRaises(AccessError):
            custom.write({"active": False})
        with self.assertRaisesRegex(ValidationError, "default service pipeline"):
            default_pipeline.action_archive()
        custom.action_archive()
        self.assertFalse(custom.active)
        custom.action_unarchive()
        self.assertTrue(custom.active)

    def test_noop_transition_is_idempotent_without_advancing_revision(self):
        case = self.channel.contact_center_case_ids.filtered("is_default")
        request_uuid = str(uuid.uuid4())
        result = case.with_user(self.agent).action_transition(
            case.stage_id.id,
            expected_revision=case.stage_revision,
            request_uuid=request_uuid,
        )
        repeated = case.with_user(self.agent).action_transition(
            case.stage_id.id,
            expected_revision=case.stage_revision,
            request_uuid=request_uuid,
        )

        self.assertFalse(result["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(result["transition_id"], repeated["transition_id"])
        self.assertEqual(case.stage_revision, 0)
        transition = self.env["contact.center.case.transition"].browse(
            result["transition_id"]
        )
        self.assertTrue(transition.is_noop)

    def test_authorized_channel_deletion_cascades_case_aggregate(self):
        channel = self._create_channel("Disposable Pipeline Customer")
        case = channel.contact_center_case_ids.filtered("is_default")
        transitions = case.transition_ids

        self.assertTrue(case)
        self.assertTrue(transitions)
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).unlink()

        self.assertFalse(case.exists())
        self.assertFalse(transitions.exists())

    def test_account_pipeline_precedes_team_default(self):
        alternate = self.env["contact.center.pipeline"].create(
            {
                "name": "Commercial",
                "code": "commercial-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        initial = self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Qualified",
                "code": "qualified",
                "pipeline_id": alternate.id,
                "is_initial": True,
            }
        )
        original_team_default = self.team.default_pipeline_id
        self.team.write({"pipeline_ids": [(4, alternate.id)]})
        self.account.write({"default_pipeline_id": alternate.id})

        channel = self._create_channel("Alternate Pipeline Customer")
        case = channel.contact_center_case_ids.filtered("is_default")
        self.assertEqual(case.pipeline_id, alternate)
        self.assertEqual(case.stage_id, initial)
        self.assertEqual(self.team.default_pipeline_id, original_team_default)

    def test_team_catalog_keeps_pipeline_used_by_inbox_without_cases(self):
        original = self.team.default_pipeline_id
        alternate = self.env["contact.center.pipeline"].create(
            {
                "name": "Future default",
                "code": "future-default-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        self.env["contact.center.pipeline.stage"].create(
            {
                "name": "Future initial",
                "code": "future-initial",
                "pipeline_id": alternate.id,
                "is_initial": True,
            }
        )
        self.team.write({"pipeline_ids": [(4, alternate.id)]})
        inbox_without_cases = self.env["contact.center.account"].create(
            {
                "name": "Inbox without cases",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "default_team_id": self.team.id,
                "default_pipeline_id": original.id,
            }
        )
        self.assertFalse(inbox_without_cases.connection_ids)

        with self.assertRaisesRegex(ValidationError, "used as the default by inbox"):
            with self.env.cr.savepoint():
                self.team.write(
                    {
                        "default_pipeline_id": alternate.id,
                        "pipeline_ids": [(6, 0, alternate.ids)],
                    }
                )

        self.team.invalidate_recordset(["default_pipeline_id", "pipeline_ids"])
        self.assertEqual(self.team.default_pipeline_id, original)
        self.assertIn(original, self.team.pipeline_ids)

    def test_default_case_never_expands_team_pipeline_catalog(self):
        alternate = self.env["contact.center.pipeline"].create(
            {
                "name": "External Catalog",
                "code": "external-%s" % uuid.uuid4().hex[:8],
                "company_id": self.env.company.id,
            }
        )
        self.env["contact.center.pipeline.stage"].create(
            {
                "name": "External Initial",
                "code": "external-initial",
                "pipeline_id": alternate.id,
                "is_initial": True,
            }
        )
        owner_account = self.env["contact.center.account"].create(
            {
                "name": "External Pipeline Inbox",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "owner_user_id": self.agent.id,
                "default_pipeline_id": alternate.id,
            }
        )
        channel = (
            self.env["mail.channel"]
            .sudo()
            .with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
            .create(
                {
                    "name": "Catalog Guard",
                    "channel_type": "contact_center",
                    "contact_center_company_id": self.env.company.id,
                    "contact_center_team_id": self.team.id,
                    "contact_center_state": "open",
                }
            )
        )
        original_pipeline_ids = set(self.team.pipeline_ids.ids)

        with self.assertRaisesRegex(ValidationError, "Enable pipeline"):
            channel._contact_center_ensure_default_case(
                team=self.team,
                account=owner_account,
            )

        self.team.invalidate_recordset(["pipeline_ids"])
        self.assertEqual(set(self.team.pipeline_ids.ids), original_pipeline_ids)
        self.assertNotIn(alternate, self.team.pipeline_ids)
        self.assertFalse(channel.contact_center_case_ids)

    def test_database_allows_only_one_initial_stage_per_pipeline(self):
        pipeline = self.account.default_pipeline_id
        with mute_logger("odoo.sql_db"):
            with self.assertRaises(IntegrityError):
                with self.env.cr.savepoint():
                    self.env["contact.center.pipeline.stage"].create(
                        {
                            "name": "Another Initial",
                            "code": "another-initial",
                            "pipeline_id": pipeline.id,
                            "is_initial": True,
                        }
                    )
