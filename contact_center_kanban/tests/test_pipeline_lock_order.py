import uuid
from unittest import mock

from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
)


class TestContactCenterPipelineLockOrder(SavepointCase):
    def _new_unconfigured_channel(self):
        return (
            self.env["mail.channel"]
            .sudo()
            .with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
            .create(
                {
                    "name": "Pipeline lock-order regression",
                    "channel_type": "contact_center",
                    "contact_center_company_id": self.env.company.id,
                    "contact_center_state": "open",
                }
            )
        )

    def test_fallback_pipeline_is_provisioned_before_channel_lock(self):
        channel = self._new_unconfigured_channel()
        lock_order = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized_query = " ".join(str(query).lower().split())
            if (
                "from res_company" in normalized_query
                and "for update" in normalized_query
            ):
                lock_order.append("company")
            elif (
                "from mail_channel" in normalized_query
                and "for update" in normalized_query
            ):
                lock_order.append("channel")
            return original_execute(cursor, query, params, *args, **kwargs)

        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            case = channel._contact_center_ensure_default_case()

        self.assertTrue(case)
        self.assertEqual(lock_order[:2], ["company", "channel"])

    def test_configured_pipeline_does_not_enter_company_lock_path(self):
        pipeline_model = self.env["contact.center.pipeline"]
        pipeline = pipeline_model._contact_center_provision_default_pipeline(
            self.env.company
        )
        account = self.env["contact.center.account"].create(
            {
                "name": "Configured pipeline lock-order inbox",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "default_pipeline_id": pipeline.id,
            }
        )
        channel = self._new_unconfigured_channel()

        with mock.patch.object(
            type(pipeline_model),
            "_contact_center_provision_default_pipeline",
            side_effect=AssertionError("configured path must not lock the company"),
        ):
            case = channel._contact_center_ensure_default_case(account=account)

        self.assertEqual(case.pipeline_id, pipeline)
