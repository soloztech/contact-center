from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import SavepointCase

from ..controllers.main import (
    _MEDIA_CONTENT_SECURITY_POLICY,
    _audio_upload_metadata,
    _media_response_policy,
    _upload_mimetype,
)
from ..services.dto import DTOValidationError, MediaDTO
from ..services.media import (
    MEDIA_SIZE_LIMITS,
    canonical_recorded_audio_duration_seconds,
    is_iso_bmff_audio_only,
    is_ogg_opus,
    media_kind_for_mimetype,
    validate_media_metadata,
    validate_provider_recorded_audio_capability,
)


def _ogg_page(first_packet, *, granule=0, header_type=2, serial=1, sequence=0):
    return (
        b"OggS\x00"
        + bytes((header_type,))
        + granule.to_bytes(8, "little")
        + serial.to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + (b"\x00" * 4)
        + b"\x01"
        + bytes((len(first_packet),))
        + first_packet
    )


_OPUS_HEAD = (
    b"OpusHead\x01\x01\x00\x00" + (48000).to_bytes(4, "little") + b"\x00\x00\x00"
)


def _recorded_ogg(duration_samples):
    return _ogg_page(_OPUS_HEAD) + _ogg_page(
        b"\xf8\xff\xfe",
        granule=duration_samples,
        header_type=4,
        sequence=1,
    )


def _mp4_box(box_type, payload=b""):
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _recorded_mp4(timescale, duration):
    mdhd = (
        b"\x00\x00\x00\x00"
        + (b"\x00" * 8)
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + (b"\x00" * 4)
    )
    media = _mp4_box(b"mdhd", mdhd) + _mp4_box(
        b"hdlr", (b"\x00" * 8) + b"soun" + (b"\x00" * 4)
    )
    return (
        _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00")
        + _mp4_box(
            b"moov",
            _mp4_box(
                b"trak",
                _mp4_box(b"tkhd", (b"\x00" * 12) + (1).to_bytes(4, "big"))
                + _mp4_box(b"mdia", media),
            ),
        )
        + _mp4_box(b"mdat", b"\x00")
    )


def _fragmented_recorded_mp4():
    mdhd = (
        b"\x00\x00\x00\x00"
        + (b"\x00" * 8)
        + (48000).to_bytes(4, "big")
        + (142).to_bytes(4, "big")
        + (b"\x00" * 4)
    )
    media = _mp4_box(b"mdhd", mdhd) + _mp4_box(
        b"hdlr", (b"\x00" * 8) + b"soun" + (b"\x00" * 4)
    )
    track = _mp4_box(
        b"trak",
        _mp4_box(b"tkhd", (b"\x00" * 12) + (1).to_bytes(4, "big"))
        + _mp4_box(b"mdia", media),
    )
    trex = _mp4_box(
        b"trex",
        b"\x00\x00\x00\x00"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + (0).to_bytes(8, "big"),
    )
    init_segment = _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00") + _mp4_box(
        b"moov", track + _mp4_box(b"mvex", trex)
    )

    def fragment(sequence, decode_start, durations):
        tfhd = _mp4_box(
            b"tfhd",
            b"\x00\x02\x00\x20" + (1).to_bytes(4, "big") + (0).to_bytes(4, "big"),
        )
        tfdt = _mp4_box(b"tfdt", b"\x01\x00\x00\x00" + decode_start.to_bytes(8, "big"))
        samples = b"".join(
            duration.to_bytes(4, "big") + (1).to_bytes(4, "big")
            for duration in durations
        )
        trun = _mp4_box(
            b"trun",
            b"\x01\x00\x03\x01"
            + len(durations).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
            + samples,
        )
        moof = _mp4_box(
            b"moof",
            _mp4_box(b"mfhd", b"\x00\x00\x00\x00" + sequence.to_bytes(4, "big"))
            + _mp4_box(b"traf", tfhd + tfdt + trun),
        )
        return moof + _mp4_box(b"mdat", b"\x00" * len(durations))

    return (
        init_segment + fragment(1, 0, (2973, 2847, 1008)) + fragment(2, 54832, (1008,))
    )


