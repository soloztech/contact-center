import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs


class TestRefreshSettings(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create(
            {"name": "Contact Center Refresh Settings"}
        )

    def test_settings_save_and_reopen_for_selected_company(self):
        values = {
            "contact_center_contact_cache_days": 7,
            "contact_center_group_cache_days": 7,
            "contact_center_health_interval_minutes": 60,
            "contact_center_profile_failure_hours": 24,
            "contact_center_roster_retry_minutes": 15,
        }
        settings = self.env["res.config.settings"].create(
            {"company_id": self.company.id}
        )
        self.assertEqual({name: settings[name] for name in values}, values)
        changed = dict(zip(values, (2, 3, 5, 4, 10)))
        with trap_jobs() as trap:
            settings.write(changed)
            trap.assert_jobs_count(0)
        reopened = self.env["res.config.settings"].create(
            {"company_id": self.company.id}
        )
        self.assertEqual({name: reopened[name] for name in changed}, changed)
        self.assertEqual({name: self.company[name] for name in changed}, changed)
        other = self.env["res.company"].create({"name": "Other Refresh Settings"})
        self.assertEqual({name: other[name] for name in values}, values)

    def test_company_and_related_settings_enforce_limits(self):
        bounds = (
            ("contact_center_contact_cache_days", 365),
            ("contact_center_group_cache_days", 365),
            ("contact_center_health_interval_minutes", 60),
            ("contact_center_profile_failure_hours", 168),
            ("contact_center_roster_retry_minutes", 1440),
        )
        settings = self.env["res.config.settings"].create(
            {"company_id": self.company.id}
        )
        for name, maximum in bounds:
            with self.subTest(field=name):
                self.company.write({name: maximum})
                self.assertEqual(self.company[name], maximum)
                for invalid in (0, -1, maximum + 1):
                    with self.assertRaises(ValidationError), self.env.cr.savepoint():
                        self.company.write({name: invalid})
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    settings.write({name: 0})

    def test_agent_cannot_change_refresh_settings(self):
        agent = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Refresh Settings Agent",
                    "login": "cc-refresh-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).ids,
                        )
                    ],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(agent).create(
                {"contact_center_contact_cache_days": 1}
            )
