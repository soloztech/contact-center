import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.tokens import (
    CONTACT_CENTER_ATTRIBUTION_TOKEN,
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
)

# These inherited extensions deliberately keep identity invariants beside the
# identity aggregate instead of coupling them to the already independent
# channel/account aggregates that also extend the same Odoo core models.
# pylint: disable=consider-merging-classes-inherited

_IDENTITY_LINK_TOKEN = object()
_IDENTITY_NAME_TOKEN = object()
_MANAGED_NAME_SOURCES = ("fallback", "provider")
_LINKED_PARTNER_STRUCTURAL_FIELDS = frozenset(
    {"active", "company_id", "company_type", "is_company", "type"}
)


class ContactCenterIdentity(models.Model):
    _name = "contact.center.identity"
    _description = "Contact Center Identity"
    _order = "name, id"
    _check_company_auto = True

    name = fields.Char(required=True)
    name_source = fields.Selection(
        [
            ("fallback", "Identifier fallback"),
            ("provider", "Provider observed"),
            ("manual", "Manual"),
        ],
        required=True,
        default="manual",
        readonly=True,
        copy=False,
    )
    observed_name = fields.Char(readonly=True, copy=False)
    observed_name_at = fields.Datetime(readonly=True, copy=False, index=True)
    observed_name_inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        readonly=True,
        copy=False,
        index=True,
        ondelete="set null",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    mail_guest_id = fields.Many2one(
        "mail.guest",
        required=True,
        index=True,
        ondelete="restrict",
        copy=False,
        readonly=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        index=True,
        ondelete="restrict",
        check_company=True,
        domain="[('company_id', 'in', [False, company_id])]",
    )
    partner_link_kind = fields.Selection(
        [
            ("person", "Person"),
            ("central_company", "Centralized company number"),
        ],
        readonly=True,
        copy=False,
        index=True,
    )
    partner_linked_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="set null"
    )
    partner_linked_at = fields.Datetime(readonly=True, copy=False)
    state = fields.Selection(
        [("active", "Active"), ("merged", "Merged")],
        required=True,
        default="active",
        index=True,
    )
    merged_into_id = fields.Many2one(
        "contact.center.identity", index=True, copy=False, ondelete="restrict"
    )
    alias_ids = fields.One2many(
        "contact.center.identity.alias", "identity_id", string="External Addresses"
    )
    channel_binding_ids = fields.One2many(
        "contact.center.channel.binding",
        "identity_id",
        string="Conversation Bindings",
    )

    _sql_constraints = [
        (
            "mail_guest_unique",
            "unique(mail_guest_id)",
            "A guest can belong to only one identity.",
        ),
        (
            "merged_identity_not_self",
            "check(merged_into_id IS NULL OR merged_into_id != id)",
            "An identity cannot redirect to itself.",
        ),
        (
            "partner_link_kind_consistent",
            "check((partner_id IS NULL AND partner_link_kind IS NULL) OR "
            "(partner_id IS NOT NULL AND partner_link_kind IN "
            "('person', 'central_company')))",
            "A linked contact must have an explicit Contact Center link kind.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        internal_name_write = (
            self.env.context.get("contact_center_identity_name_token")
            is _IDENTITY_NAME_TOKEN
        )
        prepared = []
        for values in vals_list:
            values = dict(values)
            values.setdefault(
                "name_source", "fallback" if internal_name_write else "manual"
            )
            prepared.append(values)
        partner_ids = [
            values.get("partner_id") for values in prepared if values.get("partner_id")
        ]
        if partner_ids:
            # Direct internal creates remain supported (notably import/test
            # fixtures), but they must join the same partner -> identity
            # serialization protocol as the explicit linking actions.
            self._contact_center_lock_partner_rows(partner_ids, touch=True)
        return super().create(prepared)

    @api.model
    def _contact_center_create_managed(self, values):
        """Create an identity whose initial name remains provider-manageable."""

        identity = (
            self.sudo()
            .with_context(contact_center_identity_name_token=_IDENTITY_NAME_TOKEN)
            .create(dict(values, name_source="fallback"))
        )
        # Never leak the internal capability token to the caller.  In particular,
        # an operator rename performed on the returned record must become manual.
        return identity.with_context(contact_center_identity_name_token=False)

    @api.constrains("state", "merged_into_id", "company_id")
    def _check_merge(self):
        for record in self:
            if record.state == "merged" and not record.merged_into_id:
                raise ValidationError(
                    _("A merged identity must point to its survivor.")
                )
            if record.state != "merged" and record.merged_into_id:
                raise ValidationError(_("Only merged identities may have a survivor."))
            if (
                record.merged_into_id
                and record.merged_into_id.company_id != record.company_id
            ):
                raise ValidationError(
                    _("Merged identities must belong to the same company.")
                )
            if record.merged_into_id and (
                record.merged_into_id.state != "active"
                or record.merged_into_id.merged_into_id
            ):
                raise ValidationError(
                    _("A merged identity must point directly to an active survivor.")
                )

    @api.constrains("partner_id", "partner_link_kind", "company_id")
    def _check_partner_link_kind(self):
        for identity in self:
            if bool(identity.partner_id) != bool(identity.partner_link_kind):
                raise ValidationError(
                    _(
                        "A linked contact must have an explicit Contact Center link kind."
                    )
                )
            if identity.partner_id:
                identity._contact_center_validate_partner_link(
                    identity.partner_id, identity.partner_link_kind
                )

    @api.model
    def _contact_center_lock_partner_rows(
        self, partner_ids, *, touch=False, required=True
    ):
        """Lock partner rows before any identity row.

        The no-op update is intentional. Odoo transactions use PostgreSQL
        ``REPEATABLE READ``: if a linker only locks the partner and then updates
        an identity row, a waiting partner writer could retain an older snapshot
        and miss that new link. Creating a new MVCC version on the partner makes
        that waiter retry with a fresh snapshot before changing its structure.
        """

        normalized_ids = []
        for partner_id in partner_ids:
            if type(partner_id) is not int or partner_id <= 0:
                raise ValidationError(_("A valid contact ID is required."))
            normalized_ids.append(partner_id)
        normalized_ids = sorted(set(normalized_ids))
        if not normalized_ids:
            return self.env["res.partner"]
        self.env.cr.execute(
            "SELECT id FROM res_partner WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [normalized_ids],
        )
        locked_ids = [row[0] for row in self.env.cr.fetchall()]
        if required and locked_ids != normalized_ids:
            raise ValidationError(_("The selected contact no longer exists."))
        if touch and locked_ids:
            self.env.cr.execute(
                "UPDATE res_partner SET write_date = write_date WHERE id = ANY(%s)",
                [locked_ids],
            )
        partners = self.env["res.partner"].browse(locked_ids)
        partners.invalidate_recordset(
            ["active", "company_id", "company_type", "is_company", "type", "user_ids"]
        )
        return partners

    @api.model
    def _contact_center_partner_projection(self, partner, values=None):
        values = values or {}
        if "company_type" in values:
            is_company = values["company_type"] == "company"
            if "is_company" in values and bool(values["is_company"]) != is_company:
                raise ValidationError(
                    _("The contact type and company flag must be consistent.")
                )
        else:
            is_company = bool(values.get("is_company", partner.is_company))
        company_value = values.get("company_id", partner.company_id.id or False)
        if company_value in (False, None):
            company_id = False
        elif type(company_value) is int and company_value > 0:
            company_id = company_value
        else:
            raise ValidationError(_("A valid Odoo company is required."))
        return {
            "active": bool(values.get("active", partner.active)),
            "company_id": company_id,
            "is_company": is_company,
            "type": values.get("type", partner.type),
        }

    def _contact_center_validate_partner_link(self, partner, link_kind, values=None):
        """Validate the durable identity -> partner classification invariant."""

        self.ensure_one()
        projection = self._contact_center_partner_projection(partner, values=values)
        expected_is_company = link_kind == "central_company"
        if link_kind not in ("person", "central_company"):
            raise ValidationError(_("Unsupported Contact Center link kind."))
        internal_users = (
            partner.sudo()
            .with_context(active_test=False)
            .user_ids.filtered(lambda user: user.active and not user.share)
        )
        if (
            not projection["active"]
            or projection["is_company"] != expected_is_company
            or projection["type"] != "contact"
            or internal_users
        ):
            if link_kind == "person":
                raise ValidationError(_("Select an active person contact."))
            raise ValidationError(_("Select an active company contact."))
        if projection["company_id"] not in (False, self.company_id.id):
            if link_kind == "person":
                raise ValidationError(_("The contact belongs to another company."))
            raise ValidationError(_("The company belongs to another Odoo company."))
        return True

    def action_link_partner(self, partner_id):
        self.ensure_one()
        self._check_promotion_access()
        partner = self.env["res.partner"].browse(partner_id).exists()
        if not partner:
            raise ValidationError(_("The selected contact does not exist."))
        partner.check_access_rights("read")
        partner.check_access_rule("read")
        partner = self._contact_center_lock_partner_rows([partner.id], touch=True)
        self._lock_partner_link()
        if self.state != "active" or self.merged_into_id:
            raise ValidationError(_("A merged identity cannot be linked."))
        self._contact_center_validate_partner_link(partner, "person")
        if self.partner_id:
            if self.partner_id == partner and self.partner_link_kind == "person":
                return False
            raise ValidationError(
                _("This identity is already linked to another contact.")
            )
        self.sudo().with_context(
            contact_center_identity_link_token=_IDENTITY_LINK_TOKEN
        ).write(
            {
                "partner_id": partner.id,
                "partner_link_kind": "person",
                "partner_linked_by_id": self.env.user.id,
                "partner_linked_at": fields.Datetime.now(),
            }
        )
        return True

    def action_link_central_company(self, partner_id):
        """Link an explicitly selected shared company number to its company.

        The normal promotion path deliberately remains person-only.  A company is
        accepted here only through the separately named supervisor operation so a
        regular contact number cannot be classified as a company by accident.
        """

        self.ensure_one()
        self._check_promotion_access()
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _(
                    "Only Contact Center supervisors can link a centralized number "
                    "directly to a company."
                )
            )
        partner = self.env["res.partner"].browse(partner_id).exists()
        if not partner:
            raise ValidationError(_("The selected company does not exist."))
        partner.check_access_rights("read")
        partner.check_access_rule("read")
        partner = self._contact_center_lock_partner_rows([partner.id], touch=True)
        self._lock_partner_link()
        if self.state != "active" or self.merged_into_id:
            raise ValidationError(_("A merged identity cannot be linked."))
        self._contact_center_validate_partner_link(partner, "central_company")
        if self.partner_id:
            if (
                self.partner_id == partner
                and self.partner_link_kind == "central_company"
            ):
                return False
            raise ValidationError(
                _("This identity is already linked to another contact.")
            )
        self.sudo().with_context(
            contact_center_identity_link_token=_IDENTITY_LINK_TOKEN
        ).write(
            {
                "partner_id": partner.id,
                "partner_link_kind": "central_company",
                "partner_linked_by_id": self.env.user.id,
                "partner_linked_at": fields.Datetime.now(),
            }
        )
        return True

    def write(self, values):
        values = dict(values)
        internal_name_write = (
            self.env.context.get("contact_center_identity_name_token")
            is _IDENTITY_NAME_TOKEN
        )
        manual_name_write = "name" in values and not internal_name_write
        previous_names = {identity.id: identity.name for identity in self}
        if manual_name_write:
            # A direct ORM rename is an explicit operator decision.  Mark it here so
            # later provider observations can still be recorded without projecting
            # over the chosen display name.
            values["name_source"] = "manual"
        protected_fields = {
            "company_id",
            "mail_guest_id",
            "partner_id",
            "partner_link_kind",
            "partner_linked_by_id",
            "partner_linked_at",
            "state",
            "merged_into_id",
            "name_source",
            "observed_name",
            "observed_name_at",
            "observed_name_inbox_event_id",
        }
        protected_changes = protected_fields & set(values)
        if manual_name_write:
            protected_changes -= {"name_source"}
        if (
            protected_changes
            and self.env.context.get("contact_center_identity_link_token")
            is not _IDENTITY_LINK_TOKEN
            and not internal_name_write
        ):
            raise AccessError(
                _(
                    "Identity ownership, promotion and merge fields require an "
                    "explicit Contact Center action."
                )
            )
        if "mail_guest_id" in values:
            for identity in self:
                if values["mail_guest_id"] != identity.mail_guest_id.id:
                    raise ValidationError(
                        _("The persistent guest of an identity cannot be replaced.")
                    )
        result = super().write(values)
        if manual_name_write:
            for identity in self:
                identity._contact_center_sync_managed_name(
                    previous_names.get(identity.id) or "", identity.name or ""
                )
        return result

    @api.model
    def _contact_center_name_datetime(self, value):
        observed_at = (
            value
            if isinstance(value, datetime.datetime)
            else fields.Datetime.to_datetime(value)
        )
        if not observed_at:
            return False
        if observed_at.tzinfo:
            observed_at = observed_at.astimezone(datetime.timezone.utc).replace(
                tzinfo=None
            )
        return observed_at.replace(microsecond=0)

    def _contact_center_sync_managed_name(self, previous_name, new_name):
        """Synchronize dependants only while they still equal our old fallback."""

        self.ensure_one()
        if not new_name or new_name == previous_name:
            return False
        guest = self.mail_guest_id.sudo()
        if guest and (guest.name or "") == previous_name:
            guest.write({"name": new_name})
        direct_channels = (
            self.channel_binding_ids.sudo()
            .filtered(
                lambda binding: binding.active
                and not binding.merged_into_id
                and binding.conversation_type == "direct"
            )
            .channel_id
        )
        managed_channels = direct_channels.filtered(
            lambda channel: (channel.name or "") == previous_name
        )
        if managed_channels:
            managed_channels.sudo().write({"name": new_name})
        if not self.env.context.get("contact_center_skip_name_notification"):
            application = self.env["contact.center.application"]
            for channel in direct_channels:
                application._notify_ui(
                    channel,
                    "conversation_updated",
                    {"changed_fields": ["identity_name"]},
                )
        return True

    @api.model
    def _contact_center_clean_observed_name(self, name):
        clean_name = name.strip()[:255] if isinstance(name, str) else ""
        return (
            clean_name if any(character.isalnum() for character in clean_name) else ""
        )

    @api.model
    def _contact_center_format_whatsapp_pn(self, value):
        """Return a conservative human-readable label for a WhatsApp PN."""

        normalized = value.strip() if isinstance(value, str) else ""
        if not normalized:
            return ""
        local_part, separator, domain = normalized.partition("@")
        if separator and domain.casefold() not in ("s.whatsapp.net", "c.us"):
            return ""
        if (
            not local_part.isdigit()
            or local_part.startswith("0")
            or not 8 <= len(local_part) <= 15
        ):
            return ""
        if local_part.startswith("55") and len(local_part) in (12, 13):
            area_code = local_part[2:4]
            subscriber = local_part[4:]
            prefix_size = len(subscriber) - 4
            return "+55 %s %s-%s" % (
                area_code,
                subscriber[:prefix_size],
                subscriber[prefix_size:],
            )
        return "+%s" % local_part

    @api.model
    def _contact_center_fallback_name_from_addresses(self, addresses):
        """Prefer a trusted PN label over opaque provider identifiers."""

        addresses = tuple(addresses or ())
        for address in addresses:
            if getattr(address, "namespace", "") == "whatsapp.pn" and getattr(
                address, "confidence", ""
            ) in ("protocol", "manual"):
                formatted = self._contact_center_format_whatsapp_pn(
                    getattr(address, "value_normalized", "")
                    or getattr(address, "value", "")
                )
                if formatted:
                    return formatted
        for address in addresses:
            fallback = getattr(address, "value_normalized", "") or getattr(
                address, "value", ""
            )
            if isinstance(fallback, str) and fallback.strip():
                return fallback.strip()[:255]
        return ""

    def _contact_center_sync_fallback_name_from_addresses(self, addresses):
        """Improve an existing managed fallback when trusted PN evidence arrives."""

        self.ensure_one()
        addresses = tuple(addresses or ())
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset(
            [
                "name",
                "name_source",
                "partner_id",
                "mail_guest_id",
                "alias_ids",
                "channel_binding_ids",
            ]
        )
        source = self.name_source
        if source != "fallback" or self.partner_id:
            return False
        persisted_pn_aliases = self.alias_ids.filtered(
            lambda alias: alias.namespace == "whatsapp.pn"
            and alias.confidence in ("protocol", "manual")
        ).sorted(
            key=lambda alias: (
                alias.last_seen_at or alias.first_seen_at,
                alias.id,
            ),
            reverse=True,
        )
        fallback_name = self._contact_center_fallback_name_from_addresses(
            (*addresses, *persisted_pn_aliases)
        )
        if not fallback_name:
            return False
        previous_name = self.name or ""
        values = {}
        if previous_name != fallback_name:
            values["name"] = fallback_name
        if not values:
            return False
        self.sudo().with_context(
            contact_center_identity_name_token=_IDENTITY_NAME_TOKEN
        ).write(values)
        if previous_name != fallback_name:
            self._contact_center_sync_managed_name(previous_name, fallback_name)
        return True

    def _contact_center_observe_name(self, name, observed_at, inbox_event):
        """Accept one provider-neutral name observation in monotonic event order."""

        self.ensure_one()
        clean_name = self._contact_center_clean_observed_name(name)
        if not clean_name:
            return False
        inbox_event = inbox_event.sudo().exists() if inbox_event else inbox_event
        if inbox_event:
            inbox_event.ensure_one()
        normalized_at = self._contact_center_name_datetime(
            observed_at or (inbox_event and inbox_event.create_date)
        )
        if not normalized_at:
            return False

        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset(
            [
                "name",
                "name_source",
                "partner_id",
                "mail_guest_id",
                "observed_name",
                "observed_name_at",
                "observed_name_inbox_event_id",
                "channel_binding_ids",
            ]
        )
        alias_values = {
            value.strip().casefold()
            for alias in self.alias_ids
            for value in (alias.value_raw, alias.value_normalized)
            if isinstance(value, str) and value.strip()
        }
        if clean_name.casefold() in alias_values:
            return False
        current_at = self._contact_center_name_datetime(self.observed_name_at)
        current_key = (
            current_at or datetime.datetime.min,
            self.observed_name_inbox_event_id.id or 0,
        )
        incoming_key = (normalized_at, inbox_event.id if inbox_event else 0)
        if incoming_key <= current_key:
            return False

        source = self.name_source
        previous_name = self.name or ""
        values = {
            "observed_name": clean_name,
            "observed_name_at": normalized_at,
            "observed_name_inbox_event_id": inbox_event.id if inbox_event else False,
        }
        project_name = source in _MANAGED_NAME_SOURCES and not self.partner_id
        if project_name:
            values.update({"name": clean_name, "name_source": "provider"})
        self.sudo().with_context(
            contact_center_identity_name_token=_IDENTITY_NAME_TOKEN
        ).write(values)
        if project_name:
            self._contact_center_sync_managed_name(previous_name, clean_name)
        return True

    @api.model
    def _contact_center_portable_merge_blocker(self, identities):
        if len(identities.mapped("company_id")) != 1 or any(
            identity.state != "active" or identity.merged_into_id
            for identity in identities
        ):
            return "identity state or company mismatch"
        if len(identities.mapped("partner_id")) > 1:
            return "different linked contacts"
        if len(set(identities.mapped("partner_link_kind")) - {False}) > 1:
            return "different contact link kinds"
        manual_names = {
            (identity.name or "").strip().casefold()
            for identity in identities
            if identity.name_source == "manual" and (identity.name or "").strip()
        }
        if len(manual_names) > 1:
            return "different manual names"
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("identity_id", "in", identities.ids),
                    ("conversation_type", "=", "direct"),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ]
            )
        )
        account_ids = [binding.account_id.id for binding in bindings]
        if len(account_ids) != len(set(account_ids)):
            return "multiple active direct conversations in one inbox"
        return ""

    @api.model
    def _contact_center_portable_merge_survivor(self, identities):
        def priority(identity):
            observed_at = (
                self._contact_center_name_datetime(identity.observed_name_at)
                or datetime.datetime.min
            )
            return (
                bool(identity.partner_id),
                identity.name_source == "manual",
                bool(identity.observed_name),
                observed_at,
                -identity.id,
            )

        return max(identities, key=priority)

    @api.model
    def _contact_center_member_operational_values(self, member):
        """Copy only portable state when replacing an immutable guest member."""

        member.ensure_one()
        return {
            "custom_channel_name": member.custom_channel_name or False,
            "fetched_message_id": member.fetched_message_id.id or False,
            "seen_message_id": member.seen_message_id.id or False,
            "fold_state": member.fold_state,
            "is_minimized": bool(member.is_minimized),
            "is_pinned": bool(member.is_pinned),
            "last_interest_dt": member.last_interest_dt or False,
            "last_seen_dt": member.last_seen_dt or False,
        }

    @api.model
    def _contact_center_merge_member_operational_values(self, survivor, retired):
        """Keep the survivor preferences and advance its monotonic pointers."""

        survivor.ensure_one()
        retired.ensure_one()
        values = {}
        if not survivor.custom_channel_name and retired.custom_channel_name:
            values["custom_channel_name"] = retired.custom_channel_name
        for field_name in ("fetched_message_id", "seen_message_id"):
            current = survivor[field_name]
            incoming = retired[field_name]
            if incoming and (not current or incoming.id > current.id):
                values[field_name] = incoming.id
        for field_name in ("last_interest_dt", "last_seen_dt"):
            current = survivor[field_name]
            incoming = retired[field_name]
            if incoming and (not current or incoming > current):
                values[field_name] = incoming
        return values

    @api.model
    def _contact_center_rebind_retired_guest_memberships(
        self, retired_guests, survivor
    ):
        """Move active membership without rewriting immutable author history."""

        if not retired_guests:
            return True
        survivor.ensure_one()
        survivor_guest = survivor.mail_guest_id
        member_model = (
            self.env["mail.channel.member"]
            .sudo()
            .with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
        )
        member_ids = member_model.search(
            [
                ("guest_id", "in", retired_guests.ids),
                ("channel_id.channel_type", "=", "contact_center"),
                (
                    "channel_id.contact_center_company_id",
                    "=",
                    survivor.company_id.id,
                ),
                ("channel_id.contact_center_binding_ids.active", "=", True),
                (
                    "channel_id.contact_center_binding_ids.merged_into_id",
                    "=",
                    False,
                ),
            ],
            order="channel_id, id",
        ).ids
        for member_id in member_ids:
            member = member_model.browse(member_id).exists()
            if not member:
                continue
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE",
                [member.channel_id.id],
            )
            self.env.cr.execute(
                "SELECT id FROM mail_channel_member "
                "WHERE channel_id = %s AND guest_id IN %s "
                "ORDER BY id FOR UPDATE",
                [
                    member.channel_id.id,
                    (member.guest_id.id, survivor_guest.id),
                ],
            )
            locked_member_ids = {row[0] for row in self.env.cr.fetchall()}
            if member.id not in locked_member_ids:
                continue
            member.invalidate_recordset()
            duplicate = member_model.search(
                [
                    ("id", "!=", member.id),
                    ("channel_id", "=", member.channel_id.id),
                    ("guest_id", "=", survivor_guest.id),
                ],
                limit=1,
            )
            if duplicate:
                updates = self._contact_center_merge_member_operational_values(
                    duplicate, member
                )
                if updates:
                    duplicate.write(updates)
            else:
                values = self._contact_center_member_operational_values(member)
                values.update(
                    {
                        "channel_id": member.channel_id.id,
                        "guest_id": survivor_guest.id,
                    }
                )
                member_model.create(values)
            # Odoo 16 intentionally forbids changing ``guest_id`` on an existing
            # member, even under sudo.  Replace the immutable member only after its
            # successor exists, then let core unlink clean any RTC state.
            member.unlink()
        return True

    @api.model
    def _contact_center_merge_portable_component(self, identities):
        identities = identities.sudo().exists()
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(identities.ids)],
        )
        identities.invalidate_recordset()
        blocker = self._contact_center_portable_merge_blocker(identities)
        if blocker:
            return False, blocker
        survivor = self._contact_center_portable_merge_survivor(identities)
        retired = identities - survivor
        retired_guests = retired.mapped("mail_guest_id")
        self._contact_center_rebind_retired_guest_memberships(retired_guests, survivor)
        self.env["contact.center.identity.alias"].sudo().search(
            [("identity_id", "in", retired.ids)]
        ).write({"identity_id": survivor.id})
        self.env["contact.center.channel.binding"].sudo().search(
            [("identity_id", "in", retired.ids)]
        ).write({"identity_id": survivor.id})
        touchpoints = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("identity_id", "in", retired.ids)])
        )
        if touchpoints:
            touchpoints.with_context(
                contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
            ).write({"identity_id": survivor.id})
        retired.with_context(
            contact_center_identity_link_token=_IDENTITY_LINK_TOKEN
        ).write({"state": "merged", "merged_into_id": survivor.id})
        return survivor, ""

    def _lock_partner_link(self):
        if self.ids:
            self.env.cr.execute(
                "SELECT id FROM contact_center_identity WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [sorted(self.ids)],
            )
            locked_ids = [row[0] for row in self.env.cr.fetchall()]
            if locked_ids != sorted(self.ids):
                raise ValidationError(_("The identity no longer exists."))
            self.invalidate_recordset(
                [
                    "company_id",
                    "merged_into_id",
                    "partner_id",
                    "partner_link_kind",
                    "state",
                ]
            )
        return self

    def action_unlink_partner(self, expected_partner_id=False):
        self.ensure_one()
        self._check_promotion_access()
        if type(expected_partner_id) is not int or expected_partner_id <= 0:
            raise ValidationError(_("The expected linked contact ID is required."))
        # The expected ID is mandatory precisely so an unlink can acquire the
        # canonical partner lock without trusting a stale identity cache.
        locked_partner = self._contact_center_lock_partner_rows(
            [expected_partner_id], touch=True, required=False
        )
        self._lock_partner_link()
        if not self.partner_id:
            # An identical retry after the first request committed is harmless.
            return False
        if not locked_partner:
            raise ValidationError(_("The linked contact no longer exists."))
        if self.partner_id.id != expected_partner_id:
            raise ValidationError(
                _("This identity is no longer linked to the expected contact.")
            )
        if self.partner_link_kind == "central_company" and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _(
                    "Only Contact Center supervisors can unlink a centralized "
                    "company number."
                )
            )
        self.sudo().with_context(
            contact_center_identity_link_token=_IDENTITY_LINK_TOKEN
        ).write(
            {
                "partner_id": False,
                "partner_link_kind": False,
                "partner_linked_by_id": False,
                "partner_linked_at": False,
            }
        )
        return True

    def action_rename_guest(self, name):
        """Set the operator-owned display name without replacing the guest."""

        self.ensure_one()
        self._check_promotion_access()
        if self.partner_id:
            raise UserError(
                _(
                    "Rename the linked contact from Contacts, not from the guest profile."
                )
            )
        if not isinstance(name, str):
            raise ValidationError(_("The guest name must be text."))
        clean_name = " ".join(name.split()).strip()[:255]
        if not clean_name or not any(character.isalnum() for character in clean_name):
            raise ValidationError(_("Enter a valid guest name."))
        self.sudo().write({"name": clean_name})
        return True

    def _check_promotion_access(self):
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            return True
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_agent"
        ):
            raise AccessError(_("Only Contact Center agents can link contacts."))
        partner = self.env.user.partner_id
        inaccessible = self.sudo().filtered(
            lambda identity: not any(
                partner in binding.channel_id.channel_member_ids.partner_id
                for binding in identity.channel_binding_ids.filtered(
                    lambda binding: binding.active and not binding.merged_into_id
                )
            )
        )
        if inaccessible:
            raise AccessError(
                _("You must be a conversation member to link this identity.")
            )
        return True


