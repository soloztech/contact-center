import uuid

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase


class TestMultiAccessConversationUi(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.users = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {
                        "name": "Access User %s" % index,
                        "login": "cc-access-ui-%s" % uuid.uuid4(),
                        "company_id": cls.env.company.id,
                        "company_ids": [fields.Command.set(cls.env.company.ids)],
                        "groups_id": [fields.Command.set(group.ids)],
                    }
                    for index in range(3)
                ]
            )
        )
        cls.teams = cls.env["contact.center.team"].create(
            [
                {
                    "name": "Access Team %s" % index,
                    "company_id": cls.env.company.id,
                    "agent_ids": [fields.Command.set(user.ids)],
                }
                for index, user in enumerate(cls.users[1:])
            ]
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Multiple access grants UI",
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "access_user_ids": [fields.Command.set(cls.users[:2].ids)],
                "access_team_ids": [fields.Command.set(cls.teams.ids)],
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Multiple access customer"})
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
            teams=cls.teams,
            responsible=cls.users[1],
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "cc-access-ui-%s" % uuid.uuid4(),
            }
        )

    def test_agent_payload_hides_access_grants_and_keeps_responsible(self):
        for viewer in self.users:
            with self.subTest(viewer=viewer.name):
                ui = self.env["contact.center.ui.api"].with_user(viewer)
                items = [
                    ui.get_conversation(self.channel.id)["item"],
                    ui.update_conversation(self.channel.id, {})["item"],
                    *ui.list_conversations(filters={"account_id": self.account.id})[
                        "items"
                    ],
                ]
                self.assertEqual(len(items), 3)
                for item in items:
                    self.assertFalse(item["capabilities"]["view_inbox_access"])
                    self.assertEqual(item["access_users"], [])
                    self.assertEqual(item["access_teams"], [])
                    self.assertNotIn("owner", item)
                    self.assertNotIn("team", item)
                    self.assertEqual(item["responsible"]["id"], self.users[1].id)
                    self.assertEqual(
                        item["responsible"]["name"], self.users[1].display_name
                    )
                self.assertFalse(ui.bootstrap()["capabilities"]["manage_assignment"])
                with self.assertRaises(AccessError):
                    ui.update_conversation(
                        self.channel.id, {"responsible_id": viewer.id}
                    )

    def _management_api(self, group_xmlid):
        # This direct user is outside both teams. Managers still need the full
        # inbox access projection to populate the responsible selector.
        viewer = self.users[0]
        viewer.write({"groups_id": [fields.Command.link(self.env.ref(group_xmlid).id)]})
        return self.env["contact.center.ui.api"].with_user(viewer)

    def _assert_management_access_projection(self, ui):
        items = [
            ui.get_conversation(self.channel.id)["item"],
            *ui.list_conversations(filters={"account_id": self.account.id})["items"],
        ]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertTrue(item["capabilities"]["view_inbox_access"])
            self.assertEqual(
                {user["id"] for user in item["access_users"]},
                set(self.users[:2].ids),
            )
            self.assertEqual(
                {team["id"] for team in item["access_teams"]}, set(self.teams.ids)
            )

        bootstrap = ui.bootstrap()
        self.assertTrue(bootstrap["capabilities"]["manage_assignment"])
        self.assertTrue(
            set(self.teams.ids).issubset({team["id"] for team in bootstrap["teams"]})
        )
        self.assertTrue(
            set(self.users.ids).issubset({user["id"] for user in bootstrap["agents"]})
        )
        assigned = ui.update_conversation(
            self.channel.id, {"responsible_id": self.users[2].id}
        )["item"]
        self.assertTrue(assigned["capabilities"]["view_inbox_access"])
        self.assertEqual(assigned["responsible"]["id"], self.users[2].id)

    def test_supervisor_can_view_access_and_assign_conversation(self):
        ui = self._management_api("contact_center_base.group_contact_center_supervisor")
        self._assert_management_access_projection(ui)

    def test_administrator_can_view_access_and_assign_conversation(self):
        ui = self._management_api("contact_center_base.group_contact_center_admin")
        self._assert_management_access_projection(ui)

    def test_conversation_refreshes_after_partial_access_revocation(self):
        ui = self._management_api("contact_center_base.group_contact_center_supervisor")
        self.account.write(
            {
                "access_user_ids": [fields.Command.unlink(self.users[1].id)],
                "access_team_ids": [fields.Command.unlink(self.teams[1].id)],
            }
        )
        item = ui.get_conversation(self.channel.id)["item"]
        self.assertEqual(
            [user["id"] for user in item["access_users"]], self.users[:1].ids
        )
        self.assertEqual(
            [team["id"] for team in item["access_teams"]], self.teams[:1].ids
        )
        self.assertEqual(
            set(self.channel.channel_member_ids.partner_id.ids),
            set(self.users[:2].partner_id.ids),
        )
