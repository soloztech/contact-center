from werkzeug.wrappers import Response

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.contact_center_base.services.adapter import AdapterError


class ContactCenterWuzapiOnboardingController(http.Controller):
    @http.route(
        "/contact-center/onboarding/wuzapi/qr/<string:setup_ref>/<int:revision>",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def qr_code(self, setup_ref, revision, **_kwargs):
        user = request.env.user
        if not (
            request.env.is_superuser()
            or user.has_group("contact_center_base.group_contact_center_admin")
        ):
            return request.not_found()
        wizard = request.env["contact.center.account.setup.wizard"].search(
            [
                ("setup_ref", "=", setup_ref),
                ("user_id", "=", user.id),
                ("company_id", "in", request.env.companies.ids),
            ],
            limit=1,
        )
        if (
            not wizard
            or wizard.provider_key != "wuzapi"
            or wizard.wuzapi_pairing_revision != revision
            or not wizard.connection_id
        ):
            return request.not_found()
        try:
            content = wizard.connection_id._wuzapi_onboarding_qr_png()
        except (AccessError, AdapterError, ValidationError):
            return request.not_found()
        return Response(
            content,
            status=200,
            content_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Content-Security-Policy": "default-src 'none'",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
