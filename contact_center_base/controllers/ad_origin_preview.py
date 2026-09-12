from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request


class ContactCenterAdPreviewController(http.Controller):
    @http.route(
        "/contact_center/attribution/<string:public_ref>/thumbnail",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def thumbnail(self, public_ref):
        preview = (
            request.env["contact.center.attribution.preview"]
            .sudo()
            .search([("public_ref", "=", public_ref)], limit=1)
        )
        try:
            if not preview:
                raise NotFound()
            preview._authorize_read()
        except (AccessError, ValidationError):
            raise NotFound() from None
        attachment = preview.thumbnail_attachment_id
        if (
            not attachment
            or attachment.public
            or attachment.res_model != preview._name
            or attachment.res_id != preview.id
        ):
            raise NotFound()
        content = attachment.raw
        if not content:
            raise NotFound()
        return request.make_response(
            content,
            headers=[
                ("Content-Type", "image/jpeg"),
                ("Content-Length", str(len(content))),
                ("Cache-Control", "private, no-store"),
                ("Content-Security-Policy", "default-src 'none'; sandbox"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
