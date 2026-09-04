import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..models.application import IdentityConflictError
from ..services.dto import EventDTO
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN


class TestCrossAccountIdentity(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Cross-inbox Agent",
                    "login": "cc-cross-inbox-%s" % uuid.uuid4(),
                    "email": "cc-cross-inbox@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Cross-inbox %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account_a, cls.connection_a = cls._create_account_connection(
            "Inbox A", cls.env.company, cls.team
        )
        cls.account_b, cls.connection_b = cls._create_account_connection(
            "Inbox B", cls.env.company, cls.team
        )

        cls.other_company = cls.env["res.company"].create(
            {"name": "Other Contact Center Company %s" % uuid.uuid4()}
        )
        cls.other_agent = (
            cls.env["res.users"]
            .sudo()
            .with_company(cls.other_company)
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other-company Agent",
                    "login": "cc-other-company-%s" % uuid.uuid4(),
                    "email": "cc-other-company@example.invalid",
                    "company_id": cls.other_company.id,
                    "company_ids": [(6, 0, cls.other_company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.other_team = (
            cls.env["contact.center.team"]
            .sudo()
            .with_company(cls.other_company)
            .create(
                {
                    "name": "Other-company %s" % uuid.uuid4(),
                    "company_id": cls.other_company.id,
                    "agent_ids": [(6, 0, cls.other_agent.ids)],
                }
            )
        )
        cls.other_account, cls.other_connection = cls._create_account_connection(
            "Other-company Inbox",
            cls.other_company,
            cls.other_team,
            sudo=True,
        )

    @classmethod
    def _create_account_connection(cls, name, company, team, sudo=False):
        env = cls.env
        if sudo:
            env = cls.env["contact.center.account"].sudo().with_company(company).env
        account = env["contact.center.account"].create(
            {
                "name": "%s %s" % (name, uuid.uuid4()),
                "company_id": company.id,
                "platform": "whatsapp",
                "external_ref": "account-%s" % uuid.uuid4(),
                "default_team_id": team.id,
            }
        )
        connection = env["contact.center.provider.connection"].create(
            {
                "name": "%s Provider" % name,
                "account_id": account.id,
                "adapter_key": "test.fake",
                "external_ref": "connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )
        return account, connection

    def _event(self, connection, person_address, *, lid=None, company_scope=True):
        actor_addresses = []
        if lid:
            actor_addresses.append(
                {
                    "namespace": "whatsapp.lid",
                    "value": lid,
                    "value_normalized": lid,
                    "role": "primary",
                    "confidence": "protocol",
                    "resolution_scope": "account",
                }
            )
        actor_addresses.append(
            {
                "namespace": ("whatsapp.pn" if company_scope else "whatsapp.lid"),
                "value": person_address,
                "value_normalized": person_address,
                "role": "alternate" if lid else "primary",
                "confidence": "protocol",
                "resolution_scope": "company" if company_scope else "account",
            }
        )
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "event-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-25T12:00:00Z",
                "account_ref": connection.account_id.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": "conversation-%s" % uuid.uuid4(),
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Shared Person",
                    "addresses": actor_addresses,
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [dict(actor_addresses[-1], role="primary")],
                },
                "message": {
                    "external_message_id": "message-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Cross-inbox identity fixture",
                },
            }
        )

    def _process(self, connection, event, *, company=None):
        application = self.env["contact.center.application"]
        if company:
            application = application.sudo().with_company(company)
            connection = connection.with_env(application.env)
        return application._process_event(connection, event)

    def _account_scoped_pn_event(self, connection, pn):
        values = self._event(connection, pn, company_scope=False).to_dict()
        address = {
            "namespace": "whatsapp.pn",
            "value": pn,
            "value_normalized": pn,
            "role": "primary",
            "confidence": "protocol",
            "resolution_scope": "account",
        }
        values["actor"]["addresses"] = [address]
        values["conversation"]["addresses"] = [address]
        return EventDTO.from_dict(values)

    def _binding_for_message(self, message):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)], limit=1)
            .channel_binding_id
        )

    def _late_portable_bridge_fixture(self, left_connection, right_connection):
        unique = str(uuid.uuid4().int)
        pn = "55%s@s.whatsapp.net" % unique[:11]
        left_lid = "%s@lid" % unique[:15]
        right_lid = "%s@lid" % unique[1:16]
        left_message = self._process(
            left_connection,
            self._event(left_connection, left_lid, company_scope=False),
        )
        right_message = self._process(
            right_connection,
            self._event(right_connection, right_lid, company_scope=False),
        )
        left_binding = self._binding_for_message(left_message)
        right_binding = self._binding_for_message(right_message)
        self._process(
            right_connection,
            self._event(right_connection, pn, lid=right_lid),
        )
        bridge_event = self._event(left_connection, pn, lid=left_lid)
        return {
            "pn": pn,
            "left_message": left_message,
            "right_message": right_message,
            "left_binding": left_binding,
            "right_binding": right_binding,
            "bridge_event": bridge_event,
        }

    def test_company_scoped_pn_reuses_guest_across_two_inboxes(self):
        unique = str(uuid.uuid4().int)
        pn = "55%s@s.whatsapp.net" % unique[:11]
        lid_a = "%s@lid" % unique[:15]
        lid_b = "%s@lid" % unique[1:16]
        message_a = self._process(
            self.connection_a, self._event(self.connection_a, pn, lid=lid_a)
        )
        message_b = self._process(
            self.connection_b, self._event(self.connection_b, pn, lid=lid_b)
        )
        binding_a = self._binding_for_message(message_a)
        binding_b = self._binding_for_message(message_b)

        self.assertNotEqual(binding_a, binding_b)
        self.assertNotEqual(binding_a.channel_id, binding_b.channel_id)
        self.assertEqual(binding_a.identity_id, binding_b.identity_id)
        self.assertEqual(
            binding_a.identity_id.mail_guest_id,
            binding_b.identity_id.mail_guest_id,
        )
        self.assertEqual(
            set((binding_a | binding_b).mapped("account_id").ids),
            {self.account_a.id, self.account_b.id},
        )

        pn_aliases = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .search(
                [
                    ("namespace", "=", "whatsapp.pn"),
                    ("value_normalized", "=", pn),
                    ("account_id", "in", (self.account_a | self.account_b).ids),
                ]
            )
        )
        self.assertEqual(len(pn_aliases), 2)
        self.assertEqual(len(pn_aliases.mapped("identity_id")), 1)
        self.assertEqual(len(pn_aliases.mapped("identity_id.mail_guest_id")), 1)
        self.assertEqual(set(pn_aliases.mapped("resolution_scope")), {"company"})

        payload_a = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .get_conversation(binding_a.channel_id.id)["item"]["identity"]
        )
        payload_alias_ids = {item["id"] for item in payload_a["aliases"]}
        expected_alias_ids = set(
            binding_a.identity_id.alias_ids.filtered(
                lambda alias: alias.account_id == self.account_a
            ).ids
        )
        self.assertEqual(payload_alias_ids, expected_alias_ids)
        self.assertIn(lid_a, {item["normalized"] for item in payload_a["aliases"]})
        self.assertNotIn(lid_b, {item["normalized"] for item in payload_a["aliases"]})

    def test_protocol_promotion_makes_observed_alias_portable(self):
        pn = "55%s@s.whatsapp.net" % str(uuid.uuid4().int)[:11]
        first_message = self._process(
            self.connection_a,
            self._account_scoped_pn_event(self.connection_a, pn),
        )
        first_binding = self._binding_for_message(first_message)
        alias = first_binding.identity_id.alias_ids.filtered(
            lambda item: item.account_id == self.account_a
            and item.namespace == "whatsapp.pn"
            and item.value_normalized == pn
        )
        self.assertEqual(len(alias), 1)
        alias.sudo().write({"confidence": "observed", "source_field": False})

        self._process(self.connection_a, self._event(self.connection_a, pn))
        alias.invalidate_recordset(["confidence", "resolution_scope"])
        self.assertEqual(alias.confidence, "protocol")
        self.assertEqual(alias.resolution_scope, "company")

        second_message = self._process(
            self.connection_b,
            self._event(self.connection_b, pn),
        )
        second_binding = self._binding_for_message(second_message)
        self.assertEqual(first_binding.identity_id, second_binding.identity_id)
        self.assertEqual(
            first_binding.identity_id.mail_guest_id,
            second_binding.identity_id.mail_guest_id,
        )

    def test_late_portable_pn_merges_two_lid_identities_at_runtime(self):
        fixture = self._late_portable_bridge_fixture(
            self.connection_a, self.connection_b
        )
        left_binding = fixture["left_binding"]
        right_binding = fixture["right_binding"]
        historical_authors = {
            fixture["left_message"].id: fixture["left_message"].author_guest_id,
            fixture["right_message"].id: fixture["right_message"].author_guest_id,
        }
        self.assertNotEqual(left_binding.identity_id, right_binding.identity_id)

        bridge_message = self._process(self.connection_a, fixture["bridge_event"])

        left_binding.invalidate_recordset(["identity_id"])
        right_binding.invalidate_recordset(["identity_id"])
        fixture["left_message"].invalidate_recordset(["author_guest_id"])
        fixture["right_message"].invalidate_recordset(["author_guest_id"])
        self.assertEqual(left_binding.identity_id, right_binding.identity_id)
        canonical_guest = left_binding.identity_id.mail_guest_id
        self.assertEqual(bridge_message.author_guest_id, canonical_guest)
        self.assertEqual(
            fixture["left_message"].author_guest_id,
            historical_authors[fixture["left_message"].id],
        )
        self.assertEqual(
            fixture["right_message"].author_guest_id,
            historical_authors[fixture["right_message"].id],
        )
        for binding in left_binding | right_binding:
            self.assertEqual(
                binding.channel_id.channel_member_ids.guest_id, canonical_guest
            )

    def test_late_portable_pn_keeps_conflict_for_different_contacts(self):
        fixture = self._late_portable_bridge_fixture(
            self.connection_a, self.connection_b
        )
        left_identity = fixture["left_binding"].identity_id
        right_identity = fixture["right_binding"].identity_id
        left_partner = self.env["res.partner"].create({"name": "Left Contact"})
        right_partner = self.env["res.partner"].create({"name": "Right Contact"})
        left_identity.with_user(self.agent).action_link_partner(left_partner.id)
        right_identity.with_user(self.agent).action_link_partner(right_partner.id)

        with self.assertRaises(IdentityConflictError):
            self._process(self.connection_a, fixture["bridge_event"])
        self.assertEqual(left_identity.state, "active")
        self.assertEqual(right_identity.state, "active")
        self.assertNotEqual(left_identity.mail_guest_id, right_identity.mail_guest_id)

    def test_late_portable_pn_keeps_conflict_for_different_manual_names(self):
        fixture = self._late_portable_bridge_fixture(
            self.connection_a, self.connection_b
        )
        left_identity = fixture["left_binding"].identity_id
        right_identity = fixture["right_binding"].identity_id
        left_identity.with_user(self.agent).action_rename_guest("Manual Left")
        right_identity.with_user(self.agent).action_rename_guest("Manual Right")

        with self.assertRaises(IdentityConflictError):
            self._process(self.connection_a, fixture["bridge_event"])
        self.assertEqual(left_identity.state, "active")
        self.assertEqual(right_identity.state, "active")

    def test_late_portable_pn_keeps_conflict_for_two_same_inbox_channels(self):
        fixture = self._late_portable_bridge_fixture(
            self.connection_a, self.connection_a
        )
        left_identity = fixture["left_binding"].identity_id
        right_identity = fixture["right_binding"].identity_id

        with self.assertRaises(IdentityConflictError):
            self._process(self.connection_a, fixture["bridge_event"])
        self.assertEqual(left_identity.state, "active")
        self.assertEqual(right_identity.state, "active")
        self.assertNotEqual(fixture["left_binding"], fixture["right_binding"])

    def test_runtime_merge_reconciles_two_account_scoped_guests_for_one_pn(self):
        pn = "55%s@s.whatsapp.net" % str(uuid.uuid4().int)[:11]
        first_message = self._process(
            self.connection_a,
            self._account_scoped_pn_event(self.connection_a, pn),
        )
        second_message = self._process(
            self.connection_b,
            self._account_scoped_pn_event(self.connection_b, pn),
        )
        first_binding = self._binding_for_message(first_message)
        second_binding = self._binding_for_message(second_message)
        legacy_identities = first_binding.identity_id | second_binding.identity_id
        legacy_guests = legacy_identities.mapped("mail_guest_id")
        self.assertEqual(len(legacy_identities), 2)
        self.assertEqual(len(legacy_guests), 2)
        historical_authors = {
            first_message.id: first_message.author_guest_id,
            second_message.id: second_message.author_guest_id,
        }
        survivor_before = legacy_identities._contact_center_portable_merge_survivor(
            legacy_identities
        )
        retired_before = legacy_identities - survivor_before
        retired_binding = (first_binding | second_binding).filtered(
            lambda item: item.identity_id == retired_before
        )
        retired_message = (
            first_message if first_binding == retired_binding else second_message
        )
        retired_member = (
            self.env["mail.channel.member"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", retired_binding.channel_id.id),
                    ("guest_id", "=", retired_before.mail_guest_id.id),
                ],
                limit=1,
            )
        )
        retired_member.with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write(
            {
                "custom_channel_name": "Legacy inbox state",
                "fetched_message_id": retired_message.id,
                "seen_message_id": retired_message.id,
            }
        )
        retired_member_id = retired_member.id
        retired_message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", retired_message.id)], limit=1)
        )
        historical_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .create(
                {
                    "target_message_binding_id": retired_message_binding.id,
                    "provider_connection_id": (
                        retired_message_binding.provider_connection_id.id
                    ),
                    "external_event_id": "historical-mutation-%s" % uuid.uuid4(),
                    "mutation_type": "react",
                    "direction": "inbound",
                    "actor_guest_id": retired_before.mail_guest_id.id,
                    "reaction_emoji": "👍",
                    "reaction_operation": "add",
                }
            )
        )
        historical_reaction = (
            self.env["mail.message.reaction"]
            .sudo()
            .create(
                {
                    "message_id": retired_message.id,
                    "content": "👍",
                    "guest_id": retired_before.mail_guest_id.id,
                }
            )
        )

        survivor, blocker = (
            self.env["contact.center.identity"]
            .sudo()
            ._contact_center_merge_portable_component(legacy_identities)
        )
        self.assertFalse(blocker)

        first_binding.invalidate_recordset(["identity_id"])
        second_binding.invalidate_recordset(["identity_id"])
        first_message.invalidate_recordset(["author_guest_id"])
        second_message.invalidate_recordset(["author_guest_id"])
        self.assertEqual(survivor, first_binding.identity_id)
        self.assertEqual(first_binding.identity_id, second_binding.identity_id)
        survivor_guest = first_binding.identity_id.mail_guest_id
        self.assertEqual(
            first_message.author_guest_id, historical_authors[first_message.id]
        )
        self.assertEqual(
            second_message.author_guest_id, historical_authors[second_message.id]
        )
        for binding in first_binding | second_binding:
            self.assertEqual(
                binding.channel_id.channel_member_ids.guest_id,
                survivor_guest,
            )
        replacement_member = (
            self.env["mail.channel.member"]
            .sudo()
            .search(
                [
                    ("channel_id", "=", retired_binding.channel_id.id),
                    ("guest_id", "=", survivor_guest.id),
                ],
                limit=1,
            )
        )
        self.assertTrue(replacement_member)
        self.assertNotEqual(replacement_member.id, retired_member_id)
        self.assertFalse(
            self.env["mail.channel.member"].sudo().browse(retired_member_id).exists()
        )
        self.assertEqual(replacement_member.custom_channel_name, "Legacy inbox state")
        self.assertEqual(replacement_member.fetched_message_id, retired_message)
        self.assertEqual(replacement_member.seen_message_id, retired_message)
        self.assertEqual(
            historical_mutation.actor_guest_id, retired_before.mail_guest_id
        )
        self.assertEqual(historical_reaction.guest_id, retired_before.mail_guest_id)
        retired = legacy_identities.filtered(lambda item: item.state == "merged")
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired.merged_into_id, first_binding.identity_id)
        # Consolidating two identities must not upgrade account-scoped evidence
        # to a company-wide portable identifier.  That promotion belongs only to
        # a later protocol-validated observation in ``_enrich_identity_aliases``.
        self.assertEqual(
            set(
                first_binding.identity_id.alias_ids.filtered(
                    lambda alias: alias.namespace == "whatsapp.pn"
                ).mapped("resolution_scope")
            ),
            {"account"},
        )
        future_message = self._process(
            retired_binding.account_id.connection_ids[:1],
            self._account_scoped_pn_event(
                retired_binding.account_id.connection_ids[:1], pn
            ),
        )
        self.assertEqual(future_message.author_guest_id, survivor_guest)

    def test_runtime_merge_collapses_duplicate_survivor_membership(self):
        pn = "55%s@s.whatsapp.net" % str(uuid.uuid4().int)[:11]
        first_message = self._process(
            self.connection_a,
            self._account_scoped_pn_event(self.connection_a, pn),
        )
        second_message = self._process(
            self.connection_b,
            self._account_scoped_pn_event(self.connection_b, pn),
        )
        bindings = self._binding_for_message(first_message) | self._binding_for_message(
            second_message
        )
        identities = bindings.identity_id
        survivor = identities._contact_center_portable_merge_survivor(identities)
        retired = identities - survivor
        retired_binding = bindings.filtered(lambda item: item.identity_id == retired)
        member_model = (
            self.env["mail.channel.member"]
            .sudo()
            .with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
        )
        retired_member = member_model.search(
            [
                ("channel_id", "=", retired_binding.channel_id.id),
                ("guest_id", "=", retired.mail_guest_id.id),
            ],
            limit=1,
        )
        retired_channel_message = (
            first_message
            if self._binding_for_message(first_message) == retired_binding
            else second_message
        )
        retired_member.write({"seen_message_id": retired_channel_message.id})
        canonical_member = member_model.create(
            {
                "channel_id": retired_binding.channel_id.id,
                "guest_id": survivor.mail_guest_id.id,
                "custom_channel_name": "Canonical preference",
            }
        )
        canonical_member_id = canonical_member.id

        merged_survivor, blocker = identities._contact_center_merge_portable_component(
            identities
        )

        canonical_member = member_model.browse(canonical_member_id).exists()
        self.assertFalse(blocker)
        self.assertEqual(merged_survivor, survivor)
        self.assertTrue(canonical_member)
        self.assertEqual(canonical_member.custom_channel_name, "Canonical preference")
        self.assertEqual(canonical_member.seen_message_id, retired_channel_message)
        self.assertFalse(retired_member.exists())
        self.assertEqual(
            member_model.search_count(
                [
                    ("channel_id", "=", retired_binding.channel_id.id),
                    ("guest_id", "=", survivor.mail_guest_id.id),
                ]
            ),
            1,
        )

    def test_account_scoped_lid_does_not_reuse_identity(self):
        lid = "%s@lid" % str(uuid.uuid4().int)[:15]
        first = self._binding_for_message(
            self._process(
                self.connection_a,
                self._event(self.connection_a, lid, company_scope=False),
            )
        )
        second = self._binding_for_message(
            self._process(
                self.connection_b,
                self._event(self.connection_b, lid, company_scope=False),
            )
        )

        self.assertNotEqual(first.identity_id, second.identity_id)
        self.assertNotEqual(
            first.identity_id.mail_guest_id, second.identity_id.mail_guest_id
        )

    def test_company_scoped_pn_never_crosses_company_boundary(self):
        pn = "55%s@s.whatsapp.net" % str(uuid.uuid4().int)[:11]
        local = self._binding_for_message(
            self._process(self.connection_a, self._event(self.connection_a, pn))
        )
        foreign_message = self._process(
            self.other_connection,
            self._event(self.other_connection, pn),
            company=self.other_company,
        )
        foreign = self._binding_for_message(foreign_message)

        self.assertNotEqual(local.identity_id, foreign.identity_id)
        self.assertNotEqual(
            local.identity_id.mail_guest_id, foreign.identity_id.mail_guest_id
        )
        self.assertNotEqual(
            local.identity_id.company_id, foreign.identity_id.company_id
        )

    def test_opaque_namespace_cannot_claim_company_wide_resolution(self):
        event_values = self._event(
            self.connection_a,
            "opaque-person-id",
            company_scope=False,
        ).to_dict()
        opaque_address = {
            "namespace": "meta.psid",
            "value": "opaque-person-id",
            "value_normalized": "opaque-person-id",
            "role": "primary",
            "confidence": "protocol",
            "resolution_scope": "company",
        }
        event_values["actor"]["addresses"] = [opaque_address]
        event_values["conversation"]["addresses"] = [opaque_address]

        with self.assertRaisesRegex(
            ValidationError, "cannot resolve an identity across inboxes"
        ):
            self._process(self.connection_a, EventDTO.from_dict(event_values))

    def test_alias_rules_keep_participants_scoped_to_their_inbox(self):
        rule_xmlids = (
            "rule_contact_center_identity_alias_company",
            "rule_contact_center_identity_alias_supervisor_company",
        )
        for xmlid in rule_xmlids:
            rule = self.env.ref("contact_center_base.%s" % xmlid)
            self.assertIn("account_id.default_team_id.agent_ids", rule.domain_force)
            self.assertIn(
                "identity_id.channel_binding_ids.channel_id.channel_member_ids",
                rule.domain_force,
            )