class ResPartnerContactCenterIdentityInvariant(models.Model):
    _inherit = "res.partner"

    def _contact_center_lock_linked_identities(self, *, touch=False):
        """Lock partner rows, then every identity that currently references them."""

        partners = self.exists()
        if not partners:
            return self.env["contact.center.identity"]
        partner_ids = sorted(partners.ids)
        self.env["contact.center.identity"]._contact_center_lock_partner_rows(
            partner_ids, touch=touch
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity "
            "WHERE partner_id = ANY(%s) ORDER BY id FOR UPDATE",
            [partner_ids],
        )
        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .browse([row[0] for row in self.env.cr.fetchall()])
        )
        identities.invalidate_recordset(
            ["company_id", "partner_id", "partner_link_kind", "state"]
        )
        return identities

    def _contact_center_validate_linked_identities(self, identities, values=None):
        partners_by_id = {partner.id: partner for partner in self}
        self.invalidate_recordset(
            ["active", "company_id", "company_type", "is_company", "type", "user_ids"]
        )
        for identity in identities:
            partner = partners_by_id.get(identity.partner_id.id)
            if partner:
                identity._contact_center_validate_partner_link(
                    partner, identity.partner_link_kind, values=values
                )
        return True

    def write(self, values):
        values = dict(values)
        if not (_LINKED_PARTNER_STRUCTURAL_FIELDS & set(values)):
            # Name, phone, e-mail, address and normal commercial maintenance do
            # not participate in the identity classification invariant.
            return super().write(values)
        identities = self._contact_center_lock_linked_identities()
        self._contact_center_validate_linked_identities(identities, values=values)
        return super().write(values)


