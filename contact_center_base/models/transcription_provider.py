import os
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.transcription import (
    ProviderConfig,
    TranscriptionError,
    transcription_registry,
)


class ContactCenterTranscriptionProvider(models.Model):
    _name = "contact.center.transcription.provider"
    _description = "Contact Center Speech Provider"
    _check_company_auto = True
    _order = "name, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    routing_revision = fields.Integer(default=0, readonly=True, copy=False)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
        index=True,
    )
    backend = fields.Selection(
        selection="_backend_selection", required=True, default="openai"
    )
    base_url = fields.Char(
        string="API Base URL",
        help="For compatible services, include the API prefix, for example /v1.",
    )
    model = fields.Char(required=True, default="gpt-transcribe")
    api_key = fields.Char(
        string="API Key",
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help="Private credential used only by the server. An environment variable takes precedence.",
    )
    api_key_env = fields.Char(
        string="API Key Environment Variable",
        help="Name of a server environment variable starting with CC_TRANSCRIPTION_. "
        "Optional alternative to storing the API key in this configuration.",
    )
    language = fields.Char(
        default="pt", help="Language hint such as pt; leave empty for detection."
    )
    prompt = fields.Text(help="Optional vocabulary or transcription context.")
    timeout_seconds = fields.Integer(default=60, required=True)
    max_duration_seconds = fields.Integer(
        default=900,
        required=True,
        help="Skip audio exceeding this duration when duration metadata is available.",
    )

    @api.model
    def _backend_selection(self):
        return transcription_registry.selection()

    @api.onchange("backend")
    def _onchange_backend(self):
        if self.backend == "openai":
            self.base_url = False

    @api.model_create_multi
    def create(self, vals_list):
        if any("routing_revision" in values for values in vals_list) or (
            "default_routing_revision" in self.env.context
        ):
            raise AccessError(_("The speech routing revision is managed internally."))
        return super().create(vals_list)

    def write(self, values):
        self.check_access_rights("write")
        self.check_access_rule("write")
        if "routing_revision" in values:
            raise AccessError(_("The speech routing revision is managed internally."))
        routing_fields = {"backend", "base_url", "api_key", "api_key_env", "company_id"}
        if not routing_fields.intersection(values):
            return super().write(values)
        for provider in self.sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM contact_center_transcription_provider WHERE id = %s FOR UPDATE",
                [provider.id],
            )
            provider.invalidate_recordset(["routing_revision"])
            super(ContactCenterTranscriptionProvider, provider).write(
                dict(values, routing_revision=provider.routing_revision + 1)
            )
        return True

    @api.constrains("company_id")
    def _check_inbox_companies(self):
        for provider in self:
            if (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search_count(
                    [
                        ("transcription_provider_id", "=", provider.id),
                        ("company_id", "!=", provider.company_id.id),
                    ]
                )
            ):
                raise ValidationError(
                    _("The speech provider is used by an inbox in another company.")
                )

    @api.constrains(
        "backend",
        "base_url",
        "model",
        "api_key",
        "api_key_env",
        "language",
        "prompt",
        "timeout_seconds",
        "max_duration_seconds",
    )
    def _check_configuration(self):
        for provider in self:
            if provider.api_key_env and not re.fullmatch(
                r"CC_TRANSCRIPTION_[A-Z0-9_]+", provider.api_key_env
            ):
                raise ValidationError(
                    _("Use an environment variable starting with CC_TRANSCRIPTION_.")
                )
            if provider.backend == "openai" and not (
                provider.api_key_env or provider.api_key
            ):
                raise ValidationError(
                    _("Configure the OpenAI API key or its environment variable.")
                )
            if provider.language and not re.fullmatch(r"[a-z]{2}", provider.language):
                raise ValidationError(_("Use a two letter language code."))
            if len(provider.prompt or "") > 2000:
                raise ValidationError(
                    _("Transcription context is limited to 2,000 characters.")
                )
            if (
                provider.backend in ("openai", "openai_compatible")
                and provider.model == "gpt-4o-transcribe-diarize"
                and provider.prompt
            ):
                raise ValidationError(
                    _("This diarization model does not support transcription context.")
                )
            if not 1 <= provider.max_duration_seconds <= 3600:
                raise ValidationError(
                    _("The audio duration limit must be between 1 and 3,600 seconds.")
                )
            try:
                adapter = transcription_registry.get(provider.backend)()
                adapter.validate_config(
                    ProviderConfig(
                        base_url=provider.base_url or "",
                        model=provider.model or "",
                        api_key=provider.api_key or "configuration-validation",
                        timeout_seconds=provider.timeout_seconds,
                    )
                )
            except (TranscriptionError, KeyError, ValueError) as error:
                raise ValidationError(
                    _("Invalid speech provider configuration.")
                ) from error

    def _configuration_snapshot(self):
        self.ensure_one()
        return {
            "routing_revision": self.routing_revision,
            "backend": self.backend,
            "base_url": self.base_url or "",
            "model": self.model,
            "api_key_env": self.api_key_env or "",
            "timeout_seconds": self.timeout_seconds,
            "language": self.language or "",
            "prompt": self.prompt or "",
            "max_duration_seconds": self.max_duration_seconds,
        }

    def _transcribe(self, snapshot, request):
        self.ensure_one()
        # Only a protected server-side snapshot reaches this method.
        variable = snapshot.get("api_key_env", "")
        if variable and not re.fullmatch(r"CC_TRANSCRIPTION_[A-Z0-9_]+", variable):
            raise TranscriptionError("invalid_config")
        api_key = os.environ.get(variable, "") if variable else self.api_key or ""
        if variable and not api_key:
            raise TranscriptionError("invalid_credentials")
        config = ProviderConfig(
            base_url=snapshot["base_url"],
            model=snapshot["model"],
            api_key=api_key,
            timeout_seconds=snapshot["timeout_seconds"],
        )
        adapter = transcription_registry.get(snapshot["backend"])()
        return adapter.transcribe(config, request)
