import inspect

from odoo.tests.common import TransactionCase

from ..models.onboarding import ContactCenterAccountSetupWizard


class TestApplicationServiceContract(TransactionCase):
    def test_base_onboarding_has_no_reverse_dependency_on_optional_ui(self):
        source = inspect.getsource(
            ContactCenterAccountSetupWizard._contact_center_inbox_action
        )
        self.assertNotIn("contact_center_ui", source)

    def test_registry_composes_application_and_ui_services(self):
        application = self.env["contact.center.application"]
        ui_api = self.env["contact.center.ui.api"]

        application_methods = (
            "_validate_event_scope",
            "_process_event",
            "_resolve_identity",
            "_post_inbound",
            "_outbound_connection",
            "_validated_outbound_uploads",
            "_queue_direct_mark_read",
            "_send_message",
            "_send_message_mutation",
            "_notify_ui",
            "_notify_connection_health",
        )
        ui_rpc_methods = (
            "bootstrap",
            "check_connection_health",
            "list_conversations",
            "get_conversation",
            "get_attribution",
            "get_timeline",
            "mark_fetched",
            "mark_seen",
            "update_conversation",
            "claim_conversation",
            "search_partners",
            "link_partner",
            "create_and_link_partner",
            "unlink_partner",
            "search_central_companies",
            "link_central_company",
            "create_and_link_central_company",
            "search_partner_companies",
            "link_partner_company",
            "create_and_link_partner_company",
            "rename_guest",
            "send_message",
            "react_message",
            "edit_message",
            "delete_message",
        )

        self.assertEqual(application._name, "contact.center.application")
        self.assertEqual(ui_api._name, "contact.center.ui.api")
        for method_name in application_methods:
            self.assertTrue(
                callable(getattr(application, method_name, None)),
                "Application service method is missing: %s" % method_name,
            )
        for method_name in ui_rpc_methods:
            self.assertTrue(
                callable(getattr(ui_api, method_name, None)),
                "UI API method is missing: %s" % method_name,
            )
