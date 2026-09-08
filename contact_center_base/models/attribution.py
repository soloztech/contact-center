import datetime
import hashlib
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.dto import AttributionDTO, DTOValidationError, EventDTO
from ..services.tokens import (
    CONTACT_CENTER_ATTRIBUTION_TOKEN,
    CONTACT_CENTER_DELETION_TOKEN,
)

_TOUCHPOINT_TYPES = [
    ("paid_ad_click", "Paid ad click"),
    ("paid_ad_signal", "Paid ad signal"),
    ("entry_point", "Entry point"),
    ("organic_link", "Organic link"),
    ("unknown", "Unknown"),
]
_EVIDENCE_LEVELS = [
    ("provider_asserted", "Provider asserted"),
    ("provider_asserted_non_paid", "Provider asserted non-paid"),
    ("provider_hint", "Provider hint"),
    ("observed", "Observed"),
    ("derived", "Derived"),
]
_MONOTONIC_FIELDS = {
    "message_binding_id",
    "channel_binding_id",
    "identity_id",
    "network",
    "evidence_level",
    "source_platform",
    "source_type",
    "source_url",
    "entry_point_source",
    "entry_point_app",
    "entry_point_delay_seconds",
    "entry_point_external_source",
    "entry_point_external_medium",
    "conversion_source",
    "conversion_delay_seconds",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "creative_media_type",
    "show_ad_attribution",
    "always_show_ad_attribution",
    "provider_extensions_json",
}
_CRITICAL_FIELDS = {
    "message_binding_id",
    "channel_binding_id",
    "identity_id",
    "network",
    "evidence_level",
    "source_platform",
    "source_type",
    "source_url",
    "entry_point_source",
    "entry_point_app",
    "entry_point_external_source",
    "entry_point_external_medium",
    "conversion_source",
    "utm_source",
    "utm_medium",
    "utm_campaign",
}
_RECONCILIATION_RETRY_CEILING = 3
_ACTIVE_QUEUE_JOB_STATES = (
    "pending",
    "enqueued",
    "started",
    "wait_dependencies",
)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_datetime(event):
    value = event.occurred_at
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


