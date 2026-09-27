from odoo import api, fields, models

from ..services.contracts import WHATSAPP_CLOUD_ADAPTER_KEY


class ContactCenterWhatsAppCloudOutboxCommand(models.Model):
    _inherit = "contact.center.outbox.command"

    wac_reaction_message_id = fields.Char(
        string="Reaction Message ID",
        compute="_compute_wac_reaction_message_id",
        store=True,
        index=True,
        copy=False,
        help=(
            "WhatsApp message ID of a reaction sent through WhatsApp Cloud. Its "
            "statuses concern no local message and are acknowledged as handled."
        ),
    )

    @api.depends("command_type", "provider_response_json")
    def _compute_wac_reaction_message_id(self):
        for command in self:
            response = command.provider_response_json
            message_id = (
                response.get("message_id") if isinstance(response, dict) else ""
            )
            command.wac_reaction_message_id = (
                message_id
                if command.command_type == "react"
                and command.provider_connection_id.adapter_key
                == WHATSAPP_CLOUD_ADAPTER_KEY
                and isinstance(message_id, str)
                and message_id
                else False
            )

    @api.model
    def _wac_sent_reaction(self, connection, wamid):
        """Return the reaction command this route sent as ``wamid``, if any.

        A reaction is a message of its own: WhatsApp reports its statuses, but
        no local message binding holds its ID. Every connection of the phone
        number counts, so a reaction sent before a connection was replaced is
        still recognized.
        """

        if not isinstance(wamid, str) or not wamid or not connection:
            return self.browse()
        return (
            self.sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("wac_reaction_message_id", "=", wamid),
                    ("command_type", "=", "react"),
                    (
                        "provider_connection_id.wa_phone_number_id",
                        "=",
                        connection.wa_phone_number_id,
                    ),
                    (
                        "provider_connection_id.company_id",
                        "=",
                        connection.company_id.id,
                    ),
                ],
                limit=1,
            )
        )