class TestContactCenterMediaSecurity(SavepointCase):
    def test_recorded_audio_upload_metadata_is_strict(self):
        self.assertEqual(
            _audio_upload_metadata({"is_voice_note": "1", "duration_seconds": "12"}),
            (True, 12),
        )
        self.assertEqual(
            _audio_upload_metadata({"is_voice_note": "0", "duration_seconds": "12"}),
            (False, 12),
        )
        self.assertEqual(_audio_upload_metadata({}), (False, 0))
        for values in (
            {"is_voice_note": "true", "duration_seconds": "12"},
            {"is_voice_note": "1", "duration_seconds": "0"},
            {"is_voice_note": "0", "duration_seconds": "901"},
            {"is_voice_note": "1", "duration_seconds": "not-a-number"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                _audio_upload_metadata(values)

    def test_voice_note_dto_is_always_audio(self):
        with self.assertRaises(DTOValidationError):
            MediaDTO(kind="image", is_voice_note=True)
        self.assertTrue(MediaDTO(kind="audio", is_voice_note=True).is_voice_note)

    def test_ogg_opus_structural_identification_is_available_to_adapters(self):
        ogg_opus = _ogg_page(_OPUS_HEAD)
        self.assertEqual(
            _upload_mimetype(
                ogg_opus,
                "audio/ogg;codecs=opus",
                is_recorded_audio=True,
            ),
            "audio/ogg",
        )
        self.assertTrue(is_ogg_opus(ogg_opus))
        for content, mimetype in (
            (_ogg_page(b"\x01vorbis" + (b"\x00" * 12)), "audio/ogg"),
            (ogg_opus[:5] + b"\x00" + ogg_opus[6:], "audio/ogg"),
            (_ogg_page(_OPUS_HEAD)[:-1], "audio/ogg"),
            (b"OggS-not-a-page-with-OpusHead", "audio/ogg"),
            (b"\x1aE\xdf\xa3" + (b"\x00" * 64), "audio/webm"),
            (b"<html><script>alert(1)</script></html>", "audio/ogg"),
        ):
            with self.subTest(mimetype=mimetype):
                self.assertFalse(is_ogg_opus(content))

    def test_recorded_audio_duration_is_derived_from_the_container(self):
        self.assertEqual(
            canonical_recorded_audio_duration_seconds(
                _recorded_ogg(12 * 48000 + 1), "audio/ogg"
            ),
            13,
        )
        self.assertEqual(
            canonical_recorded_audio_duration_seconds(
                _recorded_mp4(1000, 12250), "audio/mp4"
            ),
            13,
        )
        self.assertEqual(
            canonical_recorded_audio_duration_seconds(
                _fragmented_recorded_mp4(), "audio/mp4"
            ),
            2,
            "fragmented Chromium recordings derive duration from tfdt/trun",
        )
        for content, mimetype in (
            (_ogg_page(_OPUS_HEAD), "audio/ogg"),
            (_recorded_mp4(0, 12000), "audio/mp4"),
            (b"not-a-recording", "audio/ogg"),
        ):
            with self.subTest(mimetype=mimetype), self.assertRaises(ValidationError):
                canonical_recorded_audio_duration_seconds(content, mimetype)

    def test_iso_bmff_audio_probe_requires_hierarchy_and_a_bounded_box_count(self):
        top_level_handler = (
            _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00")
            + _mp4_box(b"hdlr", (b"\x00" * 8) + b"soun" + (b"\x00" * 4))
            + _mp4_box(b"mdat", b"\x00")
        )
        self.assertFalse(is_iso_bmff_audio_only(top_level_handler))

        valid = _recorded_mp4(1000, 1000)
        ftyp_end = int.from_bytes(valid[:4], "big")
        box_flood = valid[:ftyp_end] + (_mp4_box(b"free") * 16384) + valid[ftyp_end:]
        self.assertFalse(is_iso_bmff_audio_only(box_flood))

    def test_recording_requires_an_explicit_provider_capability(self):
        policy = {
            "media": {
                "audio": {
                    "enabled": True,
                    "recording_mimetypes": [
                        "audio/ogg;codecs=opus",
                        "audio/mp4",
                    ],
                    "voice_note_mimetypes": ["audio/ogg;codecs=opus"],
                    "max_duration_seconds": 900,
                }
            }
        }
        self.assertTrue(
            validate_provider_recorded_audio_capability(policy, "audio/ogg", True, 12)
        )
        self.assertTrue(
            validate_provider_recorded_audio_capability(policy, "audio/mp4", False, 12)
        )
        for exception_class, capabilities, mimetype, is_voice_note, duration in (
            (
                UserError,
                {"media": {"audio": {"enabled": True}}},
                "audio/mp4",
                False,
                12,
            ),
            (UserError, policy, "audio/mp4", True, 12),
            (UserError, policy, "audio/webm", False, 12),
            (UserError, policy, "audio/mp4", False, 901),
        ):
            with self.subTest(mimetype=mimetype), self.assertRaises(exception_class):
                validate_provider_recorded_audio_capability(
                    capabilities, mimetype, is_voice_note, duration
                )

    def test_mp4_declared_as_audio_is_only_preserved_for_a_recording(self):
        content = (
            (16).to_bytes(4, "big")
            + b"ftyp"
            + b"isom\x00\x00\x00\x00"
            + (8).to_bytes(4, "big")
            + b"mdat"
        )
        detected = _upload_mimetype(content, "audio/mp4")
        self.assertNotEqual(detected, "audio/mp4")
        self.assertEqual(
            _upload_mimetype(content, "audio/mp4", is_recorded_audio=True),
            "audio/mp4",
        )

    def test_document_accepts_vendor_image_mime_without_reclassification(self):
        self.assertEqual(media_kind_for_mimetype("image/vnd.dwg"), "image")
        self.assertEqual(
            validate_media_metadata("document", "image/vnd.dwg", 1024),
            "image/vnd.dwg",
        )

    def test_document_vendor_mime_keeps_size_and_typed_media_validation(self):
        with self.assertRaises(ValidationError):
            validate_media_metadata(
                "document",
                "image/vnd.dwg",
                MEDIA_SIZE_LIMITS["document"] + 1,
            )
        with self.assertRaises(ValidationError):
            validate_media_metadata("image", "application/octet-stream", 1024)

    def test_documents_are_always_downloaded(self):
        detected_mimetype, disposition = _media_response_policy(
            "document", "text/html", b"<html><script>alert(1)</script></html>"
        )

        self.assertTrue(detected_mimetype)
        self.assertEqual(disposition, "attachment")

    def test_dwg_and_executable_documents_are_never_rendered_inline(self):
        for declared_mimetype, content in (
            ("image/vnd.dwg", b"AC1032" + (b"\x00" * 64)),
            ("application/x-msdownload", b"MZ" + (b"\x00" * 64)),
        ):
            with self.subTest(declared_mimetype=declared_mimetype):
                _detected_mimetype, disposition = _media_response_policy(
                    "document", declared_mimetype, content
                )
                self.assertEqual(disposition, "attachment")

    def test_sniffed_kind_must_match_before_inline_rendering(self):
        _mimetype, disposition = _media_response_policy(
            "image", "image/png", b"<html><script>alert(1)</script></html>"
        )

        self.assertEqual(disposition, "attachment")

    def test_safe_matching_media_can_render_inline_unless_download_requested(self):
        png = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 64)

        mimetype, disposition = _media_response_policy("image", "image/png", png)
        _download_mimetype, download_disposition = _media_response_policy(
            "image", "image/png", png, download=True
        )

        self.assertEqual(mimetype, "image/png")
        self.assertEqual(disposition, "inline")
        self.assertEqual(download_disposition, "attachment")
        self.assertEqual(
            _MEDIA_CONTENT_SECURITY_POLICY,
            "default-src 'none'; sandbox",
        )
