# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
"""Explicit, per-inbox CRM intake policy. No implicit historical activation."""

import datetime
import hashlib
import hmac
import secrets

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.phone import normalize_start_phone
from odoo.addons.queue_job.exception import RetryableJobError

INTAKE_TOKEN = object()
COMPANY_GATE_TOKEN = object()
ADMIN = "contact_center_base.group_contact_center_admin"
POLICY_FIELDS = {"crm_intake_enabled", "crm_intake_user_id", "crm_intake_team_id"}
POLICY_AUDIT = {
    "crm_intake_revision",
    "crm_intake_enabled_at",
    "crm_intake_binding_watermark",
}
GATE_NAMESPACE = 1135593801


def brazil_mobile_pair(phone):
    """Conservative comparison hint only; never establishes identity."""
    if not isinstance(phone, str) or not phone.startswith("55"):
        return False
    subscriber = phone[4:]
    if len(phone) == 12 and subscriber[:1] in "6789":
        other = phone[:4] + "9" + subscriber
        canonical = phone
    elif len(phone) == 13 and subscriber[:1] == "9" and subscriber[1:2] in "6789":
        other = phone[:4] + subscriber[1:]
        canonical = other
    else:
        return False
    try:
        if normalize_start_phone("+" + phone, "BR") != phone:
            return False
        if normalize_start_phone("+" + other, "BR") != other:
            return False
    except ValueError:
        return False
    return canonical


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
    comparison_secret = fields.Char(
        readonly=True,
        copy=False,
        groups=ADMIN,
    )
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
                raise RetryableJobError(
                    "CRM company fence bootstrap contention",
                    seconds=2,
                    ignore_retry=True,
                ) from error
        return gate

    @api.model
    def _acquire_company(self, company):
        """Common admission fence, before source configuration or claim locks."""
        gate = self._ensure_company(company)
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            [GATE_NAMESPACE, company.id],
        )
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError("CRM company busy", seconds=2, ignore_retry=True)
        gate.flush_recordset()
        self.env.cr.execute(
            "UPDATE contact_center_crm_intake_gate SET write_date=write_date WHERE id=%s",
            [gate.id],
        )
        gate.invalidate_recordset()
        return gate.with_context(
            crm_company_gate=COMPANY_GATE_TOKEN, crm_company_gate_company=company.id
        )

    @api.model
    def _acquire_company_ui(self, company):
        """Interactive callers receive an actionable retry instead of a job error."""
        try:
            return self._acquire_company(company)
        except RetryableJobError as error:
            raise ValidationError(
                _("Commercial intake is busy. Please retry in a few seconds.")
            ) from error

    def _comparison_keys(self, phone, country="BR"):
        self.ensure_one()
        if self.env.context.get("crm_company_gate") is not COMPANY_GATE_TOKEN:
            raise AccessError(_("Phone comparison requires the admission fence."))
        try:
            normalized = normalize_start_phone(phone, country)
        except ValueError:
            return {"exact": False, "variant": False, "version": 1}
        if not self.comparison_secret:
            self.sudo().with_context(crm_intake_service=INTAKE_TOKEN).write(
                {"comparison_secret": secrets.token_hex(32)}
            )

        def digest(kind, value):
            if not value:
                return False
            material = "%s/1/%s/%s" % (self.company_id.id, kind, value)
            return hmac.new(
                bytes.fromhex(self.comparison_secret), material.encode(), hashlib.sha256
            ).hexdigest()

        return {
            "exact": digest("phone", normalized),
            "variant": digest("br-mobile-pair", brazil_mobile_pair(normalized)),
            "version": 1,
        }

    def _phone_candidates(self, phone, country="BR", *, review_only=False):
        """Private, complete-number probe shared by both admission adapters.

        Include closed and company-less records so neither becomes a false
        zero. The caller still has to prove business scope and native rights.
        A structural BR pair is always a review hint, never an identity alias.
        """
        self.ensure_one()
        if (
            not review_only
            and self.env.context.get("crm_company_gate") is not COMPANY_GATE_TOKEN
        ):
            raise AccessError(_("Candidate search requires the admission fence."))
        Lead = self.env["crm.lead"].sudo().with_context(active_test=False)
        try:
            normalized = normalize_start_phone(phone, country)
        except ValueError:
            return Lead.browse(), Lead.browse()
        pair = brazil_mobile_pair(normalized)
        alternate = False
        if pair:
            alternate = pair if normalized != pair else pair[:4] + "9" + pair[4:]

        def find(numbers):
            return Lead.search(
                [
                    ("company_id", "in", [False, self.company_id.id]),
                    "|",
                    ("contact_center_phone_normalized", "in", numbers),
                    ("contact_center_mobile_normalized", "in", numbers),
                ],
                order="id",
                limit=3,
            )

        exact = find([normalized])
        weak = find([alternate]) - exact if alternate else Lead.browse()
        return exact, weak


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
