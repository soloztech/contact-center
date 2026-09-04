import threading
import uuid
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase

from ..models.identity import _IDENTITY_LINK_TOKEN


class TestIdentityPartnerInvariant(SavepointCase):
    def _identity(self, name):
        guest = self.env["mail.guest"].sudo().create({"name": name})
        return (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": name,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )

    def _person(self, name, company_id=None):
        return self.env["res.partner"].create(
            {
                "name": name,
                "company_id": company_id,
                "company_type": "person",
                "type": "contact",
            }
        )

    def _central_company(self, name, company_id=None):
        return self.env["res.partner"].create(
            {
                "name": name,
                "company_id": company_id,
                "is_company": True,
                "type": "contact",
            }
        )

    def test_person_link_allows_maintenance_and_rejects_invalid_structure(self):
        identity = self._identity("Personal identity")
        person = self._person("Personal contact", self.env.company.id)
        identity.action_link_partner(person.id)

        person.write(
            {
                "name": "Maintained personal contact",
                "phone": "+5511999999999",
                "street": "Normal address maintenance",
            }
        )
        self.assertEqual(person.name, "Maintained personal contact")
        self.assertEqual(person.phone, "+5511999999999")
        self.assertEqual(person.street, "Normal address maintenance")
        person.write({"company_id": False})
        person.write({"company_id": self.env.company.id})

        other_company = self.env["res.company"].create(
            {"name": "Foreign identity invariant scope"}
        )
        invalid_changes = (
            {"active": False},
            {"company_type": "company"},
            {"is_company": True},
            {"type": "delivery"},
            {"company_id": other_company.id},
        )
        for values in invalid_changes:
            with self.assertRaises(ValidationError):
                person.write(values)

        person.invalidate_recordset(["active", "company_id", "is_company", "type"])
        self.assertTrue(person.active)
        self.assertFalse(person.is_company)
        self.assertEqual(person.type, "contact")
        self.assertEqual(person.company_id, self.env.company)

    def test_central_link_allows_maintenance_and_rejects_invalid_structure(self):
        identity = self._identity("Central identity")
        company = self._central_company("Central contact", self.env.company.id)
        identity.action_link_central_company(company.id)

        company.write(
            {
                "name": "Maintained central contact",
                "phone": "+5511888888888",
                "street": "Normal central address maintenance",
            }
        )
        self.assertEqual(company.name, "Maintained central contact")
        company.write({"company_id": False})
        company.write({"company_id": self.env.company.id})

        other_company = self.env["res.company"].create(
            {"name": "Foreign central invariant scope"}
        )
        invalid_changes = (
            {"active": False},
            {"company_type": "person"},
            {"is_company": False},
            {"type": "invoice"},
            {"company_id": other_company.id},
        )
        for values in invalid_changes:
            with self.assertRaises(ValidationError):
                company.write(values)

        company.invalidate_recordset(["active", "company_id", "is_company", "type"])
        self.assertTrue(company.active)
        self.assertTrue(company.is_company)
        self.assertEqual(company.type, "contact")
        self.assertEqual(company.company_id, self.env.company)

    def test_portal_partner_is_allowed_but_internal_user_partner_is_not(self):
        portal_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Identity portal contact",
                    "login": "cc-identity-portal-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_portal").ids)],
                }
            )
        )
        portal_identity = self._identity("Portal identity")
        portal_identity.action_link_partner(portal_user.partner_id.id)
        self.assertEqual(portal_identity.partner_id, portal_user.partner_id)

        internal_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Identity internal user",
                    "login": "cc-identity-internal-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )
        with self.assertRaises(ValidationError):
            self._identity("Internal identity").action_link_partner(
                internal_user.partner_id.id
            )

    def test_portal_promotion_to_internal_rolls_back_under_canonical_locks(self):
        portal_user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Identity promoted portal",
                    "login": "cc-identity-promoted-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.env.ref("base.group_portal").ids)],
                }
            )
        )
        identity = self._identity("Promoted portal identity")
        identity.action_link_partner(portal_user.partner_id.id)
        observed = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized:
                if "from res_partner" in normalized:
                    observed.append("partner")
                elif "from contact_center_identity" in normalized:
                    observed.append("identity")
            return original_execute(cursor, query, params, *args, **kwargs)

        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                portal_user.write(
                    {"groups_id": [(6, 0, self.env.ref("base.group_user").ids)]}
                )
        self.assertEqual(observed[:2], ["partner", "identity"])
        self.env.invalidate_all()
        portal_user.invalidate_recordset(["groups_id", "share"])
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        self.assertTrue(portal_user.share)
        self.assertEqual(identity.partner_id, portal_user.partner_id)
        self.assertEqual(identity.partner_link_kind, "person")

        observed.clear()
        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            portal_user.write({"name": "Normal portal name maintenance"})
        self.assertFalse(observed)
        self.assertEqual(portal_user.name, "Normal portal name maintenance")

    def test_link_and_unlink_lock_partner_before_identity(self):
        identity = self._identity("Lock-order identity")
        person = self._person("Lock-order person", self.env.company.id)
        observed = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized:
                if "from res_partner" in normalized:
                    observed.append("partner")
                elif "from contact_center_identity" in normalized:
                    observed.append("identity")
            return original_execute(cursor, query, params, *args, **kwargs)

        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            identity.action_link_partner(person.id)
        self.assertEqual(observed[:2], ["partner", "identity"])

        observed.clear()
        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            identity.action_unlink_partner(person.id)
        self.assertEqual(observed[:2], ["partner", "identity"])

    def test_merge_runtime_stays_one_hop_and_rejects_chain_target(self):
        first = self._identity("Merge identity")
        second = self._identity("Merge identity")
        third = self._identity("Third merge identity")

        survivor, blocker = first._contact_center_merge_portable_component(
            first | second
        )
        self.assertFalse(blocker)
        retired = (first | second) - survivor
        self.assertEqual(retired.state, "merged")
        self.assertEqual(retired.merged_into_id, survivor)
        self.assertEqual(survivor.state, "active")
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            third.with_context(
                contact_center_identity_link_token=_IDENTITY_LINK_TOKEN
            ).write({"state": "merged", "merged_into_id": retired.id})
        third.invalidate_recordset(["merged_into_id", "state"])
        self.assertEqual(third.state, "active")
        self.assertFalse(third.merged_into_id)

    def test_native_partner_merge_rebinds_linked_identity_to_compatible_person(self):
        identity = self._identity("Native contact merge identity")
        destination_identity = self._identity("Native destination identity")
        source = self._person("Native merge source", self.env.company.id)
        destination = self._person("Native merge destination", self.env.company.id)
        identity.action_link_partner(source.id)
        destination_identity.action_link_partner(destination.id)
        observed = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if "for update" in normalized:
                if "from res_partner" in normalized:
                    observed.append("partner")
                elif "from contact_center_identity" in normalized:
                    observed.append("identity")
            return original_execute(cursor, query, params, *args, **kwargs)

        with mock.patch.object(cursor_class, "execute", new=tracked_execute):
            self.env["base.partner.merge.automatic.wizard"]._merge(
                [source.id, destination.id],
                dst_partner=destination,
            )

        self.assertEqual(observed[:2], ["partner", "identity"])
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        self.assertFalse(source.exists())
        self.assertEqual(identity.partner_id, destination)
        self.assertEqual(identity.partner_link_kind, "person")
        destination_identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        self.assertEqual(destination_identity.partner_id, destination)
        self.assertEqual(destination_identity.partner_link_kind, "person")

    def test_native_partner_merge_rejects_incompatible_destination_atomically(self):
        identity = self._identity("Rejected native contact merge")
        source = self._person("Rejected native merge source", self.env.company.id)
        destination = self._central_company(
            "Rejected native merge company", self.env.company.id
        )
        identity.action_link_partner(source.id)

        with self.assertRaisesRegex(ValidationError, "active person contact"):
            with self.env.cr.savepoint():
                self.env["base.partner.merge.automatic.wizard"]._merge(
                    [source.id, destination.id],
                    dst_partner=destination,
                )

        self.env.invalidate_all()
        self.assertTrue(source.exists())
        self.assertTrue(destination.exists())
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        self.assertEqual(identity.partner_id, source)
        self.assertEqual(identity.partner_link_kind, "person")

    def test_native_partner_merge_rejects_foreign_company_atomically(self):
        identity = self._identity("Rejected foreign-company merge")
        source = self._person("Foreign-company merge source", self.env.company.id)
        other_company = self.env["res.company"].create(
            {"name": "Foreign-company merge scope"}
        )
        destination = self._person(
            "Foreign-company merge destination", other_company.id
        )
        identity.action_link_partner(source.id)

        with self.assertRaisesRegex(ValidationError, "belongs to another company"):
            with self.env.cr.savepoint():
                self.env["base.partner.merge.automatic.wizard"]._merge(
                    [source.id, destination.id],
                    dst_partner=destination,
                )

        self.env.invalidate_all()
        self.assertTrue(source.exists())
        self.assertTrue(destination.exists())
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        self.assertEqual(identity.partner_id, source)
        self.assertEqual(identity.partner_link_kind, "person")