class ContactCenterAttributionTouchpoint(models.Model):
    _name = "contact.center.attribution.touchpoint"
    _description = "Contact Center Attribution Touchpoint"
    _order = "occurred_at desc, id desc"
    _rec_name = "public_ref"
    _check_company_auto = True

    def _default_public_ref(self):
        return str(uuid.uuid4())

    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    public_ref = fields.Char(
        required=True,
        default=_default_public_ref,
        size=36,
        index=True,
        copy=False,
        readonly=True,
    )
    account_id = fields.Many2one(
        "contact.center.account",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        string="First Evidence",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    evidence_inbox_event_ids = fields.Many2many(
        "contact.center.inbox.event",
        "contact_center_attribution_inbox_rel",
        "touchpoint_id",
        "inbox_event_id",
        string="Evidence Events",
        copy=False,
    )
    message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        index=True,
        ondelete="restrict",
        check_company=True,
        copy=False,
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        index=True,
        ondelete="restrict",
        check_company=True,
        copy=False,
    )
    identity_id = fields.Many2one(
        "contact.center.identity",
        index=True,
        ondelete="restrict",
        check_company=True,
        copy=False,
    )
    occurred_at = fields.Datetime(required=True, index=True, readonly=True)
    captured_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
        readonly=True,
    )
    conversation_ref = fields.Char(required=True, index=True, readonly=True)
    conversation_address_fingerprint = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    source_key_kind = fields.Selection(
        [("message", "Provider message"), ("event", "Provider event")],
        required=True,
        readonly=True,
    )
    source_external_key = fields.Char(required=True, index=True, readonly=True)
    external_message_id = fields.Char(index=True, readonly=True)
    canonical_key = fields.Char(required=True, size=64, index=True, readonly=True)
    attribution_fingerprint = fields.Char(
        required=True, size=64, index=True, readonly=True
    )
    touchpoint_type = fields.Selection(
        _TOUCHPOINT_TYPES, required=True, index=True, readonly=True
    )
    evidence_level = fields.Selection(
        _EVIDENCE_LEVELS, required=True, index=True, readonly=True
    )
    network = fields.Char(required=True, index=True, readonly=True)
    source_platform = fields.Char(index=True, readonly=True)
    source_type = fields.Char(index=True, readonly=True)
    source_url = fields.Char(readonly=True)
    entry_point_source = fields.Char(index=True, readonly=True)
    entry_point_app = fields.Char(index=True, readonly=True)
    entry_point_delay_seconds = fields.Integer(readonly=True)
    entry_point_external_source = fields.Char(index=True, readonly=True)
    entry_point_external_medium = fields.Char(index=True, readonly=True)
    conversion_source = fields.Char(index=True, readonly=True)
    conversion_delay_seconds = fields.Integer(readonly=True)
    utm_source = fields.Char(index=True, readonly=True)
    utm_medium = fields.Char(index=True, readonly=True)
    utm_campaign = fields.Char(index=True, readonly=True)
    utm_content = fields.Char(readonly=True)
    utm_term = fields.Char(readonly=True)
    creative_media_type = fields.Char(readonly=True)
    show_ad_attribution = fields.Boolean(readonly=True)
    always_show_ad_attribution = fields.Boolean(readonly=True)
    provider_extensions_json = fields.Json(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    enrichment_state = fields.Selection(
        [("initial", "Initial"), ("enriched", "Enriched")],
        required=True,
        default="initial",
        index=True,
        readonly=True,
    )
    conflict_state = fields.Selection(
        [("clean", "Clean"), ("conflict", "Conflict")],
        required=True,
        default="clean",
        index=True,
        readonly=True,
    )
    identifier_ids = fields.One2many(
        "contact.center.attribution.identifier",
        "touchpoint_id",
        string="External Identifiers",
        readonly=True,
    )

    _sql_constraints = [
        (
            "connection_canonical_key_unique",
            "unique(provider_connection_id, canonical_key)",
            "This attribution touchpoint already exists.",
        ),
        (
            "public_ref_unique",
            "unique(public_ref)",
            "The attribution public reference must be unique.",
        ),
        (
            "conversation_fingerprint_sha256",
            "check(char_length(conversation_address_fingerprint) = 64)",
            "The conversation fingerprint must be a SHA-256 digest.",
        ),
        (
            "attribution_fingerprint_sha256",
            "check(char_length(attribution_fingerprint) = 64)",
            "The attribution fingerprint must be a SHA-256 digest.",
        ),
        (
            "canonical_key_sha256",
            "check(char_length(canonical_key) = 64)",
            "The canonical attribution key must be a SHA-256 digest.",
        ),
        (
            "entry_delay_nonnegative",
            "check(entry_point_delay_seconds IS NULL OR entry_point_delay_seconds >= 0)",
            "The entry-point delay cannot be negative.",
        ),
        (
            "conversion_delay_nonnegative",
            "check(conversion_delay_seconds IS NULL OR conversion_delay_seconds >= 0)",
            "The conversion delay cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_attribution_token")
            is not CONTACT_CENTER_ATTRIBUTION_TOKEN
        ):
            raise AccessError(
                _(
                    "Attribution touchpoints are created only by the application service."
                )
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("contact_center_attribution_token")
            is not CONTACT_CENTER_ATTRIBUTION_TOKEN
        ):
            raise AccessError(_("Attribution touchpoints cannot be edited manually."))
        allowed = _MONOTONIC_FIELDS | {
            "evidence_inbox_event_ids",
            "enrichment_state",
            "conflict_state",
        }
        if set(values) - allowed:
            raise AccessError(
                _("Immutable attribution evidence cannot be overwritten.")
            )
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        """Keep attribution evidence append-only, including for administrators."""

        raise AccessError(_("Attribution touchpoints cannot be deleted."))

    @api.model
    def _contact_center_prepare_conversation_deletion(
        self, channel, bindings, messages, inbox_events
    ):
        """Keep acquisition evidence while releasing deleted chat projections."""
        if (
            self.env.context.get("contact_center_deletion_token")
            is not CONTACT_CENTER_DELETION_TOKEN
        ):
            raise AccessError(
                _("Conversation deletion requires the application service.")
            )
        touchpoints = self.sudo().search(
            [
                ("account_id", "in", bindings.account_id.ids),
                "|",
                ("channel_binding_id", "in", bindings.ids),
                ("message_binding_id.message_id", "in", messages.ids),
            ]
        )
        # These optional projection links are not attribution evidence. Avoid a
        # new Marketing revision merely because an operator removed chat history.
        touchpoints.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).write({"channel_binding_id": False, "message_binding_id": False})
        return True

    @api.constrains(
        "company_id",
        "account_id",
        "provider_connection_id",
        "inbox_event_id",
        "message_binding_id",
        "channel_binding_id",
        "identity_id",
    )
    def _check_scope(self):
        for touchpoint in self:
            company = touchpoint.company_id
            if touchpoint.account_id.company_id != company:
                raise ValidationError(
                    _("The attribution account belongs to another company.")
                )
            if (
                touchpoint.provider_connection_id.account_id != touchpoint.account_id
                or touchpoint.inbox_event_id.provider_connection_id
                != touchpoint.provider_connection_id
            ):
                raise ValidationError(
                    _("Attribution evidence must use one provider connection.")
                )
            if touchpoint.message_binding_id and (
                touchpoint.message_binding_id.provider_connection_id
                != touchpoint.provider_connection_id
            ):
                raise ValidationError(
                    _("The attributed message belongs to another connection.")
                )
            if (
                touchpoint.message_binding_id
                and touchpoint.channel_binding_id
                and touchpoint.message_binding_id.channel_binding_id
                != touchpoint.channel_binding_id
            ):
                raise ValidationError(
                    _("The attributed message and conversation do not match.")
                )
            if touchpoint.channel_binding_id and (
                touchpoint.channel_binding_id.account_id != touchpoint.account_id
            ):
                raise ValidationError(
                    _("The attributed conversation belongs to another account.")
                )
            if touchpoint.identity_id and touchpoint.identity_id.company_id != company:
                raise ValidationError(
                    _("The attributed identity belongs to another company.")
                )
            if (
                touchpoint.identity_id
                and touchpoint.channel_binding_id
                and (
                    touchpoint.channel_binding_id.conversation_type != "direct"
                    or touchpoint.channel_binding_id.identity_id
                    != touchpoint.identity_id
                )
            ):
                raise ValidationError(
                    _("The attributed identity and conversation do not match.")
                )

    @api.model
    def _conversation_fingerprint(self, event):
        addresses = sorted(
            {
                (address.namespace, address.value_normalized)
                for address in event.conversation.addresses
            }
        )
        evidence = addresses or [("conversation_ref", event.conversation_ref)]
        return _sha256(_canonical_json(evidence))

    @api.model
    def _source_key(self, event):
        external_message_id = event.message.external_message_id if event.message else ""
        if external_message_id:
            return "message", external_message_id
        return "event", event.event_id

    @api.model
    def _attribution_fingerprint(self, attribution):
        values = attribution.to_dict()
        creative = dict(values.get("creative") or {})
        # Presentation copy and remote preview URLs are intentionally not canonical.
        for key in ("title", "body", "greeting", "media_url", "thumbnail_url"):
            creative.pop(key, None)
        values["creative"] = creative
        return _sha256(_canonical_json(values))

    @api.model
    def _canonical_key(self, connection, event, attribution, conversation_fingerprint):
        source_kind, source_key = self._source_key(event)
        values = {
            "connection_id": connection.id,
            "source_kind": source_kind,
            "source_key": source_key,
            "touchpoint_type": attribution.touchpoint_type,
        }
        if source_kind == "event":
            values["conversation_address_fingerprint"] = conversation_fingerprint
        return _sha256(_canonical_json(values))

    @api.model
    def _projection_records(self, connection, event, projection=None):
        message_binding = self.env["contact.center.message.binding"]
        channel_binding = self.env["contact.center.channel.binding"]
        identity = self.env["contact.center.identity"]
        if projection and getattr(projection, "_name", "") == "mail.message":
            message_binding = (
                self.env["contact.center.message.binding"]
                .sudo()
                .search([("message_id", "=", projection.id)], limit=1)
            )
        elif projection and getattr(projection, "_name", "") == "mail.channel":
            candidates = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("channel_id", "=", projection.id),
                        ("account_id", "=", connection.account_id.id),
                        ("active", "=", True),
                        ("merged_into_id", "=", False),
                    ],
                    limit=2,
                )
            )
            if len(candidates) == 1:
                channel_binding = candidates
        external_message_id = event.message.external_message_id if event.message else ""
        if not message_binding and external_message_id:
            candidates = (
                self.env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("provider_connection_id", "=", connection.id),
                        ("external_message_id", "=", external_message_id),
                    ],
                    limit=2,
                )
            )
            if len(candidates) == 1:
                message_binding = candidates
        if message_binding:
            channel_binding = message_binding.channel_binding_id
        if not channel_binding:
            candidates = self.env["contact.center.channel.binding"].sudo()
            exact_keys = {
                (address.namespace, address.value_normalized)
                for address in event.conversation.addresses
            }
            if exact_keys:
                aliases = (
                    self.env["contact.center.channel.alias"]
                    .sudo()
                    .search(
                        [
                            ("account_id", "=", connection.account_id.id),
                            ("namespace", "in", list({key[0] for key in exact_keys})),
                            (
                                "value_normalized",
                                "in",
                                list({key[1] for key in exact_keys}),
                            ),
                            ("channel_binding_id.active", "=", True),
                            ("channel_binding_id.merged_into_id", "=", False),
                        ]
                    )
                )
                aliases = aliases.filtered(
                    lambda alias: (alias.namespace, alias.value_normalized)
                    in exact_keys
                )
                candidates |= aliases.mapped("channel_binding_id")
            if event.conversation_ref:
                candidates |= (
                    self.env["contact.center.channel.binding"]
                    .sudo()
                    .search(
                        [
                            ("account_id", "=", connection.account_id.id),
                            ("conversation_ref", "=", event.conversation_ref),
                            ("active", "=", True),
                            ("merged_into_id", "=", False),
                        ],
                        limit=2,
                    )
                )
            if len(candidates) == 1:
                channel_binding = candidates
        if channel_binding and channel_binding.conversation_type == "direct":
            identity = channel_binding.identity_id
        return message_binding, channel_binding, identity

    @api.model
    def _touchpoint_values(
        self,
        connection,
        event,
        inbox_event,
        attribution,
        conversation_fingerprint,
        canonical_key,
        projection=None,
    ):
        # Initial evidence capture runs before the canonical projection locks.
        # Do not resolve or write projection FKs in that phase: their PostgreSQL
        # referential-integrity checks would acquire implicit KEY SHARE locks on a
        # binding before mail.channel.  Link them later in _link_projection(), while
        # the application still holds channel -> binding locks.
        if projection is None:
            message_binding = self.env["contact.center.message.binding"]
            channel_binding = self.env["contact.center.channel.binding"]
            identity = self.env["contact.center.identity"]
        else:
            message_binding, channel_binding, identity = self._projection_records(
                connection, event, projection=projection
            )
        entry = attribution.entry_point
        utm = attribution.utm
        creative = attribution.creative
        source_kind, source_key = self._source_key(event)
        return {
            "company_id": connection.company_id.id,
            "account_id": connection.account_id.id,
            "provider_connection_id": connection.id,
            "inbox_event_id": inbox_event.id,
            "evidence_inbox_event_ids": [(6, 0, [inbox_event.id])],
            "message_binding_id": message_binding.id or False,
            "channel_binding_id": channel_binding.id or False,
            "identity_id": identity.id or False,
            "occurred_at": _event_datetime(event),
            "conversation_ref": event.conversation_ref,
            "conversation_address_fingerprint": conversation_fingerprint,
            "source_key_kind": source_kind,
            "source_external_key": source_key,
            "external_message_id": (
                event.message.external_message_id if event.message else False
            ),
            "canonical_key": canonical_key,
            "attribution_fingerprint": self._attribution_fingerprint(attribution),
            "touchpoint_type": attribution.touchpoint_type,
            "evidence_level": attribution.evidence_level,
            "network": attribution.network,
            "source_platform": attribution.source_platform or False,
            "source_type": attribution.source_type or False,
            "source_url": attribution.source_url or False,
            "entry_point_source": entry.get("source") or False,
            "entry_point_app": entry.get("app") or False,
            "entry_point_delay_seconds": entry.get("delay_seconds"),
            "entry_point_external_source": entry.get("external_source") or False,
            "entry_point_external_medium": entry.get("external_medium") or False,
            "conversion_source": entry.get("conversion_source") or False,
            "conversion_delay_seconds": entry.get("conversion_delay_seconds"),
            "utm_source": utm.get("source") or False,
            "utm_medium": utm.get("medium") or False,
            "utm_campaign": utm.get("campaign") or False,
            "utm_content": utm.get("content") or False,
            "utm_term": utm.get("term") or False,
            "creative_media_type": creative.get("media_type") or False,
            "show_ad_attribution": bool(attribution.flags.get("show_ad_attribution")),
            "always_show_ad_attribution": bool(
                attribution.flags.get("always_show_ad_attribution")
            ),
            "provider_extensions_json": attribution.provider_extensions or {},
        }

    def _merge_observation(self, values, attribution, inbox_event):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_attribution_touchpoint WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset(list(_MONOTONIC_FIELDS) + ["conflict_state"])
        updates = {
            "evidence_inbox_event_ids": [(4, inbox_event.id)],
        }
        enriched = False
        conflict = self.conflict_state == "conflict"
        for field_name in _MONOTONIC_FIELDS:
            incoming = values.get(field_name)
            current = self[field_name]
            current_value = current.id if getattr(current, "_name", False) else current
            incoming_value = incoming or False
            if incoming_value in (False, None, "", {}):
                continue
            if current_value in (False, None, "", {}):
                updates[field_name] = incoming
                enriched = True
            elif field_name in _CRITICAL_FIELDS and current_value != incoming_value:
                conflict = True
        if enriched:
            updates["enrichment_state"] = "enriched"
        if conflict:
            updates["conflict_state"] = "conflict"
        self.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
        ).write(updates)
        self._append_identifiers(
            attribution, inbox_event, observed_at=values.get("occurred_at")
        )
        return self

    def _append_identifiers(self, attribution, inbox_event, observed_at=None):
        self.ensure_one()
        identifier_model = self.env["contact.center.attribution.identifier"].sudo()
        existing = identifier_model.search([("touchpoint_id", "=", self.id)])
        existing_keys = {
            (item.namespace, item.role, item.comparison_hash) for item in existing
        }
        role_values = {}
        for item in existing:
            role_values.setdefault((item.namespace, item.role), set()).add(
                item.comparison_hash
            )
        conflict = False
        for identifier in attribution.external_identifiers:
            key = (identifier.namespace, identifier.role, identifier.comparison_hash)
            if key in existing_keys:
                continue
            if role_values.get((identifier.namespace, identifier.role)):
                conflict = True
            identifier_model.with_context(
                contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
            ).create(
                {
                    "touchpoint_id": self.id,
                    "inbox_event_id": inbox_event.id,
                    "namespace": identifier.namespace,
                    "role": identifier.role,
                    "value": identifier.value,
                    "comparison_hash": identifier.comparison_hash,
                    "source_field": identifier.source_field or False,
                    "source_provider": self.provider_connection_id.adapter_key,
                    "observed_at": observed_at or self.occurred_at,
                }
            )
            existing_keys.add(key)
            role_values.setdefault((identifier.namespace, identifier.role), set()).add(
                identifier.comparison_hash
            )
        if conflict and self.conflict_state != "conflict":
            self.with_context(
                contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
            ).write({"conflict_state": "conflict"})
        return True

    @api.model
    def _capture_event(self, connection, event, inbox_event, projection=None):
        connection.ensure_one()
        inbox_event.ensure_one()
        if not isinstance(event, EventDTO):
            raise ValidationError(_("Attribution capture requires an EventDTO."))
        if not event.attribution:
            return self.browse()
        if inbox_event.provider_connection_id != connection:
            raise ValidationError(
                _("Attribution evidence belongs to another provider connection.")
            )
        if (
            event.account_ref != connection.account_id.external_ref
            or event.connection_ref != connection.external_ref
            or event.platform != connection.account_id.platform
        ):
            raise ValidationError(
                _("Attribution event scope does not match the provider connection.")
            )
        conversation_fingerprint = self._conversation_fingerprint(event)
        result = self.browse()
        for attribution in event.attribution:
            if not isinstance(attribution, AttributionDTO):
                raise ValidationError(_("Invalid attribution touchpoint DTO."))
            canonical_key = self._canonical_key(
                connection, event, attribution, conversation_fingerprint
            )
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["contact_center:attribution:%s:%s" % (connection.id, canonical_key)],
            )
            touchpoint = self.sudo().search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("canonical_key", "=", canonical_key),
                ],
                limit=1,
            )
            values = self._touchpoint_values(
                connection,
                event,
                inbox_event,
                attribution,
                conversation_fingerprint,
                canonical_key,
                projection=projection,
            )
            if touchpoint:
                touchpoint._merge_observation(values, attribution, inbox_event)
            else:
                touchpoint = (
                    self.sudo()
                    .with_context(
                        contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
                    )
                    .create(values)
                )
                touchpoint._append_identifiers(
                    attribution, inbox_event, observed_at=values.get("occurred_at")
                )
            result |= touchpoint
        for channel in result.mapped("channel_binding_id.channel_id"):
            self.env["contact.center.application"]._notify_ui(
                channel, "attribution_updated", {}
            )
        return result

    def _link_projection(self, connection, event, projection=None):
        self = self.sudo()
        current_touchpoint_ids = set(self.ids)
        message_binding, channel_binding, identity = self._projection_records(
            connection, event, projection=projection
        )
        if channel_binding:
            # A standalone referral can arrive before the first message known to
            # this Odoo database.  Once the exact provider conversation exists,
            # link its still-unprojected touchpoints to the channel/identity only;
            # they remain event-sourced and never acquire a message binding.
            self |= self.sudo().search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("account_id", "=", connection.account_id.id),
                    (
                        "conversation_address_fingerprint",
                        "=",
                        self._conversation_fingerprint(event),
                    ),
                    ("channel_binding_id", "=", False),
                ]
            )
        if not self:
            return self
        values = {
            "message_binding_id": message_binding.id or False,
            "channel_binding_id": channel_binding.id or False,
            "identity_id": identity.id or False,
        }
        previously_linked_channel_ids = set(self.mapped("channel_binding_id").ids)
        for touchpoint in self:
            updates = {}
            conflict = touchpoint.conflict_state == "conflict"
            touchpoint_values = dict(values)
            if touchpoint.id not in current_touchpoint_ids:
                touchpoint_values["message_binding_id"] = False
            for field_name, incoming in touchpoint_values.items():
                if not incoming:
                    continue
                current = touchpoint[field_name]
                if not current:
                    updates[field_name] = incoming
                elif current.id != incoming:
                    conflict = True
            if updates:
                updates["enrichment_state"] = "enriched"
            if conflict:
                updates["conflict_state"] = "conflict"
            if updates:
                touchpoint.with_context(
                    contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN
                ).write(updates)
        if channel_binding and channel_binding.id not in previously_linked_channel_ids:
            self.env["contact.center.application"]._notify_ui(
                channel_binding.channel_id, "attribution_updated", {}
            )
        return self

    def _reconciliation_job_attempt(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if job_uuid:
            job = (
                self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
            )
            if job:
                return job.retry + 1
        return 1

    def _has_active_reconciliation_job(self):
        self.ensure_one()
        identity_key = "contact_center:attribution_reconcile:%s" % self.id
        return bool(
            self.env["queue.job"]
            .sudo()
            .search_count(
                [
                    ("identity_key", "=", identity_key),
                    ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
                ]
            )
        )

    def _enqueue_projection_reconciliation(self, eta=None):
        """Schedule a bounded post-commit repair for referral/message races."""

        for touchpoint in self.sudo().filtered(
            lambda item: item.source_key_kind == "event"
            and not item.channel_binding_id
            and not item._has_active_reconciliation_job()
        ):
            (
                touchpoint.with_company(touchpoint.company_id)
                .with_delay(
                    identity_key="contact_center:attribution_reconcile:%s"
                    % touchpoint.id,
                    max_retries=0,
                    priority=40,
                    description="Contact Center attribution reconciliation %s"
                    % touchpoint.id,
                    eta=eta,
                )
                ._job_reconcile_projection()
            )
        return True

    def _job_reconcile_projection(self):
        """Eventually link one standalone touchpoint without inventing a chat."""

        self.ensure_one()
        touchpoint = self.sudo().exists()
        if not touchpoint or touchpoint.channel_binding_id:
            return True
        inbox_event = touchpoint.inbox_event_id
        normalized = inbox_event.normalized_dto_json
        if not isinstance(normalized, dict):
            raise ValidationError(
                _("Attribution reconciliation requires a normalized source event.")
            )
        try:
            event = EventDTO.from_dict(normalized)
        except DTOValidationError as error:
            raise ValidationError(
                _("Attribution reconciliation source is invalid.")
            ) from error
        connection = touchpoint.provider_connection_id
        if (
            event.account_ref != connection.account_id.external_ref
            or event.connection_ref != connection.external_ref
            or event.platform != connection.account_id.platform
            or touchpoint.conversation_address_fingerprint
            != touchpoint._conversation_fingerprint(event)
        ):
            raise ValidationError(
                _("Attribution reconciliation source does not match its ledger.")
            )
        _message_binding, channel_binding, _identity = touchpoint._projection_records(
            connection, event
        )
        if not channel_binding:
            if touchpoint._reconciliation_job_attempt() < _RECONCILIATION_RETRY_CEILING:
                raise RetryableJobError(
                    "attribution conversation is not visible yet",
                    seconds=None,
                )
            # No conversation is a normal outcome: opening a referral link does not
            # necessarily lead to a message.  A later message still performs the same
            # exact-fingerprint reconciliation synchronously.
            return False
        if not channel_binding._contact_center_lock_identity_channel_binding():
            raise RetryableJobError(
                "attribution projection is waiting for canonical locks",
                seconds=None,
            )
        touchpoint.invalidate_recordset(["channel_binding_id", "identity_id"])
        if touchpoint.channel_binding_id:
            return True
        touchpoint._link_projection(
            connection,
            event,
            projection=channel_binding.channel_id,
        )
        touchpoint.invalidate_recordset(["channel_binding_id", "identity_id"])
        if not touchpoint.channel_binding_id:
            raise RetryableJobError(
                "attribution projection is waiting for canonical visibility",
                seconds=None,
            )
        return True

    @api.model
    def _safe_projection_for_binding(self, binding, limit=3, before_public_ref=None):
        binding.ensure_one()
        account = binding.account_id.sudo()
        if not account._contact_center_user_can_view_attribution():
            return {
                "enabled": False,
                "items": [],
                "has_more": False,
                "next_cursor": False,
            }
        limit = max(1, min(int(limit or 3), 20))
        domain = [
            ("channel_binding_id", "=", binding.id),
            ("account_id", "=", account.id),
            ("conflict_state", "=", "clean"),
        ]
        if before_public_ref:
            cursor = self.sudo().search(
                domain + [("public_ref", "=", before_public_ref)], limit=1
            )
            if not cursor:
                raise ValidationError(_("The attribution cursor is invalid."))
            domain += [
                "|",
                ("occurred_at", "<", cursor.occurred_at),
                "&",
                ("occurred_at", "=", cursor.occurred_at),
                ("id", "<", cursor.id),
            ]
        records = self.sudo().search(
            domain,
            order="occurred_at desc, id desc",
            limit=limit + 1,
        )
        has_more = len(records) > limit
        records = records[:limit]
        return {
            "enabled": True,
            "items": [
                {
                    "public_ref": item.public_ref,
                    "touchpoint_type": item.touchpoint_type,
                    "evidence_level": item.evidence_level,
                    "network": item.network,
                    "source_platform": item.source_platform or "",
                    "source_type": item.source_type or "",
                    "entry_point_source": item.entry_point_source or "",
                    "entry_point_app": item.entry_point_app or "",
                    "utm_source": item.utm_source or "",
                    "utm_medium": item.utm_medium or "",
                    "utm_campaign": item.utm_campaign or "",
                    "creative_media_type": item.creative_media_type or "",
                    "show_ad_attribution": bool(item.show_ad_attribution),
                    "occurred_at": fields.Datetime.to_string(item.occurred_at),
                }
                for item in records
            ],
            "has_more": has_more,
            "next_cursor": records[-1].public_ref if has_more and records else False,
        }


class ContactCenterAttributionIdentifier(models.Model):
    _name = "contact.center.attribution.identifier"
    _description = "Contact Center Attribution Identifier"
    _order = "touchpoint_id, namespace, role, id"

    touchpoint_id = fields.Many2one(
        "contact.center.attribution.touchpoint",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="touchpoint_id.company_id", store=True, readonly=True, index=True
    )
    inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        required=True,
        index=True,
        ondelete="restrict",
    )
    namespace = fields.Char(required=True, index=True, readonly=True)
    role = fields.Char(required=True, index=True, readonly=True)
    value = fields.Char(
        required=True,
        readonly=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    comparison_hash = fields.Char(required=True, size=64, index=True, readonly=True)
    source_field = fields.Char(readonly=True)
    source_provider = fields.Char(required=True, index=True, readonly=True)
    observed_at = fields.Datetime(required=True, index=True, readonly=True)

    _sql_constraints = [
        (
            "touchpoint_identifier_unique",
            "unique(touchpoint_id, namespace, role, comparison_hash)",
            "This attribution identifier was already observed.",
        ),
        (
            "comparison_hash_sha256",
            "check(char_length(comparison_hash) = 64)",
            "The attribution identifier hash must be a SHA-256 digest.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_attribution_token")
            is not CONTACT_CENTER_ATTRIBUTION_TOKEN
        ):
            raise AccessError(
                _(
                    "Attribution identifiers are created only by the application service."
                )
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        """External identifiers are immutable evidence."""

        raise AccessError(_("Attribution identifiers cannot be edited."))

    def unlink(self):  # pylint: disable=method-required-super
        """External identifiers are immutable evidence."""

        raise AccessError(_("Attribution identifiers cannot be deleted."))

    @api.constrains("touchpoint_id", "inbox_event_id")
    def _check_scope(self):
        for identifier in self:
            if (
                identifier.inbox_event_id.provider_connection_id
                != identifier.touchpoint_id.provider_connection_id
            ):
                raise ValidationError(
                    _("Attribution identifier evidence uses another connection.")
                )
