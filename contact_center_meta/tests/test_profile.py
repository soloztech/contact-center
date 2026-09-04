import hashlib
from unittest import mock

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import AddressDTO, AvatarResult

from .common import MetaCase


class TestMetaIdentityProfile(MetaCase):
    @staticmethod
    def _address(value="900000000000050", namespace="meta.messenger.psid"):
        return AddressDTO(
            namespace=namespace,
            value=value,
            value_normalized=value,
            role="primary",
            source_field="identity_alias",
            confidence="protocol",
        )

    @mock.patch(
        "odoo.addons.contact_center_meta.services.profile.download_profile_avatar"
    )
    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_profile_uses_shared_runtime_and_keeps_media_private(
        self, graph_request, download_avatar
    ):
        picture = "https://lookaside.fbsbx.com/profile?token=private"
        graph_request.return_value = {
            "id": "900000000000050",
            "first_name": "Meta",
            "last_name": "Person",
            "profile_pic": picture,
        }
        avatar = AvatarResult(
            state="ready",
            content=b"\xff\xd8\xff\xe0meta-profile",
            mime_type="image/jpeg",
        )
        download_avatar.return_value = avatar

        result = self.connection.get_adapter().fetch_identity_profile(
            self.connection, self._address()
        )

        self.assertEqual(result.display_name, "Meta Person")
        self.assertEqual(result.avatar, avatar)
        runtime, access_token, method, path = graph_request.call_args.args[:4]
        self.assertEqual(runtime.external_app_id, self.app.external_app_id)
        self.assertEqual(access_token, self.PAGE_TOKEN)
        self.assertEqual((method, path), ("GET", "900000000000050"))
        self.assertEqual(
            graph_request.call_args.kwargs["params"],
            {"fields": "first_name,last_name,profile_pic"},
        )
        download_avatar.assert_called_once_with(
            picture,
            provider_revision=hashlib.sha256(picture.encode("utf-8")).hexdigest(),
        )

    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_profile_without_picture_returns_absent_avatar(self, graph_request):
        graph_request.return_value = {
            "id": "900000000000050",
            "first_name": "Only",
            "last_name": "Name",
        }

        result = self.connection.get_adapter().fetch_identity_profile(
            self.connection, self._address()
        )

        self.assertEqual(result.display_name, "Only Name")
        self.assertEqual(result.avatar.state, "absent")

    @mock.patch(
        "odoo.addons.contact_center_meta.services.profile.download_profile_avatar"
    )
    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_avatar_failure_preserves_the_profile_name(
        self, graph_request, download_avatar
    ):
        picture = "https://lookaside.fbsbx.com/profile/retry"
        graph_request.return_value = {
            "id": "900000000000050",
            "first_name": "Name",
            "last_name": "Survives",
            "profile_pic": picture,
        }
        download_avatar.side_effect = AdapterError("unsupported avatar response")

        result = self.connection.get_adapter().fetch_identity_profile(
            self.connection, self._address()
        )

        self.assertEqual(result.display_name, "Name Survives")
        self.assertEqual(result.avatar.state, "unavailable")
        self.assertEqual(
            result.avatar.provider_revision,
            hashlib.sha256(picture.encode("utf-8")).hexdigest(),
        )

    @mock.patch(
        "odoo.addons.contact_center_meta.services.profile.download_profile_avatar"
    )
    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_transient_avatar_failure_remains_retryable(
        self, graph_request, download_avatar
    ):
        picture = "https://lookaside.fbsbx.com/profile/retry"
        graph_request.return_value = {
            "id": "900000000000050",
            "first_name": "Retryable",
            "last_name": "Profile",
            "profile_pic": picture,
        }
        transient = TransientAdapterError("temporary avatar failure")
        transient.retry_after_seconds = 30
        download_avatar.side_effect = transient

        with self.assertRaises(TransientAdapterError) as raised:
            self.connection.get_adapter().fetch_identity_profile(
                self.connection, self._address()
            )

        self.assertIs(raised.exception, transient)
        self.assertEqual(raised.exception.retry_after_seconds, 30)

    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_missing_reviewed_permission_is_an_unavailable_capability(
        self, graph_request
    ):
        error = ProviderPausedError("permission unavailable")
        error.provider_code = 10
        graph_request.side_effect = error

        with self.assertRaises(UnsupportedEventError):
            self.connection.get_adapter().fetch_identity_profile(
                self.connection, self._address()
            )

    @mock.patch("odoo.addons.contact_center_meta.services.profile.graph_request")
    def test_profile_address_must_match_transport_namespace(self, graph_request):
        with self.assertRaises(AdapterError):
            self.connection.get_adapter().fetch_identity_profile(
                self.connection,
                self._address(namespace="meta.instagram.igsid"),
            )

        graph_request.assert_not_called()
