import datetime
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.contracts import PRIVATE_MEDIA_RETENTION_HOURS
from ..services.tokens import CONTACT_CENTER_META_INTERNAL_TOKEN

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ITEM_KEY_PATTERN = re.compile(r"^entry:[0-9]{1,3}:messaging:[0-9]{1,3}$")
_MUTABLE_FIELDS = {"download_url", "state", "consumed_at"}
_BINDING_FIELDS = {"provider_connection_id", "inbox_event_id"}


def _internal(recordset):
    return recordset.env.context.get("contact_center_meta_internal") is (
        CONTACT_CENTER_META_INTERNAL_TOKEN
    )


class ContactCenterMetaMediaLocator(models.Model):
    """Short-lived vault for signed Meta CDN URLs.

    No ACL is declared. The webhook consumer, adapter and cleanup cron enter through
    ``sudo`` plus an in-process capability token. Shared ledgers and DTOs retain only
    the opaque reference.
    """

    _name = "contact.center.meta.media.locator"
    _description = "Contact Center Meta Private Media Locator"
    _order = "create_date, id"

    reference = fields.Char(
        required=True, size=64, index=True, copy=False, readonly=True
    )
    meta_delivery_id = fields.Many2one(
        "meta.webhook.delivery",
        required=True,
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    meta_item_key = fields.Char(
        required=True,
        size=128,
        index=True,
        copy=False,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="meta_delivery_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        index=True,
        ondelete="restrict",
        readonly=True,
    )
    sequence = fields.Integer(required=True, index=True, copy=False, readonly=True)
    slot = fields.Char(required=True, size=64, copy=False, readonly=True)
    media_type = fields.Char(size=64, copy=False, readonly=True)
    download_url = fields.Text(copy=False)
    url_sha256 = fields.Char(required=True, size=64, copy=False, readonly=True)
    purge_after = fields.Datetime(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: fields.Datetime.now()
        + datetime.timedelta(hours=PRIVATE_MEDIA_RETENTION_HOURS),
        help="Local retention ceiling for the signed provider URL.",
    )
    state = fields.Selection(
        [
            ("active", "Active"),
            ("consumed", "Consumed"),
            ("discarded", "Discarded"),
            ("expired", "Expired Locally"),
        ],
        required=True,
        default="active",
        index=True,
        copy=False,
    )
    consumed_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        (
            "reference_unique",
            "unique(reference)",
            "This Meta private media locator already exists.",
        ),
        (
            "delivery_item_slot_unique",
            "unique(meta_delivery_id, meta_item_key, slot)",
            "This Meta delivery media slot already has a locator.",
        ),
        (
            "binding_complete",
            "check((provider_connection_id is null and inbox_event_id is null) "
            "or (provider_connection_id is not null and inbox_event_id is not null))",
            "A Meta private locator binding must be complete.",
        ),
        (
            "sequence_nonnegative",
            "check(sequence >= 0)",
            "Meta media locator sequence cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("Meta private media locators are created internally."))
        for values in vals_list:
            if not _SHA256_PATTERN.fullmatch(values.get("reference") or ""):
                raise ValidationError(_("Meta private locator reference is invalid."))
            if not _SHA256_PATTERN.fullmatch(values.get("url_sha256") or ""):
                raise ValidationError(_("Meta private locator URL digest is invalid."))
            if not values.get("download_url"):
                raise ValidationError(_("Meta private locator URL is required."))
            if not values.get("meta_delivery_id") or not _ITEM_KEY_PATTERN.fullmatch(
                values.get("meta_item_key") or ""
            ):
                raise ValidationError(_("Meta private locator lineage is invalid."))
        return super().create(vals_list)

    def write(self, values):
        if not _internal(self):
            raise AccessError(_("Meta private media locators are managed internally."))
        changed = set(values)
        if changed and changed.issubset(_BINDING_FIELDS):
            if changed != _BINDING_FIELDS or not all(
                values.get(field_name) for field_name in _BINDING_FIELDS
            ):
                raise ValidationError(_("Meta private locator binding is invalid."))
            for locator in self:
                for field_name in _BINDING_FIELDS:
                    current = locator[field_name]
                    if current and current.id != values[field_name]:
                        raise ValidationError(
                            _("Meta private locator binding is immutable.")
                        )
            return super().write(values)
        if changed - _MUTABLE_FIELDS:
            raise ValidationError(_("Meta private locator evidence is immutable."))
        if values.get("download_url"):
            raise ValidationError(_("A cleared Meta private URL cannot be restored."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise ValidationError(_("Meta private locator evidence cannot be deleted."))

    @api.model
    def _register_delivery_candidates(self, delivery, candidates):
        delivery.ensure_one()
        if not _internal(self):
            raise AccessError(_("Meta private media locators are created internally."))
        result = self.browse()
        for candidate in candidates:
            values = dict(
                candidate,
                meta_delivery_id=delivery.id,
                meta_item_key=candidate.get("meta_item_key"),
            )
            existing = self.sudo().search(
                [("reference", "=", values.get("reference"))], limit=1
            )
            if existing:
                immutable = {
                    "meta_delivery_id": delivery.id,
                    "meta_item_key": values.get("meta_item_key"),
                    "sequence": values.get("sequence"),
                    "slot": values.get("slot"),
                    "url_sha256": values.get("url_sha256"),
                }
                if any(
                    (
                        existing[field_name].id
                        if field_name == "meta_delivery_id"
                        else existing[field_name]
                    )
                    != expected
                    for field_name, expected in immutable.items()
                ):
                    raise ValidationError(
                        _("Meta private locator replay is inconsistent.")
                    )
                result |= existing
                continue
            result |= self.create(values)
        return result

    @api.model
    def _bind_item(self, item, connection, inbox):
        item.ensure_one()
        connection.ensure_one()
        inbox.ensure_one()
        if not _internal(self):
            raise AccessError(_("Meta private media locators are bound internally."))
        locators = self.sudo().search(
            [
                ("meta_delivery_id", "=", item.delivery_id.id),
                ("meta_item_key", "=", item.item_key),
            ],
            order="id",
        )
        if not locators:
            return locators
        if (
            item.kind != "messaging"
            or inbox.provider_connection_id.id != connection.id
            or inbox.company_id.id != item.company_id.id
        ):
            raise ValidationError(_("Meta private locator binding is out of scope."))
        if not self._inbox_matches_item(item, inbox):
            locators.with_context(
                contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
            )._finalize(succeeded=False)
            return locators
        locators.flush_recordset(["provider_connection_id", "inbox_event_id"])
        self.env.cr.execute(
            "SELECT id, provider_connection_id, inbox_event_id "
            "FROM contact_center_meta_media_locator WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            [tuple(locators.ids)],
        )
        observed = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        unbound_ids = []
        for locator in locators:
            current_connection_id, current_inbox_id = observed[locator.id]
            wrong_connection = current_connection_id not in (None, connection.id)
            wrong_inbox = current_inbox_id not in (None, inbox.id)
            if wrong_connection or wrong_inbox:
                raise ValidationError(_("Meta private locator binding is immutable."))
            if not self._item_contains_reference(item, locator):
                raise ValidationError(
                    _("Meta private locator is outside the message item.")
                )
            if current_connection_id is None:
                unbound_ids.append(locator.id)
        if unbound_ids:
            locators.browse(unbound_ids).with_context(
                contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
            ).write(
                {
                    "provider_connection_id": connection.id,
                    "inbox_event_id": inbox.id,
                }
            )
        return locators

    @api.model
    def _item_contains_reference(self, item, locator):
        try:
            message = item.payload_json["messaging"]["message"]
            if locator.slot == "story":
                payload = message["reply_to"]["story"]
            elif locator.slot.startswith("attachment:"):
                attachment_index = int(locator.slot.split(":", 1)[1])
                payload = message["attachments"][attachment_index]["payload"]
            else:
                return False
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return False
        return payload.get("private_locator_ref") == locator.reference

    @api.model
    def _inbox_matches_item(self, item, inbox):
        metadata = inbox.metadata_json
        return bool(
            isinstance(metadata, dict)
            and metadata.get("technical_ledger") == "meta_webhook_base"
            and metadata.get("meta_delivery_ref") == item.delivery_id.public_ref
            and metadata.get("meta_item_key") == item.item_key
            and metadata.get("meta_item_id") == item.id
        )

    @api.model
    def _resolve_for_download(self, connection, reference):
        connection.ensure_one()
        if not _internal(self):
            raise AccessError(_("Meta private media locators are resolved internally."))
        if not _SHA256_PATTERN.fullmatch(reference or ""):
            raise ValidationError(_("Meta private locator reference is invalid."))
        locator = self.sudo().search([("reference", "=", reference)], limit=1)
        if not locator or locator.state != "active" or not locator.download_url:
            raise ValidationError(_("Meta private media locator is unavailable."))
        asset = connection.sudo().meta_webhook_asset_id
        page = asset.page_id
        endpoint = page.endpoint_id
        app = page.app_id
        if (
            not asset
            or not asset.active
            or not page.active
            or not endpoint.active
            or not app.active
            or locator.meta_delivery_id.app_id.id != app.id
            or locator.meta_delivery_id.endpoint_id.id != endpoint.id
            or locator.provider_connection_id.id != connection.id
            or not locator.inbox_event_id
            or locator.inbox_event_id.provider_connection_id.id != connection.id
            or locator.inbox_event_id.company_id.id != locator.company_id.id
            or not (locator.slot.startswith("attachment:") or locator.slot == "story")
        ):
            raise ValidationError(
                _("Meta private locator is outside this routed message.")
            )
        item = (
            self.env["meta.webhook.item"]
            .sudo()
            .search(
                [
                    ("delivery_id", "=", locator.meta_delivery_id.id),
                    ("item_key", "=", locator.meta_item_key),
                    ("kind", "=", "messaging"),
                ],
                limit=1,
            )
        )
        if (
            not item
            or not self._inbox_matches_item(item, locator.inbox_event_id)
            or not self._item_contains_reference(item, locator)
        ):
            raise ValidationError(
                _("Meta private locator is outside this routed message.")
            )
        return locator

    def _finalize(self, *, succeeded):
        if not _internal(self):
            raise AccessError(_("Meta private media locators are managed internally."))
        now = fields.Datetime.now()
        for locator in self.filtered(lambda item: item.state == "active"):
            locator.write(
                {
                    "download_url": False,
                    "state": "consumed" if succeeded else "discarded",
                    "consumed_at": now,
                }
            )
        return True

    @api.model
    def _cron_purge_private_urls(self, limit=500):
        limit = max(1, min(int(limit or 500), 5000))
        locators = self.sudo().search(
            [
                ("state", "=", "active"),
                ("download_url", "!=", False),
                ("purge_after", "<=", fields.Datetime.now()),
            ],
            order="purge_after, id",
            limit=limit,
        )
        internal = locators.with_context(
            contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
        )
        if internal:
            internal.write(
                {
                    "download_url": False,
                    "state": "expired",
                    "consumed_at": fields.Datetime.now(),
                }
            )
        return len(internal)
