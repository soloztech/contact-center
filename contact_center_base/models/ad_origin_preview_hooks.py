"""Wire ad presentation to attribution, UI authorization and content lifecycle."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_ADMIN = "contact_center_base.group_contact_center_admin"


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    ad_preview_enrichment_enabled = fields.Boolean(
        string="Complete Ad Previews from Marketing",
        default=False,
        groups=_ADMIN,
        help="Use an authorized Marketing reader to fill missing ad details. "
        "Received titles and text are preserved. No Marketing connection is created.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            any("ad_preview_enrichment_enabled" in values for values in vals_list)
            or "default_ad_preview_enrichment_enabled" in self.env.context
        ):
            self._check_ad_preview_admin()
        return super().create(vals_list)

    def write(self, values):
        if "ad_preview_enrichment_enabled" in values:
            self._check_ad_preview_admin()
        return super().write(values)

    def _check_ad_preview_admin(self):
        if not self.env.su and not self.env.user.has_group(_ADMIN):
            raise AccessError(_("Only administrators can configure ad previews."))

    def action_backfill_ad_previews(self):
        self.ensure_one()
        self._check_ad_preview_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        count = self.env["contact.center.attribution.preview"]._backfill_account(
            self, limit=100
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Ad previews"),
                "message": _(
                    "%(count)s eligible origins examined. Repeat to process the next batch.",
                    count=count,
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_retry_ad_previews(self):
        self.ensure_one()
        self._check_ad_preview_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        previews = (
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.id),
                    ("expired", "=", False),
                    ("expires_at", ">", fields.Datetime.now()),
                    "|",
                    "|",
                    "|",
                    ("title", "=", False),
                    ("body", "=", False),
                    ("source_public_url", "=", False),
                    ("thumbnail_attachment_id", "=", False),
                ],
                order="id",
                limit=100,
            )
        )
        count = previews._retry_enrichment()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Ad previews"),
                "message": _(
                    "%(count)s incomplete previews queued again.", count=count
                ),
                "type": "success",
                "sticky": False,
            },
        }


class ContactCenterAttributionTouchpoint(models.Model):
    _inherit = "contact.center.attribution.touchpoint"

    ad_preview_ids = fields.One2many(
        "contact.center.attribution.preview", "touchpoint_id", groups=_ADMIN
    )

    @api.model
    def _capture_event(self, connection, event, inbox_event, projection=None):
        result = super()._capture_event(
            connection, event, inbox_event, projection=projection
        )
        self._capture_ad_previews(result, connection, event)
        return result

    @api.model
    def _capture_ad_previews(self, touchpoints, connection, event, *, merge=False):
        records = {item.canonical_key: item for item in touchpoints}
        fingerprint = self._conversation_fingerprint(event)
        preview_model = self.env["contact.center.attribution.preview"]
        for attribution in event.attribution:
            point = records.get(
                self._canonical_key(connection, event, attribution, fingerprint)
            )
            if point:
                preview_model._capture(point, attribution.creative, merge=merge)
        return True

    def _link_projection(self, connection, event, projection=None):
        result = super()._link_projection(connection, event, projection=projection)
        self._capture_ad_previews(result, connection, event, merge=True)
        self.env["contact.center.attribution.preview"].sudo().search(
            [("touchpoint_id", "in", result.ids)]
        )._queue_work()
        return result

    @api.model
    def _safe_projection_for_binding(self, binding, limit=3, before_public_ref=None):
        result = super()._safe_projection_for_binding(
            binding, limit=limit, before_public_ref=before_public_ref
        )
        refs = [item["public_ref"] for item in result["items"]]
        previews = (
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search([("touchpoint_id.public_ref", "in", refs), ("expired", "=", False)])
        )
        by_ref = {item.touchpoint_id.public_ref: item for item in previews}
        for item in result["items"]:
            preview = by_ref.get(item["public_ref"])
            if preview:
                try:
                    item["ad_origin_preview"] = preview._descriptor()
                except (AccessError, ValidationError):
                    continue
        return result

    @api.model
    def _contact_center_prepare_conversation_deletion(
        self, channel, bindings, messages, inbox_events
    ):
        # The base method checks the deletion capability before releasing links.
        points = self.sudo().search(
            [
                ("account_id", "in", bindings.account_id.ids),
                "|",
                ("channel_binding_id", "in", bindings.ids),
                ("message_binding_id.message_id", "in", messages.ids),
            ]
        )
        previews = (
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search([("touchpoint_id", "in", points.ids)])
        )
        result = super()._contact_center_prepare_conversation_deletion(
            channel, bindings, messages, inbox_events
        )
        previews._expire()
        return result


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _serialize_message(self, message, *args, **kwargs):
        result = super()._serialize_message(message, *args, **kwargs)
        result["ad_origin_previews"] = []
        if result.get("is_deleted"):
            return result
        previews = (
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search(
                [
                    ("touchpoint_id.message_binding_id.message_id", "=", message.id),
                    (
                        "touchpoint_id.channel_binding_id.channel_id",
                        "=",
                        message.res_id,
                    ),
                    ("expired", "=", False),
                ],
                limit=8,
            )
        )
        for preview in previews:
            try:
                result["ad_origin_previews"].append(preview._descriptor())
            except (AccessError, ValidationError):
                continue
        return result


class ContactCenterMessageMutation(models.Model):
    _inherit = "contact.center.message.mutation"

    def _purge_redacted_operational_content(self, target):
        result = super()._purge_redacted_operational_content(target)
        self.env["contact.center.attribution.preview"].sudo().search(
            [("touchpoint_id.message_binding_id", "=", target.id)]
        )._expire()
        return result


class ContactCenterRetention(models.AbstractModel):
    _inherit = "contact.center.retention"

    def _jobs(self, records, messages):
        jobs = super()._jobs(records, messages)
        previews = (
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search(
                [("touchpoint_id.message_binding_id.message_id", "in", messages.ids)]
            )
        )
        if previews:
            extra = (
                self.env["queue.job"]
                .sudo()
                .search(
                    [
                        "|",
                        ("uuid", "in", previews.mapped("queue_job_uuid")),
                        ("identity_key", "in", [item._identity() for item in previews]),
                    ]
                )
            )
            if extra:
                self.env.cr.execute(
                    "SELECT id FROM queue_job WHERE id IN %s ORDER BY id FOR UPDATE",
                    [tuple(extra.ids)],
                )
                extra.invalidate_recordset()
            jobs |= extra
        return jobs

    def _retention_prepare_external_references(
        self, binding, messages, message_bindings, inbox_events
    ):
        result = super()._retention_prepare_external_references(
            binding, messages, message_bindings, inbox_events
        )
        if result is not False:
            self.env["contact.center.attribution.preview"].sudo().search(
                [("touchpoint_id.message_binding_id", "in", message_bindings.ids)]
            )._expire()
        return result


class ContactCenterAttributionPreview(models.Model):
    _inherit = "contact.center.attribution.preview"

    @api.model
    def _backfill_account(self, account, *, limit=100):
        account.ensure_one()
        domain = [
            ("account_id", "=", account.id),
            ("provider_connection_id.active", "=", True),
            ("channel_binding_id.active", "=", True),
            ("channel_binding_id.channel_id.active", "=", True),
            ("touchpoint_type", "in", ("paid_ad_click", "paid_ad_signal")),
            ("conflict_state", "=", "clean"),
            "|",
            ("source_key_kind", "!=", "message"),
            "&",
            ("message_binding_id.message_id", "!=", False),
            ("message_binding_id.message_state", "!=", "deleted"),
        ]
        if account.ad_preview_enrichment_enabled:
            # Missing-field predicates belong only to existing previews; an
            # origin with no preview must remain eligible on its own.
            domain += [
                "|",
                ("ad_preview_ids", "=", False),
                "&",
                "&",
                "&",
                ("ad_preview_ids.expired", "=", False),
                ("ad_preview_ids.expires_at", ">", fields.Datetime.now()),
                ("ad_preview_ids.enrichment_attempted", "=", False),
                "|",
                "|",
                "|",
                ("ad_preview_ids.title", "=", False),
                ("ad_preview_ids.body", "=", False),
                ("ad_preview_ids.source_public_url", "=", False),
                ("ad_preview_ids.thumbnail_ref", "=", False),
            ]
        else:
            domain.append(("ad_preview_ids", "=", False))
        points = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search(domain, order="id", limit=max(1, min(int(limit), 100)))
        )
        for point in points:
            creative = {}
            adapter = point.provider_connection_id.get_adapter()
            extractor = getattr(adapter, "ad_origin_preview_from_history", None)
            if extractor and point.inbox_event_id.raw_envelope_json:
                creative = extractor(
                    point.inbox_event_id.raw_envelope_json, point.source_external_key
                )
            preview = self._capture(point, creative)
            if preview:
                preview._queue_work()
        return len(points)
