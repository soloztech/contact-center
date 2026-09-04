from odoo.tests.common import SavepointCase


class TestContactCenterMetaTechnicalMenus(SavepointCase):
    _SHARED_ACTION_PROXIES = (
        (
            "contact_center_meta.menu_contact_center_meta_webhook_endpoints",
            "meta_webhook_base.action_meta_webhook_endpoint",
        ),
        (
            "contact_center_meta.menu_contact_center_meta_webhook_pages",
            "meta_webhook_base.action_meta_webhook_page",
        ),
        (
            "contact_center_meta.menu_contact_center_meta_webhook_deliveries",
            "meta_webhook_base.action_meta_webhook_delivery",
        ),
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.system_group = cls.env.ref("base.group_system")
        cls.technical_records = cls.env.ref(
            "contact_center_base.menu_contact_center_technical_records"
        )
        cls.meta_webhooks = cls.env.ref(
            "contact_center_meta.menu_contact_center_meta_webhooks"
        )

    def _assert_system_only_menu(self, menu):
        self.assertEqual(
            menu.groups_id,
            self.system_group,
            "%s must remain restricted to Settings administrators" % menu.display_name,
        )

    def test_meta_webhook_section_is_scoped_to_contact_center_records(self):
        self.assertEqual(self.meta_webhooks.parent_id, self.technical_records)
        self._assert_system_only_menu(self.meta_webhooks)

    def test_meta_shortcuts_reuse_shared_webhook_actions(self):
        for menu_xmlid, action_xmlid in self._SHARED_ACTION_PROXIES:
            with self.subTest(menu_xmlid=menu_xmlid, action_xmlid=action_xmlid):
                menu = self.env.ref(menu_xmlid)
                self.assertEqual(menu.parent_id, self.meta_webhooks)
                self.assertEqual(menu.action, self.env.ref(action_xmlid))
                self._assert_system_only_menu(menu)
