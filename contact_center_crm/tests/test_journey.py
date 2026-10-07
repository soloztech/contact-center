import datetime
import runpy
from pathlib import Path

from odoo.api import call_kw
from odoo.exceptions import AccessError, ValidationError

from .test_conversation_crm import ConversationCrmCase


class TestBusinessJourney(ConversationCrmCase):
    def _context(self, lead=None, channel=None, writer="manual"):
        return (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.agent)
            ._link(
                (channel or self.channel).with_user(self.agent),
                (lead or self.lead).with_user(self.agent),
                writer=writer,
            )
        )

    def _confirm(self, row, start="2026-09-01 12:00:00", end=False):
        return row.with_env(self.env).with_user(self.agent)._confirm_scope(start, end)

    def test_scope_is_explicit_and_successors_keep_provenance(self):
        row = self._context(writer="automation")
        self.assertEqual((row.scope_state, row.writer), ("context", "automation"))
        confirmed = self._confirm(row)
        self.assertNotEqual(confirmed.id, row.id)
        self.assertEqual(row.unlinked_reason, "scope_revised")
        self.assertEqual(
            (confirmed.scope_state, confirmed.writer), ("confirmed", "automation")
        )
        self.assertEqual(confirmed.scope_actor_id, self.agent)
        self.assertEqual(confirmed.origin, row.origin)
        self.assertEqual(self._confirm(confirmed), confirmed)
        revised = self._confirm(confirmed, "2026-09-02 12:00:00")
        self.assertEqual(confirmed.scope_start, datetime.datetime(2026, 9, 1, 12))
        self.assertEqual(revised.scope_start, datetime.datetime(2026, 9, 2, 12))
        with self.assertRaises(TypeError):
            self.env["contact.center.crm.conversation.link"]._link(
                self.channel, self.lead
            )

    def test_half_open_window_and_uniform_overlap(self):
        first = self._confirm(self._context(), end="2026-09-02 12:00:00")
        self.assertTrue(first._scope_contains("2026-09-01 12:00:00"))
        self.assertFalse(first._scope_contains("2026-09-02 12:00:00"))
        second = self._context(self.company_lead)
        with self.assertRaisesRegex(ValidationError, "overlaps another business"):
            self._confirm(second, "2026-09-02 11:59:59")
        self._confirm(second, "2026-09-02 12:00:00")
        with self.assertRaises(ValidationError):
            self._confirm(first, "2026-09-03 12:00:00", "2026-09-03 12:00:00")

    def test_journey_splits_customer_context_and_restricted_existence(self):
        row = self._context()
        hidden = self._customer_channel_for(self.other)
        hidden_row = self.env["contact.center.crm.conversation.link"]._link(
            hidden, self.lead, writer="manual"
        )
        # Cache warmed in sudo must not expose IDs, person, account or history.
        hidden.sudo().read(["name"])
        hidden_row.sudo().read(["channel_id"])
        lead = self.lead.with_user(self.agent)
        linked = lead.get_contact_center_journey()
        self.assertEqual(linked["total"], 2)
        restricted = [r for r in linked["items"] if not r["can_open"]]
        self.assertEqual(restricted, [{"can_open": False, "restricted": True}])
        self.assertEqual(linked["items"][0]["scope"], "context")
        another = self._channel(self.account, self.sibling)
        context = lead.get_contact_center_journey("context")
        self.assertIn(another.id, [r.get("channel_id") for r in context["items"]])
        self.assertNotIn(
            row.channel_id.id, [r.get("channel_id") for r in context["items"]]
        )
        with self.assertRaises(AccessError):
            lead.open_contact_center_journey_conversation(hidden.id)
        self.assertEqual(
            lead.get_contact_center_journey_history(self.channel.id)["status"],
            "restricted",
        )
        self.agent.groups_id |= self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        self.assertEqual(
            lead.get_contact_center_journey_history(self.channel.id)["status"], "ready"
        )

    def test_linked_actions_do_not_require_readable_customer(self):
        self._context()
        private = self.env["res.partner"].create(
            {"name": "Private lead customer", "type": "private"}
        )
        self.lead.partner_id = private
        self.assertFalse(self.agent.has_group("base.group_private_addresses"))
        with self.assertRaises(AccessError):
            private.with_user(self.agent).read(["name"])
        lead = self.lead.with_user(self.agent)
        self.assertEqual(lead.get_contact_center_journey()["total"], 1)
        self.assertEqual(
            lead.open_contact_center_journey_conversation(self.channel.id)["item"][
                "channel_id"
            ],
            self.channel.id,
        )
        self.assertEqual(
            lead.get_contact_center_journey_history(self.channel.id)["status"],
            "restricted",
        )
        self.assertEqual(
            lead.action_contact_center_scope(self.channel.id)["context"][
                "default_channel_id"
            ],
            self.channel.id,
        )
        self.assertEqual(
            lead.get_contact_center_journey("context")["status"], "unavailable"
        )
        unrelated = self._channel(self.account, self.sibling)
        for method in (
            lead.open_contact_center_journey_conversation,
            lead.get_contact_center_journey_history,
            lead.action_contact_center_scope,
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(
                    AccessError,
                    r"^The conversation is not available for this business\.$",
                ):
                    method(unrelated.id)

    def test_missing_customer_and_invalid_pagination_are_explicit(self):
        lead = self._lead("No customer", self.env["res.partner"])
        self.assertEqual(
            lead.with_user(self.agent).get_contact_center_journey("context")["status"],
            "unavailable",
        )
        for kwargs in (
            {"offset": True},
            {"limit": False},
            {"offset": -1},
            {"area": "all"},
            {"area": []},
            {"area": {}},
        ):
            with self.assertRaises(ValidationError):
                lead.with_user(self.agent).get_contact_center_journey(**kwargs)

    def test_scope_wizard_cannot_forge_association(self):
        row = self._context()
        action = self.lead.with_user(self.agent).action_contact_center_scope(
            self.channel.id
        )
        self.assertEqual(action["views"], [(False, "form")])
        with self.assertRaises(ValidationError):
            self.env["contact.center.crm.scope"].with_user(self.agent).create(
                {
                    "lead_id": self.company_lead.id,
                    "channel_id": self.channel.id,
                    "scope_start": "2026-09-01 00:00:00",
                }
            )
        self.assertEqual(row.scope_state, "context")

    def test_crm_wizards_are_private_to_creator_and_confirmation_removes_row(self):
        self._context()
        scope = (
            self.env["contact.center.crm.scope"]
            .with_user(self.agent)
            .create(
                {
                    "lead_id": self.lead.id,
                    "channel_id": self.channel.id,
                    "scope_start": "2026-09-01 12:00:00",
                }
            )
        )
        start = (
            self.env["contact.center.crm.start"]
            .with_user(self.agent)
            .create(
                {
                    "lead_id": self.lead.id,
                    "account_id": self.account.id,
                    "phone": "+5511998765432",
                }
            )
        )
        for wizard, names in (
            (scope, ["lead_id", "channel_id", "scope_start"]),
            (start, ["lead_id", "account_id", "phone"]),
        ):
            with self.subTest(model=wizard._name):
                self.assertEqual(len(wizard.read(names)), 1)
                other = wizard.with_user(self.other)
                self.assertFalse(other.search_read([("id", "=", wizard.id)], names))
                with self.assertRaises(AccessError):
                    other.read(names)
                with self.assertRaises(AccessError):
                    other.unlink()
                with self.assertRaises(AccessError):
                    wizard.write({"lead_id": self.company_lead.id})
        hidden = self._customer_channel_for(self.other)
        with self.assertRaises(AccessError):
            scope.copy({"channel_id": hidden.id})
        with self.assertRaises(AccessError):
            start.write(
                {
                    "account_id": self.env["contact.center.channel.binding"]
                    .search([("channel_id", "=", hidden.id)], limit=1)
                    .account_id.id
                }
            )
        self.company_lead.user_id = self.other
        with self.assertRaises(AccessError):
            scope.copy({"lead_id": self.company_lead.id})
        with self.assertRaises(AccessError):
            start.copy({"lead_id": self.company_lead.id})
        scope.action_confirm()
        self.assertFalse(scope.exists())
        self.assertEqual(self.lead._conversation_links().scope_state, "confirmed")

    def test_wizard_onchange_authorizes_references_before_serializing_names(self):
        self._context()
        hidden = self._customer_channel_for(self.other)
        hidden_account = (
            self.env["contact.center.channel.binding"]
            .search([("channel_id", "=", hidden.id)], limit=1)
            .account_id
        )
        self.company_lead.user_id = self.other
        cases = (
            ("contact.center.crm.scope", "channel_id", self.channel.id, hidden.id),
            (
                "contact.center.crm.start",
                "account_id",
                self.account.id,
                hidden_account.id,
            ),
        )
        for model_name, field, allowed_id, hidden_id in cases:
            with self.subTest(model=model_name):
                model = self.env[model_name].with_user(self.agent)
                spec = {"lead_id": "0", field: "0"}
                values = {"lead_id": self.lead.id, field: allowed_id}
                result = call_kw(model, "onchange", [[], values, [], spec], {})
                self.assertEqual(result["value"]["lead_id"][0], self.lead.id)
                self.assertEqual(result["value"][field][0], allowed_id)
                for forged in (
                    dict(values, **{field: hidden_id}),
                    dict(values, lead_id=self.company_lead.id),
                ):
                    with self.assertRaises(AccessError) as caught:
                        call_kw(model, "onchange", [[], forged, [], spec], {})
                    self.assertNotIn(self.company_lead.name, str(caught.exception))
                    self.assertNotIn(hidden_account.name, str(caught.exception))

    def test_merge_different_windows_preserves_history_and_survivor_provenance(self):
        first_lead = self._lead("First business", self.person)
        second_lead = self._lead("Second business", self.person)
        first = self._confirm(
            self._context(first_lead, writer="automation"), end="2026-09-02 12:00:00"
        )
        second = self._confirm(self._context(second_lead), start="2026-09-02 12:00:00")
        original = first | second
        provenance = {
            row.lead_id.id: (row.writer, row.origin, row.linked_at, row.linked_by_id)
            for row in original
        }
        windows = {row.id: (row.scope_start, row.scope_end) for row in original}
        survivor = (first_lead | second_lead)._merge_opportunity()
        original.invalidate_recordset()
        self.assertEqual(original.mapped("state"), ["unlinked", "unlinked"])
        self.assertEqual(original.mapped("unlinked_reason"), ["merged", "merged"])
        self.assertEqual(
            {row.id: (row.scope_start, row.scope_end) for row in original}, windows
        )
        review = survivor._conversation_links()
        self.assertEqual(len(review), 1)
        self.assertEqual(review.scope_state, "review")
        self.assertFalse(review.scope_start or review.scope_end)
        self.assertEqual(
            (review.writer, review.origin, review.linked_at, review.linked_by_id),
            provenance[survivor.id],
        )

    def test_duplicate_merge_keeps_legacy_survivor_provenance(self):
        source = self._context()
        duplicate = self._context(self.company_lead)
        duplicate._service().write({"scope_state": "legacy", "writer": "unknown"})
        (self.lead | self.company_lead)._contact_center_lock_conversation_graph(
            channel_ids=self.channel.ids, touch_leads=True, touch_channels=True
        )
        source._transfer_to_lead(self.company_lead)
        review = self.company_lead._conversation_links()
        self.assertEqual((review.scope_state, review.writer), ("review", "unknown"))
        self.assertEqual(review.linked_at, duplicate.linked_at)
        self.assertEqual((source | duplicate).mapped("state"), ["unlinked", "unlinked"])

    def test_transfer_without_duplicate_preserves_context_and_reviews_confirmation(
        self,
    ):
        for state in ("context", "legacy", "confirmed"):
            with self.subTest(scope=state):
                channel = self._channel(self.account, self.person)
                source = self._lead("Source " + state, self.person)
                target = self._lead("Target " + state, self.person)
                row = self._context(source, channel, writer="automation")
                if state == "confirmed":
                    row = self._confirm(row)
                elif state == "legacy":
                    row._service().write({"scope_state": "legacy", "writer": "unknown"})
                old_writer, old_start = row.writer, row.scope_start
                (source | target)._contact_center_lock_conversation_graph(
                    channel_ids=channel.ids, touch_leads=True, touch_channels=True
                )
                row._transfer_to_lead(target)
                active = target._conversation_links()
                self.assertEqual(len(active), 1)
                self.assertEqual(active.writer, old_writer)
                if state == "confirmed":
                    self.assertNotEqual(active.id, row.id)
                    self.assertEqual(active.scope_state, "review")
                    self.assertEqual(row.state, "unlinked")
                    self.assertEqual(row.unlinked_reason, "merged")
                    self.assertEqual(row.scope_start, old_start)
                else:
                    self.assertEqual(active.id, row.id)
                    self.assertEqual(active.scope_state, state)

    def test_native_conversion_preserves_confirmed_scope_and_business_identity(self):
        lead = self._lead("Convert this lead", self.person, type="lead")
        row = self._confirm(self._context(lead), end="2026-09-02 12:00:00")
        before = (lead.id, row.id, row.scope_start, row.scope_end, row.writer)
        lead.with_user(self.agent).convert_opportunity(self.person)
        self.assertEqual(lead.type, "opportunity")
        active = lead._conversation_links()
        self.assertEqual(
            (lead.id, active.id, active.scope_start, active.scope_end, active.writer),
            before,
        )
        self.assertEqual(active.scope_state, "confirmed")

    def test_salesman_without_agent_role_cannot_use_journey(self):
        salesman = self._user(
            "Sales without Contact Center",
            self.env.ref("sales_team.group_sale_salesman"),
        )
        lead = self._lead("Sales only", self.person, user_id=salesman.id)
        self.assertFalse(
            salesman.has_group("contact_center_base.group_contact_center_agent")
        )
        lead.with_user(salesman).check_access_rule("read")
        with self.assertRaises(AccessError):
            lead.with_user(salesman).action_contact_center_journey()
        with self.assertRaises(AccessError):
            lead.with_user(salesman).get_contact_center_journey()

    def test_administrator_still_needs_membership_and_active_company(self):
        self._context()
        admin = self._user(
            "Contact Center admin",
            self.env.ref("contact_center_base.group_contact_center_admin")
            | self.env.ref("sales_team.group_sale_salesman_all_leads"),
        )
        lead = self.lead.with_user(admin)
        self.assertEqual(
            lead.get_contact_center_journey()["items"],
            [{"can_open": False, "restricted": True}],
        )
        with self.assertRaises(AccessError):
            lead.get_contact_center_journey_history(self.channel.id)
        self.account.access_user_ids |= admin
        self.assertEqual(
            lead.get_contact_center_journey_history(self.channel.id)["status"], "ready"
        )
        other_company = self.env["res.company"].create({"name": "Admin other company"})
        admin.company_ids |= other_company
        with self.assertRaises(AccessError):
            lead.with_context(
                allowed_company_ids=other_company.ids
            ).get_contact_center_journey()

    def test_companyless_business_counts_only_links_in_active_companies(self):
        other_company = self.env["res.company"].create(
            {"name": "Journey other company"}
        )
        self.agent.company_ids |= other_company
        company_ids = self.env.company.ids + other_company.ids
        account = (
            self.env["contact.center.account"]
            .with_company(other_company)
            .create(
                {
                    "name": "Journey other company inbox",
                    "platform": "whatsapp",
                    "company_id": other_company.id,
                    "external_ref": "journey-other-company",
                    "access_user_ids": [(6, 0, self.agent.ids)],
                }
            )
        )
        guest = self.env["mail.guest"].create({"name": "Other company guest"})
        identity = (
            self.env["contact.center.identity"]
            .with_company(other_company)
            .create(
                {
                    "company_id": other_company.id,
                    "name": "Other identity",
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = (
            self.env["mail.channel"]
            .with_company(other_company)
            ._contact_center_create_channel(
                account=account, identity=identity, guest_ids=guest.ids
            )
        )
        self.env["contact.center.channel.binding"].with_company(other_company).create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "journey-other-company",
            }
        )
        lead = self._lead("Global business", self.person, company_id=False)
        model = (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.agent)
            .with_context(allowed_company_ids=company_ids)
        )
        for conversation in (self.channel, channel):
            model._link(
                conversation.with_env(model.env),
                lead.with_env(model.env),
                writer="manual",
            )
        for active_companies, expected in (
            (self.env.company.ids, self.channel.ids),
            (other_company.ids, channel.ids),
            (company_ids, (self.channel | channel).ids),
        ):
            with self.subTest(companies=active_companies):
                page = (
                    lead.with_user(self.agent)
                    .with_context(allowed_company_ids=active_companies)
                    .get_contact_center_journey()
                )
                self.assertEqual(page["total"], len(expected))
                self.assertEqual(
                    {item["channel_id"] for item in page["items"]}, set(expected)
                )

    def test_historical_backfill_aborts_before_any_write(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "release_migrations/crm_conversation_backfill.py"
        )
        if not script.is_file():
            self.skipTest("Repository release script is not part of this addon package")
        row = self._context()
        names = ["state", "writer", "scope_state", "write_date"]
        before_link = row.read(names)
        before_utm = self.lead.read(["campaign_id", "source_id", "medium_id"])
        before = (
            self.env["contact.center.crm.conversation.link"].sudo().search_count([])
        )
        extraction = runpy.run_path(str(script))
        for name in (
            "migrate",
            "_backfill_links",
            "_retire_legacy_jobs",
            "_replace_marketing_authority",
        ):
            with self.subTest(entry=name):
                with self.assertRaisesRegex(RuntimeError, "Historical extraction"):
                    extraction[name](self.env)
        self.assertEqual(
            self.env["contact.center.crm.conversation.link"].sudo().search_count([]),
            before,
        )
        self.assertEqual(row.scope_state, "context")
        self.assertEqual(row.read(names), before_link)
        self.assertEqual(
            self.lead.read(["campaign_id", "source_id", "medium_id"]), before_utm
        )

    def test_context_cannot_forge_writer_and_legacy_confirmation_keeps_unknown_origin(
        self,
    ):
        row = (
            self.env["contact.center.crm.conversation.link"]
            .with_user(self.agent)
            .with_context(
                writer="automation",
                default_writer="automation",
                default_scope_state="confirmed",
            )
            ._link(
                self.channel.with_user(self.agent),
                self.lead.with_user(self.agent),
                writer="manual",
            )
        )
        self.assertEqual((row.writer, row.scope_state), ("manual", "context"))
        same = self._context(writer="automation")
        self.assertEqual(same, row)
        self.assertEqual(same.writer, "manual")
        row._service().write({"scope_state": "legacy", "writer": "unknown"})
        confirmed = self._confirm(row)
        self.assertEqual(confirmed.writer, "unknown")
        self.assertEqual(confirmed.origin, row.origin)
        self.assertEqual(confirmed.scope_actor_id, self.agent)

    def test_unreadable_overlap_is_rejected_without_business_details(self):
        occupied = self._confirm(self._context(self.company_lead))
        self.company_lead.user_id = self.other
        self.env.flush_all()
        with self.assertRaises(AccessError):
            self.company_lead.with_user(self.agent).check_access_rule("read")
        with self.assertRaises(ValidationError) as caught:
            self._confirm(self._context())
        self.assertEqual(
            str(caught.exception),
            "This period overlaps another business. "
            "Ask an authorized CRM manager to review it.",
        )
        self.assertNotIn(self.company_lead.name, str(caught.exception))
        self.assertEqual(occupied.state, "active")

    def test_journey_revalidates_lead_customer_and_inbox(self):
        lead = self.lead.with_user(self.agent)
        self.assertTrue(lead.get_contact_center_journey("context")["items"])
        self.lead.partner_id = self.env["res.partner"].create({"name": "New customer"})
        with self.assertRaises(AccessError):
            lead.open_contact_center_journey_conversation(self.channel.id)
        self._context()
        self.account.access_user_ids = [(5, 0, 0)]
        with self.assertRaises(AccessError):
            lead.open_contact_center_journey_conversation(self.channel.id)
        self.lead.user_id = self.other
        with self.assertRaises(AccessError):
            lead.get_contact_center_journey()
        with self.assertRaises(AccessError):
            self.lead.with_user(self.non_sales).get_contact_center_journey()

    def test_companyless_lead_has_no_customer_context_or_confirmed_period(self):
        self.lead.company_id = False
        lead = self.lead.with_user(self.agent)
        self.assertEqual(
            lead.get_contact_center_journey("context")["status"], "unavailable"
        )
        row = self._context()
        with self.assertRaises(ValidationError):
            self._confirm(row)