class ResUsersContactCenterIdentityInvariant(models.Model):
    _inherit = "res.users"

    @api.model
    def _contact_center_changes_partner_classification(self, values):
        return bool(
            {"active", "groups_id", "partner_id", "share"} & set(values)
            or any(
                field_name.startswith(("in_group_", "sel_groups_"))
                for field_name in values
            )
        )

    @api.model
    def _contact_center_existing_partner_ids(self, values_list):
        partner_ids = []
        for values in values_list:
            partner_id = values.get("partner_id")
            if not partner_id:
                continue
            if type(partner_id) is not int or partner_id <= 0:
                raise ValidationError(_("A valid contact ID is required."))
            partner_ids.append(partner_id)
        return partner_ids

    @api.model
    def _contact_center_lock_user_partner_scope(self, partner_ids):
        partners = self.env["res.partner"].browse(sorted(set(partner_ids))).exists()
        identities = partners._contact_center_lock_linked_identities(touch=True)
        return partners, identities

    @api.model_create_multi
    def create(self, vals_list):
        partner_ids = self._contact_center_existing_partner_ids(vals_list)
        partners, identities = self._contact_center_lock_user_partner_scope(partner_ids)
        users = super().create(vals_list)
        if partners:
            users.invalidate_recordset(["active", "partner_id", "share"])
            partners._contact_center_validate_linked_identities(identities)
        return users

    def write(self, values):
        values = dict(values)
        if not self._contact_center_changes_partner_classification(values):
            return super().write(values)
        partner_ids = self.mapped("partner_id").ids
        partner_ids += self._contact_center_existing_partner_ids([values])
        partners, identities = self._contact_center_lock_user_partner_scope(partner_ids)
        result = super().write(values)
        self.invalidate_recordset(["active", "partner_id", "share"])
        partners._contact_center_validate_linked_identities(identities)
        return result

    @api.constrains("active", "partner_id", "share")
    def _check_contact_center_identity_partner_classification(self):
        partners = self.mapped("partner_id")
        if not partners:
            return
        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .search([("partner_id", "in", partners.ids)])
        )
        partners._contact_center_validate_linked_identities(identities)


