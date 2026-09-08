import uuid

from odoo import fields
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

    def test_conversation_returns_every_access_grant_and_complete_roster(self):
        # A direct user outside both teams must still receive their names and
        # rosters, otherwise the responsible selector silently loses attendants.
        for viewer in self.users:
            with self.subTest(viewer=viewer.name):
                ui = self.env["contact.center.ui.api"].with_user(viewer)
                item = ui.get_conversation(self.channel.id)["item"]
                self.assertEqual(
                    {user["id"] for user in item["access_users"]},
                    set(self.users[:2].ids),
                )
                self.assertEqual(
                    {team["id"] for team in item["access_teams"]}, set(self.teams.ids)
                )
                self.assertNotIn("owner", item)
                self.assertNotIn("team", item)
                self.assertFalse(item["responsible"])

                bootstrap = ui.bootstrap()
                self.assertEqual(
                    {team["id"] for team in bootstrap["teams"]}, set(self.teams.ids)
                )
                self.assertEqual(
                    {user["id"] for user in bootstrap["agents"]}, set(self.users.ids)
                )

    def test_conversation_refreshes_after_partial_access_revocation(self):
        self.account.write(
            {
                "access_user_ids": [fields.Command.unlink(self.users[1].id)],
                "access_team_ids": [fields.Command.unlink(self.teams[1].id)],
            }
        )
        item = (
            self.env["contact.center.ui.api"]
            .with_user(self.users[0])
            .get_conversation(self.channel.id)["item"]
        )
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