@tagged("-at_install", "post_install")
class TestIdentityPartnerInvariantConcurrency(TransactionCase):
    WORKER_TIMEOUT_SECONDS = 12

    def _setup_committed_fixture(self, token, link_kind):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            guest = (
                env["mail.guest"]
                .sudo()
                .create({"name": "Identity concurrency %s" % token})
            )
            identity = (
                env["contact.center.identity"]
                .sudo()
                .create(
                    {
                        "name": guest.name,
                        "company_id": env.company.id,
                        "mail_guest_id": guest.id,
                    }
                )
            )
            partner = env["res.partner"].create(
                {
                    "name": "Identity concurrency partner %s" % token,
                    "company_id": env.company.id,
                    "is_company": link_kind == "central_company",
                    "type": "contact",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "guest_id": guest.id,
                "identity_id": identity.id,
                "link_kind": link_kind,
                "partner_id": partner.id,
            }

    def _retry_mutation(self, fixture, mutation):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if mutation == "link":
                    identity = env["contact.center.identity"].browse(
                        fixture["identity_id"]
                    )
                    if fixture["link_kind"] == "person":
                        identity.action_link_partner(fixture["partner_id"])
                    else:
                        identity.action_link_central_company(fixture["partner_id"])
                else:
                    env["res.partner"].browse(fixture["partner_id"]).write(
                        {"is_company": fixture["link_kind"] == "person"}
                    )
                cr.commit()  # pylint: disable=invalid-commit
                return "committed"
            except ValidationError:
                cr.rollback()
                return "validation"
            except SerializationFailure:
                cr.rollback()
                return "serialization"

    def _setup_committed_merge_fixture(self):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex, "person")
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            destination = env["res.partner"].create(
                {
                    "name": "Identity merge race destination",
                    "company_id": env.company.id,
                    "company_type": "person",
                    "type": "contact",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            fixture["destination_partner_id"] = destination.id
        return fixture

    def _run_merge_race_operation(self, fixture, operation):
        for _attempt in range(3):
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    if operation == "merge":
                        partners = (
                            env["res.partner"]
                            .sudo()
                            .with_context(active_test=False)
                            .browse(
                                [
                                    fixture["partner_id"],
                                    fixture["destination_partner_id"],
                                ]
                            )
                            .exists()
                        )
                        if len(partners) > 1:
                            destination = partners.filtered(
                                lambda partner: partner.id
                                == fixture["destination_partner_id"]
                            )
                            env["base.partner.merge.automatic.wizard"]._merge(
                                partners.ids,
                                dst_partner=destination,
                            )
                    else:
                        env["contact.center.identity"].browse(
                            fixture["identity_id"]
                        ).action_link_partner(fixture["partner_id"])
                    cr.commit()  # pylint: disable=invalid-commit
                    return "committed"
                except ValidationError:
                    cr.rollback()
                    return "validation"
                except SerializationFailure:
                    cr.rollback()
        return "serialization"

    def _concurrent_merge_race(self, fixture, operation, barrier, results):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            cr.execute(
                "SELECT id FROM res_partner WHERE id = ANY(%s) ORDER BY id",
                [
                    [
                        fixture["partner_id"],
                        fixture["destination_partner_id"],
                    ]
                ],
            )
            cr.execute(
                "SELECT id FROM contact_center_identity WHERE id = %s",
                [fixture["identity_id"]],
            )
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if operation == "merge":
                    partners = env["res.partner"].browse(
                        [
                            fixture["partner_id"],
                            fixture["destination_partner_id"],
                        ]
                    )
                    destination = partners.filtered(
                        lambda partner: partner.id == fixture["destination_partner_id"]
                    )
                    env["base.partner.merge.automatic.wizard"]._merge(
                        partners.ids,
                        dst_partner=destination,
                    )
                else:
                    env["contact.center.identity"].browse(
                        fixture["identity_id"]
                    ).action_link_partner(fixture["partner_id"])
                cr.commit()  # pylint: disable=invalid-commit
                results[operation] = "committed"
            except ValidationError:
                cr.rollback()
                results[operation] = "validation"
            except SerializationFailure:
                cr.rollback()
                results[operation] = self._run_merge_race_operation(fixture, operation)

    def _concurrent_mutation(self, fixture, mutation, barrier, results):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            # Pin both workers to the same REPEATABLE READ snapshot before
            # either side enters the partner -> identity protocol.
            cr.execute(
                "SELECT id FROM contact_center_identity WHERE id = %s",
                [fixture["identity_id"]],
            )
            cr.execute(
                "SELECT id FROM res_partner WHERE id = %s", [fixture["partner_id"]]
            )
            barrier.wait(timeout=self.WORKER_TIMEOUT_SECONDS)
            env = api.Environment(cr, SUPERUSER_ID, {})
            try:
                if mutation == "link":
                    identity = env["contact.center.identity"].browse(
                        fixture["identity_id"]
                    )
                    if fixture["link_kind"] == "person":
                        identity.action_link_partner(fixture["partner_id"])
                    else:
                        identity.action_link_central_company(fixture["partner_id"])
                else:
                    env["res.partner"].browse(fixture["partner_id"]).write(
                        {"is_company": fixture["link_kind"] == "person"}
                    )
                cr.commit()  # pylint: disable=invalid-commit
                results.append((mutation, "committed"))
            except ValidationError:
                cr.rollback()
                results.append((mutation, "validation"))
            except SerializationFailure:
                cr.rollback()
                results.append((mutation, self._retry_mutation(fixture, mutation)))

    def _cleanup_committed_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.identity"].sudo().browse(
                fixture["identity_id"]
            ).unlink()
            env["res.partner"].sudo().with_context(active_test=False).browse(
                [
                    partner_id
                    for partner_id in (
                        fixture["partner_id"],
                        fixture.get("destination_partner_id"),
                    )
                    if partner_id
                ]
            ).unlink()
            env["mail.guest"].sudo().browse(fixture["guest_id"]).unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def _assert_link_race_converges(self, link_kind):
        fixture = self._setup_committed_fixture(uuid.uuid4().hex, link_kind)
        barrier = threading.Barrier(2)
        results = []
        workers = [
            threading.Thread(
                target=self._concurrent_mutation,
                args=(fixture, mutation, barrier, results),
            )
            for mutation in ("link", "invalidate")
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(len(results), 2)
            self.assertEqual(
                sorted(state for _mutation, state in results),
                ["committed", "validation"],
            )
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                identity = env["contact.center.identity"].browse(fixture["identity_id"])
                partner = env["res.partner"].browse(fixture["partner_id"])
                if identity.partner_id:
                    self.assertEqual(identity.partner_id, partner)
                    self.assertEqual(identity.partner_link_kind, link_kind)
                    self.assertEqual(partner.is_company, link_kind == "central_company")
        finally:
            self._cleanup_committed_fixture(fixture)

    def test_person_link_and_structural_write_converge(self):
        self._assert_link_race_converges("person")

    def test_central_link_and_structural_write_converge(self):
        self._assert_link_race_converges("central_company")

    def test_native_merge_and_identity_link_converge_without_dangling_partner(self):
        fixture = self._setup_committed_merge_fixture()
        barrier = threading.Barrier(2)
        results = {}
        workers = [
            threading.Thread(
                target=self._concurrent_merge_race,
                args=(fixture, operation, barrier, results),
            )
            for operation in ("merge", "link")
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(self.WORKER_TIMEOUT_SECONDS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(results.get("merge"), "committed", results)
            self.assertIn(results.get("link"), ("committed", "validation"), results)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                source = (
                    env["res.partner"]
                    .with_context(active_test=False)
                    .browse(fixture["partner_id"])
                    .exists()
                )
                destination = (
                    env["res.partner"]
                    .with_context(active_test=False)
                    .browse(fixture["destination_partner_id"])
                    .exists()
                )
                identity = env["contact.center.identity"].browse(fixture["identity_id"])
                self.assertFalse(source)
                self.assertTrue(destination)
                if identity.partner_id:
                    self.assertEqual(identity.partner_id, destination)
                    self.assertEqual(identity.partner_link_kind, "person")
        finally:
            self._cleanup_committed_fixture(fixture)