class ContactCenterIdentityAlias(models.Model):
    _name = "contact.center.identity.alias"
    _description = "Contact Center Identity Alias"
    _order = "account_id, namespace, value_normalized"

    identity_id = fields.Many2one(
        "contact.center.identity", required=True, index=True, ondelete="restrict"
    )
    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    namespace = fields.Char(required=True, index=True)
    value_raw = fields.Char(required=True)
    value_normalized = fields.Char(required=True, index=True)
    role = fields.Selection(
        [
            ("primary", "Primary"),
            ("alternate", "Alternate"),
            ("sender", "Sender"),
            ("recipient", "Recipient"),
            ("device", "Device"),
        ],
        required=True,
        default="primary",
    )
    source_field = fields.Char()
    confidence = fields.Selection(
        [
            ("observed", "Observed"),
            ("protocol", "Protocol-validated"),
            ("manual", "Manual"),
        ],
        required=True,
        default="observed",
    )
    resolution_scope = fields.Selection(
        [("account", "Account"), ("company", "Company")],
        required=True,
        default="account",
        index=True,
        help=(
            "Account aliases resolve only inside their inbox. Company aliases may "
            "reuse one identity across inboxes when the provider supplied a "
            "protocol-validated portable identifier."
        ),
    )
    first_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)
    last_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _sql_constraints = [
        (
            "account_namespace_value_unique",
            "unique(account_id, namespace, value_normalized)",
            "This external identity address already exists for the account.",
        ),
    ]

    @api.constrains("identity_id", "account_id")
    def _check_company(self):
        for alias in self:
            if alias.identity_id.company_id != alias.account_id.company_id:
                raise ValidationError(
                    _("Identity aliases must remain in the account company.")
                )


