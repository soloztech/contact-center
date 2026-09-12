"""Run without Odoo or a provider: python3 path/to/test_adapters_standalone.py."""

import importlib.util
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVICE_PATH = Path(__file__).resolve().parents[1] / "services" / "transcription.py"
SPEC = importlib.util.spec_from_file_location(
    "cc_transcription_standalone", SERVICE_PATH
)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


class Response:
    def __init__(self, payload=None, status=200, chunks=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
        self.read = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        self.read = True
        yield from self.chunks


class TestTranscriptionAdapters(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = service.ProviderConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-transcribe",
            api_key="test-secret-not-a-real-key",
            timeout_seconds=60,
        )
        self.request = service.TranscriptionRequest(
            content=b"OggS-synthetic-test-audio",
            filename="private-customer-name.oga",
            mime_type="audio/ogg; codecs=opus",
            language="pt",
            prompt="Motores de 220 volts.",
        )
        self.adapter = service.OpenAIAdapter()
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.patch_session = patch.object(
            service.requests, "Session", return_value=self.session
        )
        self.patch_session.start()
        self.addCleanup(self.patch_session.stop)

    def transcribe(self, response=None, *, config=None, request=None, adapter=None):
        self.session.post.return_value = response or Response(
            {"text": "Pedido recebido."}
        )
        return (adapter or self.adapter).transcribe(
            config or self.config, request or self.request
        )

    def assert_error(self, expected, callback, *, retryable=False):
        with self.assertRaises(service.TranscriptionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(caught.exception.retryable, retryable)
        self.assertEqual(str(caught.exception), expected)
        self.assertNotIn(self.config.api_key, repr(caught.exception))
        return caught.exception

    def test_custom_adapter_extends_registry_without_http(self):
        registry = service.TranscriptionRegistry()

        @registry.register("company.engine", label="Company engine")
        class CustomAdapter(service.TranscriptionAdapter):
            def transcribe(self, config, request):
                return service.TranscriptionResult("Transcrição local", "pt")

        result = registry.get("company.engine")().transcribe(self.config, self.request)
        self.assertEqual(result.text, "Transcrição local")
        self.assertEqual(registry.selection(), [("company.engine", "Company engine")])
        self.session.post.assert_not_called()
        registry.register("company.engine", CustomAdapter, "Company engine")
        with self.assertRaises(ValueError):
            registry.register("company.engine", service.OpenAIAdapter)
        self.assert_error("unknown_provider", lambda: registry.get("missing"))
        with self.assertRaises(TypeError):
            registry.register("invalid", object)

    def test_contracts_are_immutable_and_repr_does_not_leak(self):
        with self.assertRaises(FrozenInstanceError):
            self.config.model = "changed"
        self.assertNotIn(self.config.api_key, repr(self.config))
        self.assertNotIn(self.config.base_url, repr(self.config))
        self.assertNotIn(self.request.filename, repr(self.request))
        self.assertNotIn(self.request.prompt, repr(self.request))
        self.assertNotIn("OggS", repr(self.request))
        self.assertNotIn(
            "private transcript",
            repr(service.TranscriptionResult("private transcript")),
        )

    def test_official_wire_format_and_gpt_language_array(self):
        response = Response(
            {"text": "  Pedido de 220 volts.  ", "languages": [{"code": "pt"}]}
        )
        result = self.transcribe(response)
        self.assertEqual(
            result, service.TranscriptionResult("Pedido de 220 volts.", "pt")
        )
        args, kwargs = self.session.post.call_args
        self.assertEqual(args, ("https://api.openai.com/v1/audio/transcriptions",))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertFalse(self.session.trust_env)
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer " + self.config.api_key
        )
        prepared = service.requests.Request(
            "POST",
            args[0],
            files=kwargs["files"],
            data=kwargs["data"],
            headers=kwargs["headers"],
        ).prepare()
        mime = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + prepared.headers["Content-Type"] + "\r\n\r\n").encode()
            + prepared.body
        )
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in mime.iter_parts()
        }
        self.assertEqual(parts["languages[]"].get_content(), "pt")
        self.assertNotIn("language", parts)
        self.assertEqual(parts["file"].get_filename(), "audio.ogg")
        self.assertEqual(parts["file"].get_content_type(), "audio/ogg")
        self.assertEqual(parts["file"].get_payload(decode=True), self.request.content)
        self.assertNotIn(self.request.filename.encode(), prepared.body)
        self.assertTrue(response.closed)

    def test_compatible_endpoint_and_older_model_wire_format(self):
        config = replace(
            self.config,
            base_url="http://192.0.2.1:8080/v1/",
            model="large-v3",
            api_key="",
        )
        self.transcribe(config=config, adapter=service.OpenAICompatibleAdapter())
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "http://192.0.2.1:8080/v1/audio/transcriptions")
        self.assertEqual(dict(kwargs["data"])["language"], "pt")
        self.assertNotIn("languages[]", dict(kwargs["data"]))
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_no_hint_is_reported_as_detected_language(self):
        result = self.transcribe(Response({"text": "Pedido recebido."}))
        self.assertEqual(result.language, "")
        result = self.transcribe(
            Response({"text": "Hi, olá", "languages": [{"code": "en"}, {"code": "pt"}]})
        )
        self.assertEqual(result.language, "")

    def test_official_endpoint_is_fixed_and_config_never_performs_network(self):
        self.adapter.validate_config(replace(self.config, base_url=""))
        invalid = [
            "http://api.openai.com/v1",
            "https://other.example/v1",
            "https://api.openai.com:444/v1",
            "https://api.openai.com/v1/other",
            "https://name:secret@api.openai.com/v1",
            "https://api.openai.com/v1?secret=x",
            "https://api.openai.com/v1#fragment",
            "https://api.openai.com/v1?",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assert_error(
                    "invalid_config",
                    lambda: self.adapter.validate_config(
                        replace(self.config, base_url=url)
                    ),
                )
        self.session.post.assert_not_called()

    def test_compatible_url_validation(self):
        for url in [
            "file:///tmp/audio",
            "//host/v1",
            "http://host:99999/v1",
            "http://user@host",
            "http://host/?",
            "http://host/#",
            "http://host\\evil",
            "http://host\n/v1",
            "http://[broken",
        ]:
            with self.subTest(url=url):
                self.assert_error(
                    "invalid_config", lambda: service.normalize_base_url(url)
                )
        self.assertEqual(
            service.normalize_base_url("http://localhost:8000/v1/"),
            "http://localhost:8000/v1",
        )

    def test_missing_credentials_invalid_model_and_timeout(self):
        self.assert_error(
            "invalid_credentials",
            lambda: self.transcribe(config=replace(self.config, api_key="")),
        )
        for fields in [
            {"api_key": "secret\r\nHeader: value"},
            {"model": "model\n"},
            {"timeout_seconds": 0},
            {"timeout_seconds": 301},
            {"timeout_seconds": True},
        ]:
            with self.subTest(fields=fields):
                self.assert_error(
                    "invalid_config",
                    lambda: self.transcribe(config=replace(self.config, **fields)),
                )
        self.session.post.assert_not_called()

    def test_request_limits_prevent_upload(self):
        cases = [
            ({"content": b""}, "empty_audio"),
            ({"content": "not bytes"}, "invalid_request"),
            ({"mime_type": "text/plain"}, "unsupported_audio_format"),
            ({"language": "pt-BR"}, "invalid_request"),
            ({"prompt": "x" * (service.MAX_PROMPT_CHARS + 1)}, "invalid_request"),
        ]
        for changes, code in cases:
            with self.subTest(changes=changes):
                self.assert_error(
                    code,
                    lambda: self.transcribe(request=replace(self.request, **changes)),
                )
        with patch.object(service, "MAX_AUDIO_BYTES", 3):
            self.assert_error("audio_too_large", lambda: self.transcribe())
        self.session.post.assert_not_called()

    def test_diarization_is_explicit_and_cannot_silently_discard_prompt(self):
        config = replace(self.config, model="gpt-4o-transcribe-diarize")
        self.assert_error("invalid_request", lambda: self.transcribe(config=config))
        self.transcribe(config=config, request=replace(self.request, prompt=""))
        self.assertEqual(
            dict(self.session.post.call_args.kwargs["data"])["chunking_strategy"],
            "auto",
        )

    def test_http_failures_are_classified_without_reading_error_body(self):
        cases = [
            (401, "invalid_credentials", False),
            (403, "access_denied", False),
            (429, "rate_limited", True),
            (503, "provider_unavailable", True),
            (408, "timeout", True),
            (302, "redirect_blocked", False),
            (413, "audio_too_large", False),
            (404, "model_or_endpoint_not_found", False),
            (422, "invalid_request", False),
        ]
        for status, code, retryable in cases:
            with self.subTest(status=status):
                response = Response(
                    {"error": self.config.api_key},
                    status=status,
                    headers={"Retry-After": "45"},
                )
                error = self.assert_error(
                    code, lambda: self.transcribe(response), retryable=retryable
                )
                if status in {429, 503}:
                    self.assertEqual(error.retry_after_seconds, 45)
                self.assertFalse(response.read)
                self.assertTrue(response.closed)

    def test_retry_after_is_bounded_and_error_text_is_never_trusted(self):
        for value, expected in [("99999999", 3600), ("-10", 0), ("bad secret", 0)]:
            error = self.assert_error(
                "rate_limited",
                lambda: self.transcribe(
                    Response(status=429, headers={"Retry-After": value})
                ),
                retryable=True,
            )
            self.assertEqual(error.retry_after_seconds, expected)
        error = service.TranscriptionError("echoed-secret-in-untrusted-code")
        self.assertEqual(str(error), "provider_error")

    def test_transport_errors_do_not_echo_secrets_or_endpoint(self):
        cases = [
            (service.requests.exceptions.ReadTimeout, "timeout", True),
            (service.requests.exceptions.SSLError, "tls_error", False),
            (service.requests.exceptions.ConnectionError, "connection_error", True),
            (service.requests.exceptions.RequestException, "provider_error", False),
        ]
        for exception, code, retryable in cases:
            with self.subTest(code=code):
                self.session.post.side_effect = exception(
                    self.config.base_url + self.config.api_key
                )
                error = self.assert_error(
                    code, lambda: self.transcribe(), retryable=retryable
                )
                self.assertTrue(error.__suppress_context__)

    def test_json_contract_and_no_speech(self):
        for payload in [
            [],
            {},
            {"text": None},
            {"text": 12},
            {"text": "hello\x00"},
            {"text": "hello\ud800"},
        ]:
            with self.subTest(payload=payload):
                self.assert_error(
                    "invalid_response", lambda: self.transcribe(Response(payload))
                )
        for value in ["", " \n\t"]:
            self.assert_error(
                "no_speech", lambda: self.transcribe(Response({"text": value}))
            )
        self.assert_error(
            "invalid_response",
            lambda: self.transcribe(Response(chunks=[b"not JSON private-content"])),
        )

    def test_response_limits_with_and_without_content_length(self):
        with patch.object(service, "MAX_RESPONSE_BYTES", 8):
            response = Response(
                chunks=[b"private audio transcript"], headers={"Content-Length": "24"}
            )
            self.assert_error("response_too_large", lambda: self.transcribe(response))
            self.assertFalse(response.read)
            response = Response(chunks=[b"12345", b"67890"])
            self.assert_error("response_too_large", lambda: self.transcribe(response))
            self.assertTrue(response.closed)
        with patch.object(service, "MAX_TEXT_CHARS", 8):
            self.assert_error(
                "response_too_large",
                lambda: self.transcribe(Response({"text": "123456789"})),
            )

    def test_response_elapsed_time_and_stream_failure(self):
        with patch.object(service.time, "monotonic", side_effect=[10, 72]):
            self.assert_error("timeout", lambda: self.transcribe(), retryable=True)

        class BrokenResponse(Response):
            def iter_content(self, chunk_size):
                raise service.requests.exceptions.ConnectionError("private-url-and-key")

        response = BrokenResponse()
        self.assert_error(
            "connection_error", lambda: self.transcribe(response), retryable=True
        )
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
