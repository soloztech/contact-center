"""No-Odoo tests of the bounded image transport; never contact a provider."""

import importlib.util
import io
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

_PACKAGE = "_contact_center_ad_preview_tests"
_DIRECTORY = Path(__file__).resolve().parents[1] / "services"
package = types.ModuleType(_PACKAGE)
package.__path__ = [str(_DIRECTORY)]
sys.modules[_PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    _PACKAGE + ".ad_origin_preview", _DIRECTORY / "ad_origin_preview.py"
)
service = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = service
spec.loader.exec_module(service)


class Response:
    def __init__(self, content=b"", *, status=200, headers=None):
        self.buffer = io.BytesIO(content)
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def read(
        self, length, decode_content=False
    ):  # pylint: disable=method-required-super
        return self.buffer.read(length)

    def close(self):
        self.closed = True


class TestAdPreviewTransport(unittest.TestCase):
    def setUp(self):
        super().setUp()
        image = Image.new("RGB", (1000, 700), (10, 20, 30))
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.content = output.getvalue()
        self.url = "https://scontent.example.fbcdn.net/ad.png?oe=private-signed-value"

    def fetch(self, response):
        pool = MagicMock()
        pool.urlopen.return_value = response
        with patch.object(
            service,
            "public_url_target",
            return_value=(service.urlsplit(self.url), 443, "8.8.8.8"),
        ), patch.object(
            service.urllib3, "HTTPSConnectionPool", return_value=pool
        ) as factory:
            result = service.fetch_thumbnail(self.url)
        return result, pool, factory

    def test_public_links_reject_credentials_private_parameters_and_paths(self):
        self.assertEqual(
            service.public_source_url("https://www.instagram.com/p/example/"),
            "https://www.instagram.com/p/example/",
        )
        self.assertTrue(
            service.public_source_url(
                "https://www.facebook.com/permalink.php?id=123&story_fbid=456"
            )
        )
        for url in (
            "https://evil.example/p/1",
            "http://instagram.com/p/1",
            "https://instagram.com:8443/p/1",
            "https://secret@instagram.com/p/1",
            "https://instagram.com/p/1?access_token=secret",
            "https://instagram.com/direct/t/private",
            "https://instagram.com/%64irect/t/private",
            "https://facebook.com/dialog/oauth",
            "https://fb.me/login",
            "https://instagram.com/p/1#secret",
            "https://instagram.com\\@evil.example/p/1",
        ):
            with self.subTest(url=url):
                self.assertEqual(service.public_source_url(url), "")

    def test_thumbnail_hosts_do_not_accept_lookalike_or_local_targets(self):
        self.assertEqual(service.thumbnail_url(self.url), self.url)
        for url in (
            "https://fbcdn.net.evil.example/ad.png",
            "https://127.0.0.1/ad.png",
            "https://fbcdn.net@127.0.0.1/ad.png",
            "http://scontent.fbcdn.net/ad.png",
            "https://scontent.fbcdn.net:8443/ad.png",
            "data:image/png;base64,AAA",
        ):
            self.assertFalse(service.thumbnail_url(url))

    def test_creative_is_bounded_plain_text_and_never_carries_remote_image_urls(self):
        result = service.normalize_creative(
            {
                "title": "A\x00B" * 100,
                "body": "c" * 4000,
                "thumbnail_url": self.url,
                "media_url": self.url,
                "thumbnail_ref": "invalid",
                "other": "secret",
            }
        )
        self.assertEqual(len(result["title"]), 256)
        self.assertEqual(len(result["body"]), 2000)
        self.assertNotIn("\x00", result["title"])
        self.assertEqual(set(result), {"title", "body"})
        self.assertEqual(service.clean_text("bad\ud800text", 100), "badtext")

    def test_derivative_is_small_jpeg_with_no_original_metadata(self):
        data, width, height = service.image_derivative(self.content)
        self.assertLessEqual(width, 640)
        self.assertLessEqual(height, 360)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.getexif())
            self.assertEqual(image.size, (width, height))

    def test_rejects_html_and_oversized_images_before_decode(self):
        for content in (
            b"<html>not an image</html>",
            b"x" * (service.MAX_IMAGE_BYTES + 1),
        ):
            with self.assertRaises(service.PreviewError):
                service.image_derivative(content)
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.format, fake.width, fake.height = "PNG", 100_000, 100_000
        with patch.object(service.Image, "open", return_value=fake):
            with self.assertRaises(service.PreviewError):
                service.image_derivative(b"small")
            fake.load.assert_not_called()

    def test_transport_pins_ip_and_tls_host_and_never_sends_credentials(self):
        response = Response(self.content)
        result, pool, factory = self.fetch(response)
        self.assertTrue(result[0])
        self.assertEqual(factory.call_args.args, ("8.8.8.8",))
        self.assertEqual(
            factory.call_args.kwargs["assert_hostname"], "scontent.example.fbcdn.net"
        )
        headers = pool.urlopen.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertFalse(pool.urlopen.call_args.kwargs["redirect"])
        self.assertFalse(pool.urlopen.call_args.kwargs["retries"])
        self.assertTrue(response.closed)
        pool.close.assert_called_once()

    def test_redirect_revalidates_cdn_before_second_request(self):
        response = Response(
            status=302, headers={"Location": "http://127.0.0.1/private"}
        )
        pool = MagicMock()
        pool.urlopen.return_value = response
        with patch.object(
            service,
            "public_url_target",
            return_value=(service.urlsplit(self.url), 443, "8.8.8.8"),
        ), patch.object(service.urllib3, "HTTPSConnectionPool", return_value=pool):
            with self.assertRaises(service.PreviewError):
                service.fetch_thumbnail(self.url)
        pool.urlopen.assert_called_once()
        self.assertTrue(response.closed)

    def test_private_dns_resolution_is_rejected_before_connection(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(socket, "getaddrinfo", return_value=addresses), patch.object(
            service.urllib3, "HTTPSConnectionPool"
        ) as pool:
            with self.assertRaises(service.PreviewError):
                service.fetch_thumbnail(self.url)
            pool.assert_not_called()

    def test_retryable_status_and_errors_are_sanitized(self):
        for status in (429, 500, 503):
            with self.assertRaises(service.PreviewError) as caught:
                self.fetch(Response(status=status))
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("private-signed", str(caught.exception))
        with self.assertRaises(service.PreviewError) as caught:
            self.fetch(Response(status=403))
        self.assertFalse(caught.exception.retryable)

    def test_bounded_response_and_encoding_fail_without_decode(self):
        for response in (
            Response(headers={"Content-Length": str(service.MAX_IMAGE_BYTES + 1)}),
            Response(headers={"Content-Encoding": "gzip"}),
            Response(b"x" * (service.MAX_IMAGE_BYTES + 1)),
        ):
            with self.assertRaises(service.PreviewError):
                self.fetch(response)
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