class ContactCenterIdentityConflict(models.Model):
    _name = "contact.center.identity.conflict"
    _description = "Contact Center Identity Conflict"
    _order = "create_date desc"

    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    identity_ids = fields.Many2many(
        "contact.center.identity",
        "contact_center_identity_conflict_rel",
        "conflict_id",
        "identity_id",
        required=True,
    )
    address_evidence_json = fields.Json(required=True, default=list)
    inbox_event_id = fields.Many2one(
        "contact.center.inbox.event", index=True, copy=False, ondelete="set null"
    )
    state = fields.Selection(
        [("open", "Open"), ("resolved", "Resolved")],
        required=True,
        default="open",
        index=True,
    )
    resolution_note = fields.Text()
    resolved_by_id = fields.Many2one("res.users", readonly=True, ondelete="set null")
    resolved_at = fields.Datetime(readonly=True)

    @api.constrains("identity_ids", "account_id")
    def _check_identity_companies(self):
        for conflict in self:
            if any(
                identity.company_id != conflict.account_id.company_id
                for identity in conflict.identity_ids
            ):
                raise ValidationError(
                    _("Conflict identities must remain in the account company.")
                )

    def action_resolve(self):
        self.write(
            {
                "state": "resolved",
                "resolved_by_id": self.env.user.id,
                "resolved_at": fields.Datetime.now(),
            }
        )
        return True
