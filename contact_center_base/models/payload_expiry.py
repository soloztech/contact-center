import datetime

from odoo import api, fields, models

_RETENTION_PARAMETER = "contact_center.payload_retention_days"


class ContactCenterInboxPayloadExpiry(models.Model):
    _inherit = "contact.center.inbox.event"

    def init(self):
        """Keep the expiry pass on the still-expirable receipts only."""

        result = super().init()
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS cc_inbox_payload_expirable_idx
                ON contact_center_inbox_event (processed_at)
             WHERE state IN ('done', 'unsupported')
               AND metadata_json ->> 'content_erased' IS NULL
            """
        )
        return result

    @api.model
    def _cron_expire_payloads(self, limit=2000):
        """Erase terminal webhook payloads by age; keep the dedupe receipt."""

        parameters = self.env["ir.config_parameter"].sudo()
        try:
            days = int(parameters.get_param(_RETENTION_PARAMETER, -1))
            cutoff = fields.Datetime.now() - datetime.timedelta(days=days)
        except (TypeError, ValueError, OverflowError):
            # Not a number, or a window no date can represent: stay off.
            return 0
        if days < 0:
            return 0
        self.flush_model()
        self.env.cr.execute(
            """
            UPDATE contact_center_inbox_event
               SET raw_envelope_json = '{"content_erased": true}'::jsonb,
                   normalized_dto_json = NULL,
                   metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                       || '{"content_erased": true,
                            "reason": "payload_expired"}'::jsonb,
                   write_date = (now() AT TIME ZONE 'UTC')
             WHERE id IN (
                    SELECT id
                      FROM contact_center_inbox_event
                     WHERE state IN ('done', 'unsupported')
                       AND metadata_json ->> 'content_erased' IS NULL
                       AND metadata_json -> 'attribution_capture_failure' IS NULL
                       AND processed_at <= %s
                       AND (%s OR id = ANY(%s))
                     ORDER BY processed_at
                     LIMIT %s
                       FOR UPDATE SKIP LOCKED)
            """,
            [cutoff, not self, self.ids, max(1, int(limit))],
        )
        expired = self.env.cr.rowcount
        self.invalidate_model(
            ["raw_envelope_json", "normalized_dto_json", "metadata_json", "write_date"]
        )
        return expired

    def write(self, values):
        result = super().write(values)
        if self and values.get("state") in ("done", "unsupported"):
            parameters = self.env["ir.config_parameter"].sudo()
            try:
                immediate = int(parameters.get_param(_RETENTION_PARAMETER, -1)) == 0
            except (TypeError, ValueError):
                immediate = False
            if immediate:
                self._cron_expire_payloads(limit=len(self))
        return result
