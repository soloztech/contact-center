from odoo import _, http
from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.http import content_disposition, request

from odoo.addons.contact_center_base.services.media import MEDIA_SIZE_LIMITS


class CustomerDocumentsController(http.Controller):
    def _document_response(self, content, filename, mimetype, *, inline=False):
        if len(content) > max(MEDIA_SIZE_LIMITS.values()):
            raise ValidationError(_("The document exceeds the media upload limit."))
        return request.make_response(
            content,
            headers=[
                ("Content-Type", mimetype),
                (
                    "Content-Disposition",
                    content_disposition(
                        filename, disposition_type="inline" if inline else "attachment"
                    ),
                ),
                ("Cache-Control", "private, no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", "default-src 'none'; sandbox"),
            ],
        )

    @http.route(
        "/contact_center/customer/<int:channel_id>/sale/<int:order_id>/pdf",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def sale_pdf(self, channel_id, order_id, **_kwargs):
        try:
            _channel, order = request.env[
                "contact.center.ui.api"
            ]._customer_sale_document(channel_id, order_id)
            content, report_format = order.env["ir.actions.report"]._render_qweb_pdf(
                "sale.action_report_saleorder", res_ids=order.ids
            )
            if report_format != "pdf" or not content.startswith(b"%PDF-"):
                raise ValidationError(_("The report did not produce a PDF document."))
            return self._document_response(
                content, "%s.pdf" % order.name, "application/pdf", inline=True
            )
        except (AccessError, MissingError, ValidationError):
            raise request.not_found() from None

    @http.route(
        "/contact_center/customer/<int:channel_id>/sale/<int:order_id>/"
        "attachment/<int:attachment_id>",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def sale_attachment(self, channel_id, order_id, attachment_id, **_kwargs):
        try:
            api = request.env["contact.center.ui.api"]
            _channel, order = api._customer_sale_document(channel_id, order_id)
            attachment = api._customer_sale_attachment(order, attachment_id)
            if attachment.file_size > max(MEDIA_SIZE_LIMITS.values()):
                raise ValidationError(_("The document exceeds the media upload limit."))
            return self._document_response(
                attachment.raw or b"",
                attachment.name,
                attachment.mimetype or "application/octet-stream",
            )
        except (AccessError, MissingError, ValidationError):
            raise request.not_found() from None
