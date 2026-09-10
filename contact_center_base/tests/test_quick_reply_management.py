import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import Form, SavepointCase


class TestContactCenterQuickReplyManagement(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("agent", "group_contact_center_agent")
        cls.other = cls._user("other", "group_contact_center_agent")
        cls.supervisor = cls._user("supervisor", "group_contact_center_supervisor")
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Quick reply team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, (cls.agent | cls.other).ids)],
                "supervisor_ids": [(6, 0, cls.supervisor.ids)],
            }
        )
        cls.other_team = cls.env["contact.center.team"].create(
            {
                "name": "Other reply team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.supervisor.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Quick reply inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "quick-replies-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Reply guest"})
        identity = cls.env["contact.center.identity"].create(
            {
                "name": guest.name,
                "company_id": cls.env.company.id,
                "mail_guest_id": guest.id,
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            conversation_type="direct",
            name="Quick reply conversation",
            teams=cls.team,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "quick-replies-%s" % uuid.uuid4(),
            }
        )

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Quick reply %s" % name,
                    "login": "quick-reply-%s-%s" % (name, uuid.uuid4()),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base.%s" % group).ids)
                    ],
                }
            )
        )

    def _reply(self, user=None, **values):
        return (
            self.env["contact.center.quick.reply.binding"]
            .with_user(user or self.agent)
            .create(
                {
                    "shortcut": "reply-%s" % uuid.uuid4(),
                    "body": "Personal reply",
                    **values,
                }
            )
        )

    def test_agent_can_create_edit_and_delete_own_personal_reply(self):
        reply = self._reply()
        native = reply.shortcode_id
        self.assertEqual(reply.scope, "personal")
        self.assertEqual(reply.owner_id, self.agent)
        reply.write({"body": "Changed personal reply", "description": "Own reply"})
        self.assertEqual(native.substitution, "Changed personal reply")
        reply.unlink()
        self.assertFalse(reply.exists())
        self.assertFalse(native.exists())

    def test_agent_can_create_reply_from_native_management_form(self):
        model = self.env["contact.center.quick.reply.binding"].with_user(self.agent)
        form = Form(
            model,
            view="contact_center_base.view_contact_center_quick_reply_binding_form",
        )
        form.shortcut = "form-reply"
        form.body = "Created in the quick reply form"
        reply = form.save()
        self.assertEqual(reply.owner_id, self.agent)
        self.assertEqual(reply.shortcode_id.source, "form-reply")
        self.assertEqual(reply.body, "Created in the quick reply form")

    def test_personal_owner_can_delete_reply_prepared_by_an_administrator(self):
        reply = self.env["contact.center.quick.reply.binding"].create(
            {
                "scope": "personal",
                "owner_id": self.agent.id,
                "shortcut": "prepared",
                "body": "Prepared for the agent",
            }
        )
        native = reply.shortcode_id
        reply.with_user(self.agent).unlink()
        self.assertFalse(reply.exists())
        self.assertFalse(native.exists())

    def test_personal_reply_is_only_offered_to_its_owner(self):
        reply = self._reply()
        api = self.env["contact.center.ui.api"]
        own = api.with_user(self.agent).search_quick_replies(
            self.channel.id, reply.shortcut
        )
        other = api.with_user(self.other).search_quick_replies(
            self.channel.id, reply.shortcut
        )
        self.assertEqual([item["binding_id"] for item in own["items"]], reply.ids)
        self.assertFalse(other["items"])

    def test_other_agent_cannot_read_or_mutate_personal_native_content(self):
        reply = self._reply()
        for record in (reply, reply.shortcode_id):
            other_record = record.with_user(self.other)
            with self.assertRaises(AccessError):
                other_record.read()
            with self.assertRaises(AccessError):
                other_record.unlink()
        with self.assertRaises(AccessError):
            reply.with_user(self.other).write({"body": "Overwritten"})
        with self.assertRaises(AccessError):
            reply.shortcode_id.with_user(self.other).write(
                {"substitution": "Overwritten"}
            )
        reply.write({"active": False})
        with self.assertRaises(AccessError):
            reply.shortcode_id.with_user(self.other).read(["substitution"])

    def test_agent_cannot_choose_another_owner_or_shared_scope(self):
        with self.assertRaises(AccessError):
            self._reply(owner_id=self.other.id)
        for scope, target in (("company", {}), ("team", {"team_id": self.team.id})):
            with self.assertRaises(AccessError):
                self._reply(scope=scope, **target)
        reply = self._reply()
        with self.assertRaises(AccessError):
            reply.write({"owner_id": self.other.id})
        with self.assertRaises(AccessError):
            reply.write({"scope": "company", "owner_id": False})

    def test_supervisor_can_manage_replies_for_a_supervised_team(self):
        reply = self._reply(self.supervisor, scope="team", team_id=self.team.id)
        self.assertFalse(reply.owner_id)
        reply.write({"body": "Team reply"})
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .search_quick_replies(self.channel.id, reply.shortcut)
        )
        self.assertEqual(result["items"][0]["body"], "Team reply")
        with self.assertRaises(AccessError):
            reply.with_user(self.agent).write({"body": "Not allowed"})
        with self.assertRaises(AccessError):
            reply.shortcode_id.with_user(self.agent).write(
                {"substitution": "Not allowed"}
            )
        reply.unlink()
        self.assertFalse(reply.exists())

    def test_supervisor_cannot_manage_an_unsupervised_team_or_other_personal_reply(
        self,
    ):
        with self.assertRaises(AccessError):
            self._reply(self.supervisor, scope="team", team_id=self.other_team.id)
        reply = self._reply()
        with self.assertRaises(AccessError):
            reply.with_user(self.supervisor).write({"body": "Not mine"})
        with self.assertRaises(AccessError):
            self._reply(self.supervisor, scope="company")

    def test_reply_management_rejects_an_unavailable_company(self):
        company = self.env["res.company"].create({"name": "Other reply company"})
        with self.assertRaises(AccessError):
            self._reply(company_id=company.id)
        with self.assertRaises(AccessError):
            self._reply(
                self.supervisor,
                scope="team",
                team_id=self.team.id,
                company_id=company.id,
            )
        reply = self._reply()
        with self.assertRaises(AccessError):
            reply.write({"company_id": company.id})

    def test_agent_cannot_link_or_retarget_native_content(self):
        native = self.env["mail.shortcode"].create(
            {"source": "existing", "substitution": "Existing"}
        )
        with self.assertRaises(AccessError):
            self._reply(shortcode_id=native.id)
        reply = self._reply()
        with self.assertRaises(AccessError):
            reply.write({"shortcode_id": native.id})

    def test_native_reverse_relation_cannot_bypass_binding_management(self):
        reply = self._reply()
        native = (
            self.env["mail.shortcode"]
            .with_user(self.other)
            .create({"source": "owned", "substitution": "Native reply"})
        )
        with self.assertRaises(AccessError):
            native.write({"contact_center_binding_ids": [(4, reply.id)]})
        with self.assertRaises(AccessError):
            native.create(
                {
                    "source": "bypass",
                    "substitution": "No",
                    "contact_center_binding_ids": [(4, reply.id)],
                }
            )

    def test_related_content_write_checks_all_shared_bindings(self):
        native = self.env["mail.shortcode"].create(
            {"source": "shared", "substitution": "Original"}
        )
        bindings = self.env["contact.center.quick.reply.binding"].create(
            [
                {"shortcode_id": native.id, "scope": "team", "team_id": team.id}
                for team in (self.team, self.other_team)
            ]
        )
        with self.assertRaises(AccessError):
            bindings[0].with_user(self.supervisor).write(
                {"body": "Must not change another team"}
            )
        self.assertEqual(native.substitution, "Original")

    def test_personal_native_content_cannot_be_published_by_another_binding(self):
        reply = self._reply()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env["contact.center.quick.reply.binding"].create(
                {"shortcode_id": reply.shortcode_id.id, "scope": "company"}
            )
        reply.write({"active": False})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env["contact.center.quick.reply.binding"].create(
                {"shortcode_id": reply.shortcode_id.id, "scope": "company"}
            )

    def test_unbound_native_replies_keep_non_contact_center_behavior(self):
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Non contact center user",
                    "login": "native-only-%s" % uuid.uuid4(),
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )
        for editor in (user, self.agent, self.supervisor):
            native = self.env["mail.shortcode"].create(
                {"source": "ordinary", "substitution": "Original"}
            )
            native.with_user(editor).write(
                {"substitution": "Native behavior unchanged"}
            )
            self.assertEqual(native.substitution, "Native behavior unchanged")
            native.with_user(editor).unlink()

    def test_discuss_bootstrap_respects_personal_content_for_every_recipient(self):
        personal = self._reply()
        archived = self._reply(active=False)
        shared = self._reply(self.supervisor, scope="team", team_id=self.team.id)
        native = self.env["mail.shortcode"].create(
            {"source": "native-public", "substitution": "Ordinary native reply"}
        )
        employee = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Discuss recipient outside Contact Center",
                    "login": "discuss-only-%s" % uuid.uuid4(),
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )
        for recipient in (self.agent, self.other, employee):
            for elevated in (False, True):
                user = recipient.with_user(recipient).sudo(elevated)
                shortcodes = user._init_messaging()["shortcodes"]
                ids = {row["id"] for row in shortcodes}
                self.assertIn(native.id, ids)
                self.assertIn(shared.shortcode_id.id, ids)
                if recipient == self.agent:
                    self.assertIn(personal.shortcode_id.id, ids)
                else:
                    self.assertNotIn(personal.shortcode_id.id, ids)
                    self.assertNotIn(archived.shortcode_id.id, ids)
                    self.assertNotIn(personal.shortcut, str(shortcodes))

    def test_quick_reply_search_and_discuss_exclude_other_company_content(self):
        company = self.env["res.company"].create({"name": "Reply search company"})
        self.agent.company_ids |= company
        marker = "company-search-%s" % uuid.uuid4()
        own = self._reply(shortcut=marker)
        foreign = (
            self.env["contact.center.quick.reply.binding"]
            .with_context(allowed_company_ids=company.ids)
            .create(
                {
                    "shortcut": marker + "-foreign",
                    "body": "Other company personal body",
                    "company_id": company.id,
                    "scope": "personal",
                    "owner_id": self.agent.id,
                }
            )
        )
        # Both companies may be active: the inbox company still defines the
        # catalog, even when a server-side caller is elevated.
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(allowed_company_ids=(self.env.company | company).ids)
        )
        for elevated in (False, True):
            result = api.sudo(elevated).search_quick_replies(self.channel.id, marker)
            self.assertEqual([row["binding_id"] for row in result["items"]], own.ids)
        user = self.agent.with_user(self.agent).with_context(
            allowed_company_ids=self.env.company.ids
        )
        shortcodes = user._init_messaging()["shortcodes"]
        self.assertIn(own.shortcode_id.id, [row["id"] for row in shortcodes])
        self.assertNotIn(foreign.shortcode_id.id, [row["id"] for row in shortcodes])

    def test_quick_reply_menu_is_available_to_agents(self):
        menu = self.env.ref("contact_center_base.menu_contact_center_quick_replies")
        self.assertEqual(
            menu.parent_id, self.env.ref("contact_center_base.menu_contact_center_root")
        )
        self.assertIn(
            self.env.ref("contact_center_base.group_contact_center_agent"),
            menu.groups_id,
        )

    def test_tag_catalog_is_company_scoped_and_reports_creation_rights(self):
        own = self.env["contact.center.tag"].create({"name": "Own tag"})
        company = self.env["res.company"].create({"name": "Other tag company"})
        other = self.env["contact.center.tag"].create(
            {"name": "Other tag", "company_id": company.id}
        )
        api = self.env["contact.center.ui.api"]
        catalog = api.with_user(self.agent).conversation_tag_catalog(self.channel.id)
        ids = [item["id"] for item in catalog["items"]]
        self.assertIn(own.id, ids)
        self.assertNotIn(other.id, ids)
        self.assertFalse(catalog["can_create"])
        self.assertTrue(
            api.with_user(self.supervisor).conversation_tag_catalog(self.channel.id)[
                "can_create"
            ]
        )

    def test_supervisor_can_register_a_tag_without_applying_it(self):
        second = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            conversation_type="group",
            name="Another tag conversation",
        )
        before = {
            channel.id: channel.contact_center_tag_ids.ids
            for channel in self.channel | second
        }
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.supervisor)
            .create_conversation_tag(self.channel.id, "  New team tag  ", color=3)
        )
        tag = self.env["contact.center.tag"].browse(result["created_id"])
        self.assertEqual(tag.name, "New team tag")
        self.assertEqual(tag.color, 3)
        self.assertEqual(tag.company_id, self.channel.contact_center_company_id)
        self.assertNotIn("item", result)
        self.assertIn(tag.id, [item["id"] for item in result["items"]])
        self.assertEqual(
            before,
            {
                channel.id: channel.contact_center_tag_ids.ids
                for channel in self.channel | second
            },
        )

    def test_previously_registered_tag_is_applied_only_by_explicit_selection(self):
        api = self.env["contact.center.ui.api"].with_user(self.supervisor)
        name = "Reusable tag %s" % uuid.uuid4()
        with patch.object(
            type(api),
            "update_conversation",
            side_effect=AssertionError("Registration must not apply tags"),
        ):
            result = api.create_conversation_tag(self.channel.id, name)
        tag = self.env["contact.center.tag"].browse(result["created_id"])
        self.assertNotIn(tag, self.channel.contact_center_tag_ids)
        api.update_conversation(
            self.channel.id,
            {"tag_ids": self.channel.contact_center_tag_ids.ids + tag.ids},
        )
        self.assertIn(tag, self.channel.contact_center_tag_ids)
        self.assertEqual(
            self.env["contact.center.tag"].search_count([("name", "=", name)]), 1
        )

    def test_tag_creation_cannot_target_an_unauthorized_channel(self):
        channel = self.env["mail.channel"].create(
            {"name": "Not a Contact Center conversation"}
        )
        name = "Wrong conversation tag %s" % uuid.uuid4()
        with self.assertRaises(ValidationError):
            self.env["contact.center.ui.api"].with_user(
                self.supervisor
            ).create_conversation_tag(channel.id, name)
        self.assertFalse(
            self.env["contact.center.tag"].search_count([("name", "=", name)])
        )

    def test_tag_creation_denies_agents_and_validates_input(self):
        api = self.env["contact.center.ui.api"]
        with self.assertRaises(AccessError):
            api.with_user(self.agent).create_conversation_tag(self.channel.id, "Denied")
        for name, color in (
            ("", 0),
            ("   ", 0),
            ("A" * 101, 0),
            ("Tag", True),
            ("Tag", 12),
            ("Tag", -1),
        ):
            with self.assertRaises(ValidationError):
                api.with_user(self.supervisor).create_conversation_tag(
                    self.channel.id, name, color
                )
