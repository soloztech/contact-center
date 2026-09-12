from odoo import _, api, models
from odoo.exceptions import ValidationError


class ContactCenterUiApi(models.AbstractModel):
    """Audio transcription projection and queueing for the operational UI."""

    _inherit = "contact.center.ui.api"

    def _serialize_message(self, message, *args, **kwargs):
        result = super()._serialize_message(message, *args, **kwargs)
        result["transcriptions"] = []
        if result["is_deleted"] and not result["deleted_content_visible"]:
            return result
        media_ids = [item["id"] for item in result["media"] if item["kind"] == "audio"]
        if not media_ids:
            return result
        # Keep the extension under the same conversation and media access rules
        # as the player. Never elevate the timeline projection to read text.
        self._authorized_channel(message.res_id)
        media_items = self.env["contact.center.media.binding"].browse(media_ids)
        media_items.check_access_rights("read")
        media_items.check_access_rule("read")
        result["transcriptions"] = [
            media._transcription_descriptor() for media in media_items
        ]
        return result

    @api.model
    def request_transcription(self, media_id):
        self._application()._check_agent()
        media = (
            self.env["contact.center.media.binding"]
            .browse(self._positive_id(media_id, _("media ID")))
            .exists()
        )
        if not media:
            raise ValidationError(_("The audio does not exist."))
        media.check_access_rights("read")
        media.check_access_rule("read")
        self._authorized_channel(
            media.message_binding_id.channel_binding_id.channel_id.id
        )
        media.action_request_transcription()
        return media._transcription_descriptor()
