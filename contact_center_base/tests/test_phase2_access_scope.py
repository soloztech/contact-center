import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase


class TestContactCenterAccessScope(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        cls.admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.joao = cls._create_user("Joao", cls.agent_group)
        cls.maria = cls._create_user("Maria", cls.agent_group)
        cls.outsider = cls._create_user("Outsider", cls.agent_group)
        cls.supervisor = cls._create_user("Supervisor", cls.supervisor_group)
        cls.owner_supervisor = cls._create_user(
            "Owner Supervisor", cls.supervisor_group
        )
        cls.admin = cls._create_user("Administrator", cls.admin_group)

        cls.general_team = cls._create_team(
            "Geral", agents=cls.joao | cls.maria, supervisors=cls.supervisor
        )
        cls.joao_team = cls._create_team(
            "Joao", agents=cls.joao, supervisors=cls.supervisor
        )
        cls.maria_team = cls._create_team(
            "Maria", agents=cls.maria, supervisors=cls.supervisor
        )
        cls.general = cls._create_inbox("Geral", cls.general_team)
        cls.joao_inbox = cls._create_inbox("Joao", cls.joao_team)
        cls.maria_inbox = cls._create_inbox("Maria", cls.maria_team)
        cls.inboxes = cls.general | cls.joao_inbox | cls.maria_inbox
        cls.connections = cls.env["contact.center.provider.connection"]
        cls.channels = cls.env["mail.channel"]
        cls.channel_by_account = {}
        for account in cls.inboxes:
            connection = cls.env["contact.center.provider.connection"].create(
                {
                    "name": "%s Provider" % account.name,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": str(uuid.uuid4()),
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                    "capabilities_json": {"send_message": True},
                }
            )
            cls.connections |= connection
            channel = cls._create_conversation(account)
            cls.channels |= channel
            cls.channel_by_account[account.id] = channel

    @classmethod
    def _create_user(cls, name, group, company=None):
        company = company or cls.env.company
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center %s" % name,
                    "login": "cc-scope-%s-%s" % (name.lower(), uuid.uuid4()),
                    "email": "cc-scope-%s@example.invalid" % name.lower(),
                    "company_id": company.id,
                    "company_ids": [(6, 0, company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    @classmethod
    def _create_team(cls, name, agents=None, supervisors=None, company=None):
        company = company or cls.env.company
        agents = agents or cls.env["res.users"]
        supervisors = supervisors or cls.env["res.users"]
        return cls.env["contact.center.team"].create(
            {
                "name": "%s %s" % (name, uuid.uuid4()),
                "company_id": company.id,
                "agent_ids": [(6, 0, agents.ids)],
                "supervisor_ids": [(6, 0, supervisors.ids)],
            }
        )

    @classmethod
    def _create_inbox(cls, name, team=None, company=None, owner=None):
        company = company or (team.company_id if team else owner.company_id)
        values = {
            "name": "%s %s" % (name, uuid.uuid4()),
            "company_id": company.id,
            "platform": "whatsapp",
            "external_ref": str(uuid.uuid4()),
        }
        if team:
            values["default_team_id"] = team.id
        if owner:
            values["owner_user_id"] = owner.id
        return cls.env["contact.center.account"].create(values)

    @classmethod
    def _create_conversation(cls, account):
        guest = (
            cls.env["mail.guest"].sudo().create({"name": "Remote %s" % account.name})
        )
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": guest.name,
                    "company_id": account.company_id.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            team=account.default_team_id,
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

    def _ids(self, records):
        return set(records.ids)

    def test_explicit_empty_company_scope_never_falls_back_to_active_companies(self):
        empty_companies = self.env["res.company"]
        scoped_models = (
            self.env["contact.center.team"],
            self.env["contact.center.account"],
            self.env["contact.center.provider.connection"],
        )
        for model in scoped_models:
            with self.subTest(model=model._name):
                records = model.search(
                    model._contact_center_scope_domain(
                        user=self.joao,
                        companies=empty_companies,
                    )
                )
                self.assertFalse(records)

    def test_general_personal_inbox_scope_filters_bootstrap_and_connections(self):
        expected_scopes = (
            (
                self.joao,
                self.general | self.joao_inbox,
                self.general_team | self.joao_team,
            ),
            (
                self.maria,
                self.general | self.maria_inbox,
                self.general_team | self.maria_team,
            ),
            (
                self.outsider,
                self.env["contact.center.account"],
                self.env["contact.center.team"],
            ),
            (
                self.supervisor,
                self.inboxes,
                self.general_team | self.joao_team | self.maria_team,
            ),
        )
        for user, accounts, teams in expected_scopes:
            with self.subTest(user=user.name):
                api = self.env["contact.center.ui.api"].with_user(user)
                bootstrap = api.bootstrap()
                self.assertEqual(
                    {item["id"] for item in bootstrap["accounts"]},
                    self._ids(accounts),
                )
                self.assertEqual(
                    {item["id"] for item in bootstrap["teams"]},
                    self._ids(teams),
                )
                scoped_connections = (
                    self.env["contact.center.provider.connection"]
                    .with_user(user)
                    .search([])
                )
                self.assertEqual(
                    self._ids(scoped_connections.account_id), self._ids(accounts)
                )
                self.assertNotIn(
                    self.outsider.id,
                    {item["id"] for item in bootstrap["agents"]},
                )

        admin_accounts = (
            self.env["contact.center.account"]
            .with_user(self.admin)
            .search([("id", "in", self.inboxes.ids)])
        )
        admin_connections = (
            self.env["contact.center.provider.connection"]
            .with_user(self.admin)
            .search([("id", "in", self.connections.ids)])
        )
        self.assertEqual(self._ids(admin_accounts), self._ids(self.inboxes))
        self.assertEqual(self._ids(admin_connections), self._ids(self.connections))
        admin_bootstrap = (
            self.env["contact.center.ui.api"].with_user(self.admin).bootstrap()
        )
        self.assertFalse(admin_bootstrap["accounts"])
        self.assertFalse(admin_bootstrap["teams"])
        self.assertFalse(
            self.env["contact.center.ui.api"]
            .with_user(self.admin)
            .list_conversations()["items"]
        )

    def test_owner_only_inbox_is_exclusive_and_live_ready(self):
        account = self._create_inbox("Exclusive", owner=self.outsider)
        connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Exclusive Provider",
                "account_id": account.id,
                "adapter_key": "test.fake",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        channel = self._create_conversation(account)

        bootstrap = (
            self.env["contact.center.ui.api"].with_user(self.outsider).bootstrap()
        )
        self.assertIn(account.id, {item["id"] for item in bootstrap["accounts"]})
        self.assertIn(self.outsider.id, {item["id"] for item in bootstrap["agents"]})
        self.assertFalse(channel.contact_center_team_id)
        self.assertEqual(channel.contact_center_owner_user_id, self.outsider)
        self.assertEqual(
            set(channel.sudo().channel_member_ids.partner_id.ids),
            set(self.outsider.partner_id.ids),
        )
        self.assertTrue(connection._contact_center_inbound_is_available())

        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.joao).get_conversation(
                channel.id
            )
        self.assertFalse(account.with_user(self.joao).search([("id", "=", account.id)]))

    def test_owner_and_team_are_one_union_and_owner_can_read_roster(self):
        account = self._create_inbox(
            "Owner plus team", team=self.general_team, owner=self.outsider
        )
        channel = self._create_conversation(account)
        expected_users = (
            self.general_team.agent_ids
            | self.general_team.supervisor_ids
            | self.outsider
        )

        self.assertEqual(
            set(channel.sudo().channel_member_ids.partner_id.ids),
            set(expected_users.partner_id.ids),
        )
        for user in (self.outsider, self.joao, self.maria, self.supervisor):
            with self.subTest(user=user.display_name):
                self.assertTrue(account.with_user(user).search_count([]))

        owner_bootstrap = (
            self.env["contact.center.ui.api"].with_user(self.outsider).bootstrap()
        )
        self.assertIn(
            self.general_team.id,
            {item["id"] for item in owner_bootstrap["teams"]},
        )
        serialized_team = next(
            item
            for item in owner_bootstrap["teams"]
            if item["id"] == self.general_team.id
        )
        self.assertEqual(
            set(serialized_team["agent_ids"]),
            set((self.general_team.agent_ids | self.general_team.supervisor_ids).ids),
        )

        binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", channel.id)]
        )
        with self.assertRaises(ValidationError):
            self.env["mail.channel"]._contact_center_create_channel(
                account=account,
                identity=binding.identity_id,
                team=self.maria_team,
            )

    def test_live_route_cannot_lose_the_last_union_attendant_from_either_side(self):
        team = self._create_team("Union invariant", agents=self.joao)
        account = self._create_inbox("Union invariant", team=team, owner=self.outsider)
        self.env["contact.center.provider.connection"].create(
            {
                "name": "Union invariant provider",
                "account_id": account.id,
                "adapter_key": "test.fake",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )

        account_revision = account.access_topology_revision
        team.write({"agent_ids": [(5, 0, 0)]})
        account.invalidate_recordset(["access_topology_revision", "owner_user_id"])
        self.assertGreater(account.access_topology_revision, account_revision)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            account.write({"owner_user_id": False})
        self.assertEqual(account.owner_user_id, self.outsider)

        team.write({"agent_ids": [(4, self.joao.id)]})
        account.write({"owner_user_id": False})
        self.assertFalse(account.owner_user_id)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            team.write({"agent_ids": [(5, 0, 0)]})
        self.assertEqual(team.agent_ids, self.joao)

    def test_owner_supervisor_can_read_but_not_edit_foreign_team_roster(self):
        self._create_inbox(
            "Owner supervisor plus team",
            team=self.general_team,
            owner=self.owner_supervisor,
        )
        scoped_team = self.general_team.with_user(self.owner_supervisor)

        self.assertEqual(scoped_team.read(["name"])[0]["name"], self.general_team.name)
        with self.assertRaises(AccessError):
            scoped_team.write({"agent_ids": [(4, self.owner_supervisor.id)]})

    def test_owner_revocation_reconciles_members_and_responsible(self):
        account = self._create_inbox(
            "Revocation", team=self.general_team, owner=self.outsider
        )
        channel = self._create_conversation(account)
        guest_ids = set(channel.sudo().channel_member_ids.guest_id.ids)
        self.env["contact.center.ui.api"].with_user(
            self.supervisor
        ).update_conversation(channel.id, {"responsible_id": self.outsider.id})
        self.assertEqual(channel.contact_center_responsible_id, self.outsider)

        account.write({"owner_user_id": False})
        channel.invalidate_recordset(
            [
                "contact_center_owner_user_id",
                "contact_center_responsible_id",
                "channel_member_ids",
            ]
        )
        self.assertFalse(channel.contact_center_owner_user_id)
        self.assertFalse(channel.contact_center_responsible_id)
        self.assertNotIn(
            self.outsider.partner_id.id,
            channel.sudo().channel_member_ids.partner_id.ids,
        )
        self.assertEqual(set(channel.sudo().channel_member_ids.guest_id.ids), guest_ids)
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.outsider).get_conversation(
                channel.id
            )

    def test_owner_revocation_reconciles_inactive_and_merged_history(self):
        account = self._create_inbox(
            "Historical revocation", team=self.general_team, owner=self.outsider
        )
        inactive_channel = self._create_conversation(account)
        inactive_binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", inactive_channel.id)]
        )
        inactive_binding.write({"active": False})

        survivor_channel = self._create_conversation(account)
        survivor_binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", survivor_channel.id)]
        )
        merged_channel = self._create_conversation(account)
        merged_binding = self.env["contact.center.channel.binding"].search(
            [("channel_id", "=", merged_channel.id)]
        )
        merged_binding.write({"active": False, "merged_into_id": survivor_binding.id})

        account.write({"owner_user_id": False})

        for channel in inactive_channel | survivor_channel | merged_channel:
            channel.invalidate_recordset(["channel_member_ids"])
            self.assertNotIn(
                self.outsider.partner_id.id,
                channel.sudo().channel_member_ids.partner_id.ids,
            )

    def test_invalid_owner_and_unscoped_live_route_fail_closed(self):
        non_agent = self._create_user(
            "Owner without role", self.env.ref("base.group_user")
        )
        with self.assertRaises(ValidationError):
            self._create_inbox("Invalid owner", owner=non_agent)

        account = self.env["contact.center.account"].create(
            {
                "name": "Unscoped draft %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
            }
        )
        connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Unscoped provider",
                "account_id": account.id,
                "adapter_key": "test.fake",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
            }
        )
        self.assertFalse(account._contact_center_access_is_ready())
        self.assertEqual(connection.role, "standby")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)
        self.assertFalse(connection._contact_center_inbound_is_available())
        self.assertFalse(connection._contact_center_outbound_is_available())
        with self.assertRaises(ValidationError):
            connection.action_use_as_primary()
        with self.assertRaises(ValidationError):
            self.env["contact.center.provider.connection"].create(
                {
                    "name": "Explicit unscoped primary",
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": str(uuid.uuid4()),
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                    "state": "connected",
                }
            )

    def test_archiving_inbox_revokes_and_restoring_rebuilds_channel_membership(self):
        account = self.general
        channel = self.channel_by_account[account.id]
        guest_ids = set(channel.sudo().channel_member_ids.guest_id.ids)
        expected_partner_ids = set(
            account._contact_center_effective_users().partner_id.ids
        )
        self.assertTrue(expected_partner_ids)

        account.write({"active": False})
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertFalse(channel.sudo().channel_member_ids.partner_id)
        self.assertEqual(set(channel.sudo().channel_member_ids.guest_id.ids), guest_ids)

        account.write({"active": True})
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertEqual(
            set(channel.sudo().channel_member_ids.partner_id.ids),
            expected_partner_ids,
        )
        self.assertEqual(set(channel.sudo().channel_member_ids.guest_id.ids), guest_ids)

    def test_rpc_ids_and_messages_require_the_same_scope(self):
        maria_channel = self.channel_by_account[self.maria_inbox.id]
        joao_api = self.env["contact.center.ui.api"].with_user(self.joao)
        with self.assertRaises(AccessError):
            joao_api.get_conversation(maria_channel.id)
        with self.assertRaises(AccessError):
            joao_api.get_timeline(maria_channel.id)
        with self.assertRaises(AccessError):
            joao_api.send_message(maria_channel.id, "Guessed RPC target")

        self.assertFalse(
            self.env["contact.center.account"]
            .with_user(self.joao)
            .search([("id", "=", self.maria_inbox.id)])
        )
        maria_connection = self.connections.filtered(
            lambda item: item.account_id == self.maria_inbox
        )
        self.assertFalse(
            self.env["contact.center.provider.connection"]
            .with_user(self.joao)
            .search([("id", "=", maria_connection.id)])
        )
        with self.assertRaises(AccessError):
            self.maria_inbox.with_user(self.joao).check_access_rule("read")
        with self.assertRaises(AccessError):
            maria_connection.with_user(self.joao).check_access_rule("read")

        supervisor_api = self.env["contact.center.ui.api"].with_user(self.supervisor)
        self.assertEqual(
            supervisor_api.get_conversation(maria_channel.id)["item"]["channel_id"],
            maria_channel.id,
        )
        general_channel = self.channel_by_account[self.general.id]
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.admin).get_conversation(
                general_channel.id
            )

    def test_administrator_outside_roster_can_audit_company_ledgers(self):
        connection = self.connections.filtered(
            lambda item: item.account_id == self.general
        )
        channel = self.channel_by_account[self.general.id]
        channel_binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("channel_id", "=", channel.id)], limit=1)
        )
        channel_alias = (
            self.env["contact.center.channel.alias"]
            .sudo()
            .create(
                {
                    "channel_binding_id": channel_binding.id,
                    "account_id": self.general.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": "5511999999999@s.whatsapp.net",
                    "value_normalized": "5511999999999@s.whatsapp.net",
                    "role": "primary",
                }
            )
        )
        message = channel.sudo()._contact_center_post(
            "inbound",
            body="Administrator ledger fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": channel_binding.id,
                    "provider_connection_id": connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "external_message_id": "admin-ledger-%s" % uuid.uuid4(),
                }
            )
        )
        delivery = (
            self.env["contact.center.delivery.event"]
            .sudo()
            .create(
                {
                    "message_binding_id": message_binding.id,
                    "state": "delivered",
                    "occurred_at": message.date,
                    "external_event_id": "admin-ledger-%s" % uuid.uuid4(),
                }
            )
        )
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "admin-ledger-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"secret_locator": "admin-only"},
                    "normalized_dto_json": {"schema_version": 1},
                    "metadata_json": {"event": "fixture"},
                    "state": "done",
                }
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "account_id": self.general.id,
                    "provider_connection_id": connection.id,
                    "channel_binding_id": channel_binding.id,
                    "outbox_idempotency_key": "admin-ledger-%s" % uuid.uuid4(),
                    "command_type": "audit_fixture",
                    "command_json": {"schema_version": 1},
                    "state": "dead",
                }
            )
        )

        self.assertNotIn(self.admin, self.general_team.agent_ids)
        self.assertNotIn(self.admin, self.general_team.supervisor_ids)
        self.assertEqual(
            self.env["contact.center.inbox.event"]
            .with_user(self.admin)
            .search([("id", "=", inbox.id)]),
            inbox,
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"]
            .with_user(self.admin)
            .search([("id", "=", outbox.id)]),
            outbox,
        )
        for model_name, record in (
            ("contact.center.channel.binding", channel_binding),
            ("contact.center.channel.alias", channel_alias),
            ("contact.center.message.binding", message_binding),
            ("contact.center.delivery.event", delivery),
        ):
            with self.subTest(model=model_name):
                self.assertEqual(
                    self.env[model_name]
                    .with_user(self.admin)
                    .search([("id", "=", record.id)]),
                    record,
                )
        self.assertEqual(
            inbox.with_user(self.admin).read(["raw_envelope_json"])[0][
                "raw_envelope_json"
            ],
            {"secret_locator": "admin-only"},
        )
        self.assertEqual(
            inbox.with_user(self.supervisor).read(["metadata_json"])[0][
                "metadata_json"
            ],
            {"event": "fixture"},
        )
        with self.assertRaises(AccessError):
            inbox.with_user(self.supervisor).read(["raw_envelope_json"])
        with self.assertRaises(AccessError):
            inbox.with_user(self.supervisor).read(["normalized_dto_json"])

        unassigned_account = self.env["contact.center.account"].create(
            {
                "name": "Unassigned conflict %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
            }
        )
        identities = self.env["contact.center.identity"]
        for suffix in ("A", "B"):
            guest = (
                self.env["mail.guest"]
                .sudo()
                .create({"name": "Unassigned conflict %s" % suffix})
            )
            identities |= (
                self.env["contact.center.identity"]
                .sudo()
                .create(
                    {
                        "name": guest.name,
                        "company_id": self.env.company.id,
                        "mail_guest_id": guest.id,
                    }
                )
            )
        conflict = (
            self.env["contact.center.identity.conflict"]
            .sudo()
            .create(
                {
                    "account_id": unassigned_account.id,
                    "identity_ids": [(6, 0, identities.ids)],
                    "address_evidence_json": [{"fixture": True}],
                }
            )
        )
        visible_conflict = (
            self.env["contact.center.identity.conflict"]
            .with_user(self.admin)
            .search([("id", "=", conflict.id)])
        )
        self.assertEqual(visible_conflict, conflict)
        self.assertFalse(
            self.env["contact.center.identity.conflict"]
            .with_user(self.supervisor)
            .search([("id", "=", conflict.id)])
        )
        visible_conflict.action_resolve()
        conflict.invalidate_recordset(["state", "resolved_by_id"])
        self.assertEqual(conflict.state, "resolved")
        self.assertEqual(conflict.resolved_by_id, self.admin)

    def test_roster_revocation_removes_responsibility_and_history(self):
        general_channel = self.channel_by_account[self.general.id]
        joao_api = self.env["contact.center.ui.api"].with_user(self.joao)
        joao_api.claim_conversation(general_channel.id)
        general_channel.with_user(self.joao).message_post(
            body="History before revocation", message_type="comment"
        )
        self.assertTrue(joao_api.get_timeline(general_channel.id)["items"])

        self.general_team.write({"agent_ids": [(3, self.joao.id)]})
        general_channel.invalidate_recordset(
            ["channel_member_ids", "contact_center_responsible_id"]
        )
        self.assertNotIn(
            self.joao.partner_id, general_channel.channel_member_ids.partner_id
        )
        self.assertFalse(general_channel.contact_center_responsible_id)
        self.assertIn(
            self.maria.partner_id, general_channel.channel_member_ids.partner_id
        )
        with self.assertRaises(AccessError):
            joao_api.get_timeline(general_channel.id)
        self.assertNotIn(
            self.general.id,
            {item["id"] for item in joao_api.bootstrap()["accounts"]},
        )
        self.assertFalse(
            self.env["contact.center.provider.connection"]
            .with_user(self.joao)
            .search([("account_id", "=", self.general.id)])
        )

    def test_team_is_fixed_by_inbox_and_assignment_stays_inside_it(self):
        general_channel = self.channel_by_account[self.general.id]
        joao_channel = self.channel_by_account[self.joao_inbox.id]
        supervisor_api = self.env["contact.center.ui.api"].with_user(self.supervisor)
        for channel, team in (
            (general_channel, self.joao_team),
            (joao_channel, self.joao_team),
        ):
            with self.subTest(channel=channel.id, team=team.id), self.assertRaisesRegex(
                ValidationError, "team is defined by its inbox"
            ):
                supervisor_api.update_conversation(channel.id, {"team_id": team.id})
        with self.assertRaises(ValidationError):
            supervisor_api.update_conversation(
                joao_channel.id, {"responsible_id": self.maria.id}
            )
        result = supervisor_api.update_conversation(
            joao_channel.id,
            {"responsible_id": self.joao.id},
        )
        joao_channel.invalidate_recordset(
            ["contact_center_team_id", "contact_center_responsible_id"]
        )
        self.assertEqual(joao_channel.contact_center_team_id, self.joao_team)
        self.assertEqual(joao_channel.contact_center_responsible_id, self.joao)
        self.assertEqual(result["item"]["responsible"]["id"], self.joao.id)
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.joao).update_conversation(
                joao_channel.id, {"responsible_id": self.supervisor.id}
            )

    def test_multi_company_scope_requires_membership_and_active_company(self):
        other_company = self.env["res.company"].create(
            {"name": "Other Contact Center Scope %s" % uuid.uuid4()}
        )
        self.joao.write({"company_ids": [(4, other_company.id)]})
        self.admin.write({"company_ids": [(4, other_company.id)]})
        other_team = self._create_team("Other", agents=self.joao, company=other_company)
        other_account = self._create_inbox("Other", other_team, company=other_company)
        other_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Other Provider",
                "account_id": other_account.id,
                "adapter_key": "test.fake",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
                "capabilities_json": {"send_message": True},
            }
        )
        other_channel = self._create_conversation(other_account)
        current_company_ids = self.env.company.ids
        both_company_ids = (self.env.company | other_company).ids
        joao_api = self.env["contact.center.ui.api"].with_user(self.joao)

        current_bootstrap = joao_api.with_context(
            allowed_company_ids=current_company_ids
        ).bootstrap()
        self.assertNotIn(
            other_account.id,
            {item["id"] for item in current_bootstrap["accounts"]},
        )
        with self.assertRaises(AccessError):
            joao_api.with_context(
                allowed_company_ids=current_company_ids
            ).get_conversation(other_channel.id)

        both_bootstrap = joao_api.with_context(
            allowed_company_ids=both_company_ids
        ).bootstrap()
        self.assertIn(
            other_account.id,
            {item["id"] for item in both_bootstrap["accounts"]},
        )
        self.assertEqual(
            joao_api.with_context(
                allowed_company_ids=both_company_ids
            ).get_conversation(other_channel.id)["item"]["channel_id"],
            other_channel.id,
        )
        self.assertFalse(
            self.env["contact.center.provider.connection"]
            .with_user(self.joao)
            .with_context(allowed_company_ids=current_company_ids)
            .search([("id", "=", other_connection.id)])
        )
        self.assertEqual(
            self.env["contact.center.provider.connection"]
            .with_user(self.admin)
            .with_context(allowed_company_ids=both_company_ids)
            .search([("id", "=", other_connection.id)]),
            other_connection,
        )
