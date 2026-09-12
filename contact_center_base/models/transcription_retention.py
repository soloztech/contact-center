from odoo import models


class ContactCenterRetention(models.AbstractModel):
    _inherit = "contact.center.retention"

    def _jobs(self, records, messages):
        jobs = super()._jobs(records, messages)
        media = self.env["contact.center.media.binding"]
        for recordset in records:
            if recordset._name == media._name:
                media |= recordset
        if media:
            extra = (
                self.env["queue.job"]
                .sudo()
                .search(
                    [
                        "|",
                        ("uuid", "in", media.mapped("transcription_queue_job_uuid")),
                        (
                            "identity_key",
                            "in",
                            [item._transcription_identity() for item in media],
                        ),
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
