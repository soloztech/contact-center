"""Contact and company projection/linking for the operational UI API."""

from odoo import _, api, models
from odoo.exceptions import ValidationError

from ..services.dto import SCHEMA_VERSION


class ContactCenterUiApiContacts(models.AbstractModel):
    # Keep contact/company operations separate from conversation projection.
    # pylint: disable-next=consider-merging-classes-inherited
    _inherit = "contact.center.ui.api"

    @api.model
    def _suggested_phone(self, identity, binding=None):
        preferred_namespaces = (
            "whatsapp.pn",
            "whatsapp.jid",
            "phone",
        )
        visible_aliases = identity.alias_ids.filtered(
            lambda alias: binding and alias.account_id == binding.account_id
        )
        aliases = sorted(
            visible_aliases,
            key=lambda alias: (
                (
                    preferred_namespaces.index(alias.namespace)
                    if alias.namespace in preferred_namespaces
                    else len(preferred_namespaces)
                ),
                alias.id,
            ),
        )
        for alias in aliases:
            if alias.namespace not in preferred_namespaces:
                continue
            value = (alias.value_normalized or alias.value_raw or "").split("@", 1)[0]
            digits = "".join(character for character in value if character.isdigit())
            if len(digits) >= 8:
                return "+%s" % digits
        return ""

    @api.model
    def _serialize_partner_company(self, company):
        if not company:
            return False
        return {
            "id": company.id,
            "name": company.name or _("Unnamed company"),
            "email": company.email or "",
            "phone": company.phone or company.mobile or "",
            "vat": company.vat or "",
        }

    @api.model
    def _visible_secondary_companies(self, identity, partner):
        if not partner or identity.partner_link_kind != "person":
            return self.env["res.partner"]
        return (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    (
                        "id",
                        "in",
                        partner.sudo().contact_center_secondary_company_ids.ids,
                    ),
                    ("is_company", "=", True),
                    ("type", "=", "contact"),
                    ("company_id", "in", [False, identity.company_id.id]),
                ],
                order="name, id",
            )
        )

    @api.model
    def _partner_company_management_allowed(self, identity, partner):
        return bool(
            partner
            and identity.partner_link_kind == "person"
            and partner.active
            and not partner.is_company
            and partner.type == "contact"
            and partner.company_id == identity.company_id
            and not self._partner_has_internal_user(partner)
        )

    @api.model
    def _visible_partner_company(self, identity, partner):
        company_id = partner.sudo().parent_id.id
        if not company_id:
            return self.env["res.partner"]
        # Resolve the parent again without sudo so an inaccessible or
        # cross-company legacy relation is hidden instead of breaking the whole
        # identity projection or leaking partner data.
        return (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", company_id),
                    ("is_company", "=", True),
                    ("type", "=", "contact"),
                    ("company_id", "in", [False, identity.company_id.id]),
                ],
                limit=1,
            )
        )

    @api.model
    def _partner_has_internal_user(self, partner):
        return bool(
            partner
            and self.env["res.users"]
            .sudo()
            .search(
                [("partner_id", "=", partner.id), ("share", "=", False)],
                limit=1,
            )
        )

    @api.model
    def _partner_company_linking_allowed(self, identity, partner):
        return bool(
            partner
            and not partner.parent_id
            and self._partner_company_management_allowed(identity, partner)
        )

    @api.model
    def _batch_partner_company_projection(self, identities):
        empty_company = self.env["res.partner"]
        partners = identities.mapped("partner_id")
        internal_user_partner_ids = set(
            self.env["res.users"]
            .sudo()
            .search(
                [
                    ("partner_id", "in", partners.ids),
                    ("share", "=", False),
                ]
            )
            .mapped("partner_id")
            .ids
        )
        parent_id_by_identity = {
            identity.id: identity.partner_id.sudo().parent_id.id
            for identity in identities
            if identity.partner_id
        }
        parent_ids = list(set(parent_id_by_identity.values()) - {False})
        secondary_ids_by_partner = {
            partner.id: partner.sudo().contact_center_secondary_company_ids.ids
            for partner in partners
        }
        all_company_ids = set(parent_ids)
        for secondary_ids in secondary_ids_by_partner.values():
            all_company_ids.update(secondary_ids)
        company_ids = identities.mapped("company_id").ids
        visible_companies = (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    ("id", "in", sorted(all_company_ids)),
                    ("is_company", "=", True),
                    ("type", "=", "contact"),
                    ("company_id", "in", [False] + company_ids),
                ]
            )
            if all_company_ids
            else empty_company
        )
        visible_by_id = {company.id: company for company in visible_companies}
        company_by_identity = {}
        linking_allowed_by_identity = {}
        management_allowed_by_identity = {}
        secondary_by_identity = {}
        for identity in identities:
            partner = identity.partner_id
            company = visible_by_id.get(parent_id_by_identity.get(identity.id))
            if (
                company
                and company.company_id
                and company.company_id != identity.company_id
            ):
                company = empty_company
            company_by_identity[identity.id] = company or empty_company
            management_allowed_by_identity[identity.id] = bool(
                partner
                and identity.partner_link_kind == "person"
                and partner.active
                and not partner.is_company
                and partner.type == "contact"
                and partner.company_id == identity.company_id
                and partner.id not in internal_user_partner_ids
            )
            linking_allowed_by_identity[identity.id] = bool(
                management_allowed_by_identity[identity.id]
                and not parent_id_by_identity.get(identity.id)
            )
            secondary_by_identity[identity.id] = visible_companies.filtered(
                lambda item: item.id in secondary_ids_by_partner.get(partner.id, [])
                and (not item.company_id or item.company_id == identity.company_id)
            ).sorted(lambda item: (item.name or "", item.id))
        return (
            company_by_identity,
            linking_allowed_by_identity,
            secondary_by_identity,
            management_allowed_by_identity,
        )

    @api.model
    def _serialize_identity(
        self,
        identity,
        binding=None,
        partner_company=None,
        company_linking_allowed=None,
        secondary_companies=None,
        company_management_allowed=None,
    ):
        if not identity:
            return False
        partner = identity.partner_id
        link_kind = identity.partner_link_kind or ("person" if partner else False)
        if partner_company is None:
            partner_company = (
                self._visible_partner_company(identity, partner) if partner else False
            )
        if company_linking_allowed is None:
            company_linking_allowed = self._partner_company_linking_allowed(
                identity, partner
            )
        if secondary_companies is None:
            secondary_companies = self._visible_secondary_companies(identity, partner)
        if company_management_allowed is None:
            company_management_allowed = self._partner_company_management_allowed(
                identity, partner
            )
        return {
            "id": identity.id,
            "name": self._identity_display_name(identity),
            "persona_kind": "contact" if partner else "guest",
            "link_kind": link_kind,
            "link_invariant_valid": bool(
                not partner
                or (
                    link_kind == "person"
                    and partner.active
                    and not partner.is_company
                    and partner.type == "contact"
                    and (
                        not partner.company_id
                        or partner.company_id == identity.company_id
                    )
                )
                or (
                    link_kind == "central_company"
                    and partner.active
                    and partner.is_company
                    and partner.type == "contact"
                    and (
                        not partner.company_id
                        or partner.company_id == identity.company_id
                    )
                )
            ),
            "guest": {
                "id": identity.mail_guest_id.id,
                "name": identity.mail_guest_id.name,
            },
            "partner": (
                {
                    "id": partner.id,
                    "name": partner.name or identity.name,
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                    "vat": partner.vat or "",
                    "is_company": bool(partner.is_company),
                    "company_linking_allowed": bool(company_linking_allowed),
                    "company": self._serialize_partner_company(partner_company),
                    "secondary_companies": [
                        self._serialize_partner_company(company)
                        for company in secondary_companies
                    ],
                    "company_management_allowed": bool(company_management_allowed),
                    "secondary_company_linking_allowed": bool(
                        company_management_allowed
                        and identity.company_id.contact_center_secondary_companies_enabled
                    ),
                }
                if partner
                else False
            ),
            "suggested_phone": self._suggested_phone(identity, binding=binding),
            "avatar_url": self._identity_avatar_url(binding, identity),
            "aliases": [
                {
                    "id": alias.id,
                    "namespace": alias.namespace,
                    "value": alias.value_raw,
                    "normalized": alias.value_normalized,
                    "role": alias.role,
                    "confidence": alias.confidence,
                }
                for alias in identity.alias_ids.filtered(
                    lambda item: binding and item.account_id == binding.account_id
                ).sorted(key=lambda item: (item.namespace, item.id))
            ],
        }

    @api.model
    def _identity_display_name(self, identity):
        """Resolve list and detail names identically without building detail."""

        partner = identity.partner_id
        return (partner.name or identity.name) if partner else identity.name

    @api.model
    def search_partners(self, channel_id, query, limit=12):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(query, str) or len(query.strip()) < 2:
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        partners = self.env["res.partner"].search(
            [
                ("active", "=", True),
                ("is_company", "=", False),
                ("type", "=", "contact"),
                ("company_id", "in", [False, channel.contact_center_company_id.id]),
                ("partner_share", "=", True),
                "|",
                "|",
                "|",
                ("name", "ilike", query),
                ("email", "ilike", query),
                ("phone", "ilike", query),
                ("mobile", "ilike", query),
            ],
            order="name, id",
            limit=limit,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "items": [
                {
                    "id": partner.id,
                    "name": partner.display_name or _("Unnamed contact"),
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                }
                for partner in partners
            ],
        }

    @api.model
    def _identity_for_channel(self, channel):
        binding = self._binding_for_channel(channel)
        if not binding or not binding.identity_id:
            raise ValidationError(_("The conversation has no remote identity."))
        return binding.identity_id

    @api.model
    def _lock_identity(self, identity):
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE",
            [identity.id],
        )
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        return identity

    @api.model
    def _check_central_company_access(self):
        self._application()._check_agent()
        return True

    @api.model
    def _company_creation_values(self, channel, values):
        if not isinstance(values, dict):
            raise ValidationError(_("Company values must be an object."))
        allowed = {"name", "vat", "email", "phone", "mobile"}
        if set(values) - allowed:
            raise ValidationError(_("Unsupported company field."))
        name = values.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(_("A company name is required."))
        company_values = {
            "name": name.strip()[:256],
            "company_id": channel.contact_center_company_id.id,
            "is_company": True,
            "type": "contact",
        }
        for field_name in ("vat", "email", "phone", "mobile"):
            value = values.get(field_name)
            if value is None or value is False or value == "":
                continue
            if not isinstance(value, str):
                raise ValidationError(_("Company fields must be text."))
            value = value.strip()
            if value:
                company_values[field_name] = value[:256]
        return company_values

    @api.model
    def _linked_person_for_company(self, channel, expected_partner_id, *, lock=False):
        # Public callers authorize the conversation before reaching this helper.
        # Keep the role check here as defense in depth while allowing the same
        # constrained operation to every Contact Center agent.
        self._application()._check_agent()
        expected_partner_id = self._positive_id(expected_partner_id, _("contact ID"))
        if lock:
            # A linked partner's structural-write guard takes partner ->
            # identity. Keep the same order while changing its parent relation.
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id = %s FOR UPDATE",
                [expected_partner_id],
            )
        identity = self._identity_for_channel(channel)
        if lock:
            self._lock_identity(identity)
        person = identity.partner_id
        if not person:
            raise ValidationError(_("Link a contact before linking a company."))
        if person.id != expected_partner_id:
            raise ValidationError(
                _("The linked contact changed. Reopen the company editor.")
            )
        if identity.partner_link_kind != "person":
            raise ValidationError(
                _("Only a personal Contact Center link can receive a company.")
            )
        if lock:
            person.invalidate_recordset(
                [
                    "active",
                    "is_company",
                    "type",
                    "company_id",
                    "parent_id",
                    "contact_center_secondary_company_ids",
                    "user_ids",
                ]
            )
        if (
            not person.active
            or person.is_company
            or person.type != "contact"
            or person.company_id != channel.contact_center_company_id
            or self._partner_has_internal_user(person)
        ):
            raise ValidationError(
                _("Manage this contact's company from the Contacts application.")
            )
        return identity, person

    @api.model
    def _partner_company_domain(self, channel):
        return [
            ("active", "=", True),
            ("is_company", "=", True),
            ("type", "=", "contact"),
            ("company_id", "in", [False, channel.contact_center_company_id.id]),
            ("partner_share", "=", True),
        ]

    @api.model
    def _search_partner_company_items(self, channel, query, limit):
        companies = self.env["res.partner"].search(
            self._partner_company_domain(channel)
            + [
                "|",
                "|",
                "|",
                "|",
                ("name", "ilike", query),
                ("email", "ilike", query),
                ("phone", "ilike", query),
                ("mobile", "ilike", query),
                ("vat", "ilike", query),
            ],
            order="name, id",
            limit=limit,
        )
        return [self._serialize_partner_company(company) for company in companies]

    @api.model
    def _partner_company_for_channel(self, channel, company_partner_id, *, lock=False):
        # Keep custom Contacts record rules as part of the boundary. The sudo
        # below is limited to the final write after this record was readable and
        # matched the explicit operational-company domain.
        company = self.env["res.partner"].search(
            self._partner_company_domain(channel)
            + [("id", "=", self._positive_id(company_partner_id, _("company ID")))],
            limit=1,
        )
        if not company:
            raise ValidationError(_("The selected company is not available."))
        company.check_access_rights("read")
        company.check_access_rule("read")
        if lock:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id = %s FOR UPDATE", [company.id]
            )
            company.invalidate_recordset(
                ["active", "is_company", "type", "company_id", "user_ids"]
            )
            if (
                not company.active
                or not company.is_company
                or company.type != "contact"
                or self._partner_has_internal_user(company)
                or (
                    company.company_id
                    and company.company_id != channel.contact_center_company_id
                )
            ):
                raise ValidationError(_("The selected company is not available."))
        return company

    @api.model
    def _notify_identity_channels(self, identity):
        """Invalidate every active inbox that projects one shared identity."""

        channels = (
            identity.channel_binding_ids.sudo()
            .filtered(
                lambda binding: binding.active
                and not binding.merged_into_id
                and binding.conversation_type == "direct"
            )
            .mapped("channel_id")
        )
        for channel in channels:
            self._application()._notify_ui(
                channel,
                "identity_updated",
                {"identity_id": identity.id},
            )
        return True

    @api.model
    def _notify_partner_identities(self, partner):
        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .search([("partner_id", "=", partner.id), ("state", "=", "active")])
        )
        for identity in identities:
            self._notify_identity_channels(identity)
        return True

    @api.model
    def link_partner(self, channel_id, partner_id):
        channel, _member = self._authorized_channel(channel_id)
        identity = self._identity_for_channel(channel)
        partner_id = self._positive_id(partner_id, _("contact ID"))
        # ``action_link_partner`` owns the canonical partner -> identity lock
        # order and all under-lock revalidation. Pre-locking the identity here
        # would invert that protocol against a concurrent Contacts edit.
        if identity.action_link_partner(partner_id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_partner(self, channel_id, values):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(values, dict):
            raise ValidationError(_("Contact values must be an object."))
        allowed = {"name", "email", "phone", "mobile"}
        if set(values) - allowed:
            raise ValidationError(_("Unsupported contact field."))
        name = values.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(_("A contact name is required."))
        identity = self._identity_for_channel(channel)
        self._lock_identity(identity)
        if identity.partner_id:
            partner = identity.partner_id
            if identity.partner_link_kind != "person" or partner.is_company:
                raise ValidationError(_("Select the centralized company workflow."))
            return {
                "schema_version": SCHEMA_VERSION,
                "created": False,
                "partner": {
                    "id": partner.id,
                    "name": partner.name or identity.name,
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                },
                "identity": self._serialize_identity(
                    identity, binding=self._binding_for_channel(channel)
                ),
            }
        partner_values = {
            "name": name.strip()[:256],
            "company_id": channel.contact_center_company_id.id,
            "company_type": "person",
            "type": "contact",
        }
        for field_name in ("email", "phone", "mobile"):
            value = values.get(field_name)
            if value:
                if not isinstance(value, str):
                    raise ValidationError(_("Contact fields must be text."))
                partner_values[field_name] = value.strip()[:256]
        # Promotion is a constrained Contact Center operation.  Do not require
        # the broad ``base.group_partner_manager`` permission merely to create
        # this one person: the authorized channel fixes the company and the
        # allow-list above fixes every accepted field.
        partner = self.env["res.partner"].sudo().create(partner_values)
        identity.action_link_partner(partner.id)
        self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "created": True,
            "partner": {
                "id": partner.id,
                "name": partner.name or identity.name,
                "email": partner.email or "",
                "phone": partner.phone or partner.mobile or "",
            },
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def search_central_companies(self, channel_id, query, limit=12):
        """Search the explicit exception for a shared company-owned number."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        identity = self._identity_for_channel(channel)
        if identity.partner_id or not isinstance(query, str) or len(query.strip()) < 2:
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "items": self._search_partner_company_items(channel, query, limit),
        }

    @api.model
    def link_central_company(self, channel_id, company_partner_id):
        """Link a guest identity directly to a centralized company number."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        company_partner_id = self._positive_id(company_partner_id, _("company ID"))
        # Resolve and authorize the company while acquiring the first canonical
        # lock; the identity action performs the final under-lock revalidation.
        company = self._partner_company_for_channel(
            channel, company_partner_id, lock=True
        )
        identity = self._identity_for_channel(channel)
        if identity.action_link_central_company(company.id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_central_company(self, channel_id, values):
        """Create and link the explicit centralized-company exception once."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        company_values = self._company_creation_values(channel, values)
        identity = self._identity_for_channel(channel)
        self._lock_identity(identity)
        if identity.partner_id:
            if (
                identity.partner_link_kind != "central_company"
                or not identity.partner_id.is_company
            ):
                raise ValidationError(
                    _("The linked contact changed. Reopen the company editor.")
                )
            company = identity.partner_id
            created = False
        else:
            company = (
                self.env["res.partner"]
                .sudo()
                .with_context(no_vat_validation=False)
                .create(company_values)
            )
            identity.action_link_central_company(company.id)
            self._notify_identity_channels(identity)
            created = True
        return {
            "schema_version": SCHEMA_VERSION,
            "created": created,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def search_partner_companies(
        self, channel_id, expected_partner_id, query, limit=12, relation_kind="primary"
    ):
        channel, _member = self._authorized_channel(channel_id)
        _identity, person = self._linked_person_for_company(
            channel, expected_partner_id
        )
        self._check_partner_company_relation_kind(channel, relation_kind)
        if (
            (relation_kind == "primary" and person.parent_id)
            or not isinstance(query, str)
            or len(query.strip()) < 2
        ):
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        items = self._search_partner_company_items(channel, query, limit)
        if relation_kind == "secondary":
            existing_ids = set(
                person.contact_center_secondary_company_ids.ids + person.parent_id.ids
            )
            items = [item for item in items if item["id"] not in existing_ids]
        return {"schema_version": SCHEMA_VERSION, "items": items}

    @api.model
    def _check_partner_company_relation_kind(self, channel, relation_kind):
        if relation_kind not in ("primary", "secondary"):
            raise ValidationError(
                _("Select a primary or secondary company relationship.")
            )
        if (
            relation_kind == "secondary"
            and not channel.contact_center_company_id.contact_center_secondary_companies_enabled
        ):
            raise ValidationError(
                _(
                    "Secondary company relationships are disabled in Contact Center settings."
                )
            )

    @api.model
    def link_partner_company(
        self,
        channel_id,
        expected_partner_id,
        company_partner_id,
        relation_kind="primary",
    ):
        channel, _member = self._authorized_channel(channel_id)
        self._check_partner_company_relation_kind(channel, relation_kind)
        company_partner_id = self._positive_id(company_partner_id, _("company ID"))
        # Authorize the person before resolving the supplied company, then lock
        # company -> identity -> person. Core partner field synchronization uses
        # the same parent-before-child order, avoiding a C/P lock inversion.
        self._linked_person_for_company(channel, expected_partner_id)
        company = self._partner_company_for_channel(
            channel, company_partner_id, lock=True
        )
        identity, person = self._linked_person_for_company(
            channel, expected_partner_id, lock=True
        )
        if relation_kind == "secondary":
            if person.parent_id == company:
                raise ValidationError(_("This company is already the primary company."))
            if company not in person.contact_center_secondary_company_ids:
                person.sudo().write(
                    {"contact_center_secondary_company_ids": [(4, company.id)]}
                )
                self._notify_partner_identities(person)
        elif person.parent_id and person.parent_id.id != company_partner_id:
            raise ValidationError(_("This contact already belongs to another company."))
        elif not person.parent_id:
            # This is the standard Odoo person -> commercial entity relation.
            # The narrow sudo is intentional: agents have partner read access,
            # while the channel, person and target company were checked above.
            person.sudo().write(
                {
                    "parent_id": company.id,
                    "contact_center_secondary_company_ids": [(3, company.id)],
                }
            )
            person.invalidate_recordset(["parent_id", "display_name"])
            self._notify_partner_identities(person)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_partner_company(
        self, channel_id, expected_partner_id, values, relation_kind="primary"
    ):
        channel, _member = self._authorized_channel(channel_id)
        self._check_partner_company_relation_kind(channel, relation_kind)
        company_values = self._company_creation_values(channel, values)

        identity, person = self._linked_person_for_company(
            channel, expected_partner_id, lock=True
        )
        if relation_kind == "primary" and person.parent_id:
            raise ValidationError(_("This contact already belongs to a company."))
        company = (
            self.env["res.partner"]
            .sudo()
            .with_context(no_vat_validation=False)
            .create(company_values)
        )
        person.sudo().write(
            {"parent_id": company.id}
            if relation_kind == "primary"
            else {"contact_center_secondary_company_ids": [(4, company.id)]}
        )
        person.invalidate_recordset(["parent_id", "display_name"])
        self._notify_partner_identities(person)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def unlink_partner_company(
        self,
        channel_id,
        expected_partner_id,
        company_partner_id,
        relation_kind="primary",
    ):
        """Remove just the selected relationship, keeping the person and documents."""
        channel, _member = self._authorized_channel(channel_id)
        if relation_kind not in ("primary", "secondary"):
            raise ValidationError(
                _("Select a primary or secondary company relationship.")
            )
        company_partner_id = self._positive_id(company_partner_id, _("company ID"))
        self._linked_person_for_company(channel, expected_partner_id)
        # The selected company must still be readable, including an archived
        # company whose existing relationship the operator needs to remove.
        company = (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", company_partner_id),
                    ("is_company", "=", True),
                    ("company_id", "in", [False, channel.contact_center_company_id.id]),
                ],
                limit=1,
            )
        )
        if not company:
            raise ValidationError(_("The selected company is not available."))
        self.env.cr.execute(
            "SELECT id FROM res_partner WHERE id = %s FOR UPDATE", [company.id]
        )
        identity, person = self._linked_person_for_company(
            channel, expected_partner_id, lock=True
        )
        if relation_kind == "primary":
            if person.parent_id and person.parent_id.id != company_partner_id:
                raise ValidationError(
                    _("The primary company changed. Reopen the company editor.")
                )
            if person.parent_id:
                person.sudo().write({"parent_id": False})
        elif company in person.contact_center_secondary_company_ids:
            person.sudo().write(
                {"contact_center_secondary_company_ids": [(3, company.id)]}
            )
        self._notify_partner_identities(person)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def unlink_partner(self, channel_id, expected_partner_id=False):
        channel, _member = self._authorized_channel(channel_id)
        if expected_partner_id in (False, None, ""):
            raise ValidationError(_("The expected linked contact ID is required."))
        expected_partner_id = self._positive_id(expected_partner_id, _("contact ID"))
        identity = self._identity_for_channel(channel)
        if identity.action_unlink_partner(expected_partner_id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def rename_guest(self, channel_id, name):
        channel, _member = self._authorized_channel(channel_id)
        identity = self._identity_for_channel(channel)
        identity.action_rename_guest(name)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }
