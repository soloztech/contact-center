from odoo.tests.common import SavepointCase


class TestContactCenterTechnicalMenus(SavepointCase):
    _NATIVE_PROXY_ACTIONS = (
        (
            "contact_center_base.menu_contact_center_native_messages",
            "mail.action_view_mail_message",
        ),
        (
            "contact_center_base.menu_contact_center_native_scheduled_messages",
            "mail.mail_message_schedule_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_guests",
            "mail.mail_guest_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_reactions",
            "mail.mail_message_reaction_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_ratings",
            "rating.rating_rating_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_link_previews",
            "mail.mail_link_preview_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_notifications",
            "mail.mail_notification_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_followers",
            "mail.action_view_followers",
        ),
        (
            "contact_center_base.menu_contact_center_native_subtypes",
            "mail.action_view_message_subtype",
        ),
        (
            "contact_center_base.menu_contact_center_native_tracking_values",
            "mail.action_view_mail_tracking_value",
        ),
        (
            "contact_center_base.menu_contact_center_native_activity_types",
            "mail.mail_activity_type_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_activities",
            "mail.mail_activity_action",
        ),
        (
            "contact_center_base.menu_contact_center_native_quick_replies",
            "mail.mail_shortcode_action",
        ),
    )
    _CONTACT_CENTER_RECORD_MENUS = (
        "contact_center_base.menu_contact_center_inbox",
        "contact_center_base.menu_contact_center_outbox",
        "contact_center_base.menu_contact_center_media_downloads",
        "contact_center_base.menu_contact_center_attribution_touchpoints",
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.system_group = cls.env.ref("base.group_system")
        cls.contact_center_root = cls.env.ref(
            "contact_center_base.menu_contact_center_root"
        )
        cls.technical_root = cls.env.ref(
            "contact_center_base.menu_contact_center_technical"
        )
        cls.odoo_messaging = cls.env.ref(
            "contact_center_base.menu_contact_center_odoo_messaging"
        )
        cls.technical_records = cls.env.ref(
            "contact_center_base.menu_contact_center_technical_records"
        )

    def _assert_system_only_menu(self, menu):
        self.assertEqual(
            menu.groups_id,
            self.system_group,
            "%s must remain restricted to Settings administrators" % menu.display_name,
        )

    def test_technical_root_has_two_explicit_sections(self):
        self.assertEqual(self.technical_root.parent_id, self.contact_center_root)
        self.assertEqual(self.odoo_messaging.parent_id, self.technical_root)
        self.assertEqual(self.technical_records.parent_id, self.technical_root)
        for menu in (
            self.technical_root,
            self.odoo_messaging,
            self.technical_records,
        ):
            self._assert_system_only_menu(menu)

    def test_odoo_messaging_shortcuts_reuse_native_actions(self):
        for menu_xmlid, action_xmlid in self._NATIVE_PROXY_ACTIONS:
            with self.subTest(menu_xmlid=menu_xmlid, action_xmlid=action_xmlid):
                menu = self.env.ref(menu_xmlid)
                self.assertEqual(menu.parent_id, self.odoo_messaging)
                self.assertEqual(menu.action, self.env.ref(action_xmlid))
                self._assert_system_only_menu(menu)

    def test_contact_center_records_are_grouped_under_technical_records(self):
        for menu_xmlid in self._CONTACT_CENTER_RECORD_MENUS:
            with self.subTest(menu_xmlid=menu_xmlid):
                menu = self.env.ref(menu_xmlid)
                self.assertEqual(menu.parent_id, self.technical_records)
                self._assert_system_only_menu(menu)
