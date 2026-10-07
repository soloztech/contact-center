"""Explicit, per-inbox CRM intake policy. No implicit historical activation."""

import datetime

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

INTAKE_TOKEN = object()
ADMIN = "contact_center_base.group_contact_center_admin"
POLICY_FIELDS = {"crm_intake_enabled", "crm_intake_user_id", "crm_intake_team_id"}
POLICY_AUDIT = {
    "crm_intake_revision",
    "crm_intake_enabled_at",
    "crm_intake_binding_watermark",
}
GATE_NAMESPACE = 1135593801


def require_intake_admin(env):
    if not env.su and not env.user.has_group(ADMIN):
        raise AccessError(
            _("Only a Contact Center administrator can configure intake.")
        )


def automation_guards_available(env):
    return all(
        name not in env.registry
        or getattr(env[name], "_contact_center_intake_guard", None) == 1
        for name in ("base.automation", "automation.configuration")
    )


class ContactCenterCrmIntakeGate(models.Model):
    _name = "contact.center.crm.intake.gate"
    _description = "Private CRM Intake Company Fence"

    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    _sql_constraints = [
        ("company_unique", "unique(company_id)", "One intake gate per company.")
    ]

    @api.model_create_multi
    def create(self, vals_list):
        self._check_intake_service()
        return super().create(vals_list)

    def write(self, values):
        self._check_intake_service()
        return super().write(values)

    def unlink(self):
        self._check_intake_service()
        return super().unlink()

    def _check_intake_service(self):
        if self.env.context.get("crm_intake_service") is not INTAKE_TOKEN:
            raise AccessError(_("Intake fences are managed internally."))

    @api.model
    def _ensure_company(self, company):
        service = self.sudo().with_context(crm_intake_service=INTAKE_TOKEN)
        gate = service.search([("company_id", "=", company.id)], limit=1)
        if not gate:
            try:
                with self.env.cr.savepoint():
                    gate = service.create({"company_id": company.id})
            except psycopg2.IntegrityError as error:
                raise ValidationError(
                    _("Another intake policy changed. Please retry.")
                ) from error
        return gate


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    crm_intake_enabled = fields.Boolean(
        string="Entrada automática no CRM",
        groups=ADMIN,
        copy=False,
        help="Somente novas conversas diretas no WhatsApp. Histórico não gera leads.",
    )
    crm_intake_user_id = fields.Many2one(
        "res.users",
        string="Executor da entrada CRM",
        groups=ADMIN,
        domain=[("share", "=", False)],
        copy=False,
    )
    crm_intake_team_id = fields.Many2one(
        "crm.team",
        string="Equipe CRM para novos leads",
        groups=ADMIN,
        copy=False,
    )
    crm_intake_revision = fields.Integer(
        default=0, readonly=True, copy=False, groups=ADMIN
    )
    crm_intake_enabled_at = fields.Datetime(
        string="Ativada a partir de", readonly=True, copy=False, groups=ADMIN
    )
    crm_intake_binding_watermark = fields.Integer(
        readonly=True, copy=False, groups=ADMIN
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(POLICY_AUDIT.intersection(v) for v in vals_list) or any(
            "default_" + key in self.env.context for key in POLICY_AUDIT
        ):
            raise AccessError(_("The intake policy audit is managed internally."))
        if any(POLICY_FIELDS.intersection(v) for v in vals_list) or any(
            "default_" + key in self.env.context for key in POLICY_FIELDS
        ):
            require_intake_admin(self.env)
        accounts = super().create(vals_list)
        for account in accounts.filtered(lambda a: a.sudo().crm_intake_enabled):
            account._crm_intake_check_policy()
            account._crm_intake_revise()
        return accounts

    def write(self, values):
        if POLICY_AUDIT.intersection(values):
            raise AccessError(_("The intake policy audit is managed internally."))
        if POLICY_FIELDS.intersection(values):
            require_intake_admin(self.env)
        relevant = POLICY_FIELDS | {"company_id", "platform", "active"}
        if not relevant.intersection(values):
            return super().write(values)
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [self.ids],
        )
        self.invalidate_recordset()
        changed = self.sudo().filtered(
            lambda a: any(
                a[k] != v
                for k, v in values.items()
                if k in relevant
                and k not in {"crm_intake_user_id", "crm_intake_team_id", "company_id"}
            )
            or any(
                a[k].id != (v or False)
                for k, v in values.items()
                if k in {"crm_intake_user_id", "crm_intake_team_id", "company_id"}
            )
        )
        result = super().write(values)
        for account in changed:
            if not account.active and account.crm_intake_enabled:
                # Supervisors may archive inboxes without access to intake
                # settings. Archiving invalidates intake; unarchive is opt-in.
                super(ContactCenterAccount, account).write(
                    {"crm_intake_enabled": False}
                )
            if account.sudo().crm_intake_enabled:
                account._crm_intake_check_policy()
            account._crm_intake_revise()
        return result

    def _crm_intake_valid_user(self, user):
        self.ensure_one()
        account = self.sudo()
        return bool(
            user
            and user.id != SUPERUSER_ID
            and user.active
            and not user.share
            and account.company_id in user.company_ids
            and user in account._contact_center_effective_users()
            and user.has_group("contact_center_base.group_contact_center_agent")
            and self.env["crm.lead"]
            .with_user(user)
            .check_access_rights("create", raise_exception=False)
        )

    def _crm_intake_check_policy(self):
        for account in self.sudo():
            if not (
                account.active
                and account.platform == "whatsapp"
                and account._crm_intake_valid_user(account.crm_intake_user_id)
                and (
                    not account.crm_intake_team_id
                    or account.crm_intake_team_id.company_id == account.company_id
                )
                and automation_guards_available(self.env)
            ):
                raise ValidationError(
                    _(
                        "Choose an active WhatsApp inbox, an authorized CRM executor "
                        "and a team from the same company. Every installed automation "
                        "engine must support protected CRM intake."
                    )
                )

    def _crm_intake_revise(self):
        self.ensure_one()
        account = self.sudo()
        values = {"crm_intake_revision": account.crm_intake_revision + 1}
        if account.crm_intake_enabled:
            self.env["contact.center.crm.intake.gate"]._ensure_company(
                account.company_id
            )
            newest = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .with_context(active_test=False)
                .search([("account_id", "=", account.id)], order="id desc", limit=1)
            )
            values.update(
                # Provider dates have second precision. Start conservatively
                # at the next whole second, excluding queued pre-activation
                # webhooks even if they arrived in the activation second.
                crm_intake_enabled_at=fields.Datetime.now()
                + datetime.timedelta(seconds=1),
                crm_intake_binding_watermark=newest.id,
            )
        # Internal audit columns share the actual account row with the policy.
        return super(ContactCenterAccount, account).write(values)
