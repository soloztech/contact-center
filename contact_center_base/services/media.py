import hashlib

from odoo import _
from odoo.exceptions import UserError, ValidationError

MEDIA_SIZE_LIMITS = {
    "image": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "document": 50 * 1024 * 1024,
}


def is_ogg_opus(content):
    """Return whether the first OGG packet is a structural OpusHead header."""

    if isinstance(content, memoryview):
        content = content.tobytes()
    if not isinstance(content, bytes) or len(content) < 47:
        return False
    if content[:5] != b"OggS\x00" or not content[5] & 0x02:
        return False
    segment_count = content[26]
    segment_table_end = 27 + segment_count
    if not segment_count or len(content) < segment_table_end:
        return False
    first_packet_size = 0
    packet_complete = False
    for segment_size in content[27:segment_table_end]:
        first_packet_size += segment_size
        if segment_size < 255:
            packet_complete = True
            break
    if len(content) < segment_table_end + first_packet_size:
        return False
    packet = content[segment_table_end : segment_table_end + first_packet_size]
    return bool(
        packet_complete
        and len(packet) >= 19
        and packet[:8] == b"OpusHead"
        and packet[8] == 1
        and packet[9] > 0
    )


def is_iso_bmff_audio_only(content):
    """Return whether a bounded ISO-BMFF file declares audio and no video track."""

    if isinstance(content, memoryview):
        content = content.tobytes()
    if not isinstance(content, bytes) or len(content) < 16:
        return False
    budget = {"remaining": 16384}
    top_level = _iso_bmff_boxes(content, 0, len(content), budget=budget)
    if not top_level or not any(box_type == b"ftyp" for box_type, *_ in top_level):
        return False
    handlers = set()
    movie_count = 0
    for box_type, box_start, box_end in top_level:
        if box_type != b"moov":
            continue
        movie_count += 1
        movie_boxes = _iso_bmff_boxes(content, box_start, box_end, budget=budget)
        if movie_boxes is None:
            return False
        for movie_type, movie_start, movie_end in movie_boxes:
            if movie_type != b"trak":
                continue
            track_boxes = _iso_bmff_boxes(
                content, movie_start, movie_end, budget=budget
            )
            if track_boxes is None:
                return False
            for track_type, track_start, track_end in track_boxes:
                if track_type != b"mdia":
                    continue
                media_boxes = _iso_bmff_boxes(
                    content, track_start, track_end, budget=budget
                )
                if media_boxes is None:
                    return False
                for media_type, media_start, media_end in media_boxes:
                    if media_type == b"hdlr" and media_start + 12 <= media_end:
                        handlers.add(content[media_start + 8 : media_start + 12])
    if movie_count != 1:
        return False
    return b"soun" in handlers and b"vide" not in handlers


def _iso_bmff_boxes(content, start, end, budget=None):
    """Return bounded child boxes or ``None`` for a malformed container."""

    budget = budget if budget is not None else {"remaining": 16384}
    boxes = []
    offset = start
    while offset + 8 <= end:
        if budget["remaining"] <= 0:
            return None
        budget["remaining"] -= 1
        size = int.from_bytes(content[offset : offset + 4], "big")
        box_type = content[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > end:
                return None
            size = int.from_bytes(content[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            return None
        boxes.append((box_type, offset + header_size, offset + size))
        offset += size
    return boxes if offset == end else None


def _iso_bmff_media_header(payload):
    """Return ``(timescale, duration)`` from one bounded ``mdhd`` payload."""

    if not payload:
        return None
    version = payload[0]
    if version == 0 and len(payload) >= 20:
        timescale = int.from_bytes(payload[12:16], "big")
        duration = int.from_bytes(payload[16:20], "big")
        if duration == 0xFFFFFFFF:
            duration = 0
    elif version == 1 and len(payload) >= 32:
        timescale = int.from_bytes(payload[20:24], "big")
        duration = int.from_bytes(payload[24:32], "big")
        if duration == 0xFFFFFFFFFFFFFFFF:
            duration = 0
    else:
        return None
    return (timescale, duration) if timescale else None


def _iso_bmff_track_id(payload):
    """Read the track ID from one ``tkhd`` full-box payload."""

    if not payload:
        return None
    version = payload[0]
    if version == 0 and len(payload) >= 16:
        track_id = int.from_bytes(payload[12:16], "big")
    elif version == 1 and len(payload) >= 24:
        track_id = int.from_bytes(payload[20:24], "big")
    else:
        return None
    return track_id or None


def _iso_bmff_stts_duration(content, media_boxes):
    """Return the sample timeline declared by a classic ``stts`` table."""

    for box_type, box_start, box_end in media_boxes:
        if box_type != b"minf":
            continue
        minf_boxes = _iso_bmff_boxes(content, box_start, box_end)
        if minf_boxes is None:
            return None
        for minf_type, minf_start, minf_end in minf_boxes:
            if minf_type != b"stbl":
                continue
            sample_boxes = _iso_bmff_boxes(content, minf_start, minf_end)
            if sample_boxes is None:
                return None
            for sample_type, sample_start, sample_end in sample_boxes:
                if sample_type != b"stts":
                    continue
                payload = content[sample_start:sample_end]
                if len(payload) < 8:
                    return None
                entry_count = int.from_bytes(payload[4:8], "big")
                if entry_count > 100000 or len(payload) < 8 + entry_count * 8:
                    return None
                duration = 0
                offset = 8
                for _index in range(entry_count):
                    sample_count = int.from_bytes(payload[offset : offset + 4], "big")
                    sample_delta = int.from_bytes(
                        payload[offset + 4 : offset + 8], "big"
                    )
                    if not sample_count or not sample_delta:
                        return None
                    duration += sample_count * sample_delta
                    offset += 8
                return duration or None
    return None


def _iso_bmff_audio_track(content, movie_boxes):
    """Return exactly one bounded audio track's ID and time base."""

    audio_tracks = []
    for box_type, box_start, box_end in movie_boxes:
        if box_type != b"trak":
            continue
        track_boxes = _iso_bmff_boxes(content, box_start, box_end)
        if track_boxes is None:
            return None
        track_id = None
        handler_type = None
        media_header = None
        sample_duration = None
        for track_type, track_start, track_end in track_boxes:
            if track_type == b"tkhd":
                track_id = _iso_bmff_track_id(content[track_start:track_end])
            elif track_type == b"mdia":
                media_boxes = _iso_bmff_boxes(content, track_start, track_end)
                if media_boxes is None:
                    return None
                for media_type, media_start, media_end in media_boxes:
                    payload = content[media_start:media_end]
                    if media_type == b"hdlr" and len(payload) >= 12:
                        handler_type = payload[8:12]
                    elif media_type == b"mdhd":
                        media_header = _iso_bmff_media_header(payload)
                sample_duration = _iso_bmff_stts_duration(content, media_boxes)
        if handler_type == b"vide":
            return None
        if handler_type == b"soun":
            if not track_id or not media_header:
                return None
            audio_tracks.append(
                {
                    "track_id": track_id,
                    "timescale": media_header[0],
                    "header_duration": media_header[1],
                    "sample_duration": sample_duration,
                }
            )
    return audio_tracks[0] if len(audio_tracks) == 1 else None


def _iso_bmff_fragment_defaults(content, movie_boxes):
    """Return ``trex`` default sample durations keyed by track ID."""

    defaults = {}
    for box_type, box_start, box_end in movie_boxes:
        if box_type != b"mvex":
            continue
        mvex_boxes = _iso_bmff_boxes(content, box_start, box_end)
        if mvex_boxes is None:
            return None
        for mvex_type, mvex_start, mvex_end in mvex_boxes:
            if mvex_type != b"trex":
                continue
            payload = content[mvex_start:mvex_end]
            if len(payload) < 16:
                return None
            track_id = int.from_bytes(payload[4:8], "big")
            if not track_id or track_id in defaults:
                return None
            defaults[track_id] = int.from_bytes(payload[12:16], "big")
    return defaults


def _iso_bmff_tfhd(payload):
    """Return a fragment track ID and optional default sample duration."""

    if len(payload) < 8 or payload[0] != 0:
        return None
    flags = int.from_bytes(payload[1:4], "big")
    known_flags = (
        0x000001 | 0x000002 | 0x000008 | 0x000010 | 0x000020 | 0x010000 | 0x020000
    )
    if flags & ~known_flags:
        return None
    track_id = int.from_bytes(payload[4:8], "big")
    offset = 8
    if flags & 0x000001:
        offset += 8
    if flags & 0x000002:
        offset += 4
    default_duration = None
    if flags & 0x000008:
        if offset + 4 > len(payload):
            return None
        default_duration = int.from_bytes(payload[offset : offset + 4], "big")
        offset += 4
    if flags & 0x000010:
        offset += 4
    if flags & 0x000020:
        offset += 4
    if offset > len(payload) or not track_id:
        return None
    return track_id, default_duration


def _iso_bmff_tfdt(payload):
    """Return one fragment's base media decode time."""

    if len(payload) < 8 or int.from_bytes(payload[1:4], "big"):
        return None
    if payload[0] == 0 and len(payload) >= 8:
        return int.from_bytes(payload[4:8], "big")
    if payload[0] == 1 and len(payload) >= 12:
        return int.from_bytes(payload[4:12], "big")
    return None


def _iso_bmff_trun_duration(payload, default_duration):
    """Sum bounded sample durations from one ``trun`` full box."""

    if len(payload) < 8 or payload[0] not in (0, 1):
        return None
    flags = int.from_bytes(payload[1:4], "big")
    known_flags = 0x000001 | 0x000004 | 0x000100 | 0x000200 | 0x000400 | 0x000800
    if flags & ~known_flags:
        return None
    sample_count = int.from_bytes(payload[4:8], "big")
    if not sample_count or sample_count > 100000:
        return None
    offset = 8
    if flags & 0x000001:
        offset += 4
    if flags & 0x000004:
        offset += 4
    sample_field_flags = (
        (0x000100, "duration"),
        (0x000200, "size"),
        (0x000400, "flags"),
        (0x000800, "composition_offset"),
    )
    field_names = [name for flag, name in sample_field_flags if flags & flag]
    required_bytes = offset + sample_count * 4 * len(field_names)
    if required_bytes > len(payload):
        return None
    if "duration" not in field_names:
        return sample_count * default_duration if default_duration else None
    duration_offset = field_names.index("duration") * 4
    sample_width = len(field_names) * 4
    duration = 0
    for sample_index in range(sample_count):
        value_start = offset + sample_index * sample_width + duration_offset
        sample_duration = int.from_bytes(payload[value_start : value_start + 4], "big")
        if not sample_duration:
            return None
        duration += sample_duration
    return duration or None


def _iso_bmff_fragment_interval(content, start, end, track_id, trex_default):
    """Return ``(decode_start, decode_end)`` for one matching ``traf``."""

    boxes = _iso_bmff_boxes(content, start, end)
    if boxes is None:
        return None
    tfhd = None
    decode_start = None
    trun_payloads = []
    for box_type, box_start, box_end in boxes:
        payload = content[box_start:box_end]
        if box_type == b"tfhd":
            if tfhd is not None:
                return None
            tfhd = _iso_bmff_tfhd(payload)
        elif box_type == b"tfdt":
            if decode_start is not None:
                return None
            decode_start = _iso_bmff_tfdt(payload)
        elif box_type == b"trun":
            trun_payloads.append(payload)
    if not tfhd or tfhd[0] != track_id:
        return False
    if decode_start is None or not trun_payloads:
        return None
    default_duration = tfhd[1] or trex_default
    duration = 0
    for payload in trun_payloads:
        run_duration = _iso_bmff_trun_duration(payload, default_duration)
        if not run_duration:
            return None
        duration += run_duration
    return decode_start, decode_start + duration


def _iso_bmff_moof_interval(content, start, end, track_id, trex_default):
    """Return the single audio interval carried by one ``moof`` box."""

    fragment_boxes = _iso_bmff_boxes(content, start, end)
    if fragment_boxes is None:
        return None
    matching_interval = None
    for fragment_type, fragment_start, fragment_end in fragment_boxes:
        if fragment_type != b"traf":
            continue
        interval = _iso_bmff_fragment_interval(
            content,
            fragment_start,
            fragment_end,
            track_id,
            trex_default,
        )
        if interval is None:
            return None
        if interval is not False:
            if matching_interval is not None:
                return None
            matching_interval = interval
    return matching_interval


def _iso_bmff_fragmented_audio_duration(content, top_level, movie_boxes, audio_track):
    """Return a non-overlapping fragmented audio timeline."""

    defaults = _iso_bmff_fragment_defaults(content, movie_boxes)
    if defaults is None:
        return None
    intervals = []
    track_id = audio_track["track_id"]
    for index, (box_type, box_start, box_end) in enumerate(top_level):
        if box_type != b"moof":
            continue
        if index + 1 >= len(top_level) or top_level[index + 1][0] != b"mdat":
            return None
        interval = _iso_bmff_moof_interval(
            content,
            box_start,
            box_end,
            track_id,
            defaults.get(track_id),
        )
        if interval is None:
            return None
        intervals.append(interval)
    if not intervals:
        return None
    intervals.sort()
    previous_end = None
    for decode_start, decode_end in intervals:
        if decode_end <= decode_start or (
            previous_end is not None and decode_start < previous_end
        ):
            return None
        previous_end = decode_end
    return (
        max(decode_end for _decode_start, decode_end in intervals),
        audio_track["timescale"],
    )


def _iso_bmff_audio_duration(content):
    """Return the verified audio timeline as integer ``(units, timescale)``."""

    if not is_iso_bmff_audio_only(content):
        return None
    top_level = _iso_bmff_boxes(content, 0, len(content))
    if (
        not top_level
        or not any(box_type == b"ftyp" for box_type, *_ in top_level)
        or not any(
            box_type == b"mdat" and box_end > box_start
            for box_type, box_start, box_end in top_level
        )
    ):
        return None
    movie_boxes = None
    for box_type, box_start, box_end in top_level:
        if box_type == b"moov":
            if movie_boxes is not None:
                return None
            movie_boxes = _iso_bmff_boxes(content, box_start, box_end)
    if movie_boxes is None:
        return None
    audio_track = _iso_bmff_audio_track(content, movie_boxes)
    if not audio_track:
        return None
    fragments_present = any(box_type == b"moof" for box_type, *_ in top_level)
    if not fragments_present:
        duration = audio_track["sample_duration"] or audio_track["header_duration"]
        return (
            (duration, audio_track["timescale"])
            if duration and audio_track["timescale"]
            else None
        )

    return _iso_bmff_fragmented_audio_duration(
        content,
        top_level,
        movie_boxes,
        audio_track,
    )


def _ogg_opus_duration(content):
    """Return the verified Opus timeline as integer ``(units, 48000)``."""

    if not is_ogg_opus(content):
        return None
    first_segment_count = content[26]
    first_packet_start = 27 + first_segment_count
    pre_skip = int.from_bytes(
        content[first_packet_start + 10 : first_packet_start + 12], "little"
    )
    stream_serial = content[14:18]
    last_granule = None
    offset = 0
    expected_sequence = 0
    page_count = 0
    saw_eos = False
    while offset < len(content):
        page_count += 1
        if (
            page_count > 65536
            or offset + 27 > len(content)
            or content[offset : offset + 5] != b"OggS\x00"
            or content[offset + 14 : offset + 18] != stream_serial
        ):
            return None
        header_type = content[offset + 5]
        sequence = int.from_bytes(content[offset + 18 : offset + 22], "little")
        if sequence != expected_sequence or (page_count == 1) != bool(
            header_type & 0x02
        ):
            return None
        if saw_eos or header_type & ~0x07:
            return None
        saw_eos = bool(header_type & 0x04)
        expected_sequence = (expected_sequence + 1) & 0xFFFFFFFF
        segment_count = content[offset + 26]
        table_end = offset + 27 + segment_count
        if table_end > len(content):
            return None
        body_size = sum(content[offset + 27 : table_end])
        page_end = table_end + body_size
        if page_end > len(content):
            return None
        granule = int.from_bytes(content[offset + 6 : offset + 14], "little")
        if granule != 0xFFFFFFFFFFFFFFFF:
            last_granule = granule
        offset = page_end
    if not saw_eos or last_granule is None or last_granule <= pre_skip:
        return None
    return last_granule - pre_skip, 48000


def canonical_recorded_audio_duration_seconds(content, mimetype):
    """Derive a positive whole-second duration from a supported audio container."""

    normalized_mimetype = (mimetype or "").split(";", 1)[0].strip().lower()
    if normalized_mimetype == "audio/ogg":
        duration = _ogg_opus_duration(content)
    elif normalized_mimetype == "audio/mp4":
        duration = _iso_bmff_audio_duration(content)
    else:
        duration = None
    if not duration or duration[0] <= 0 or duration[1] <= 0:
        raise ValidationError(
            _("The recorded audio duration could not be verified from its container.")
        )
    units, timescale = duration
    return max(1, (units + timescale - 1) // timescale)


def media_kind_for_mimetype(mimetype):
    value = (mimetype or "").split(";", 1)[0].strip().lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    if value:
        return "document"
    return False


def validate_media_metadata(kind, mimetype, size_bytes):
    if kind not in MEDIA_SIZE_LIMITS:
        raise ValidationError(_("Unsupported media kind: %s", kind or "missing"))
    normalized_mimetype = (mimetype or "").split(";", 1)[0].strip().lower()
    detected_kind = media_kind_for_mimetype(normalized_mimetype)
    # A provider's document container is authoritative for disposition.  Some
    # legitimate document formats live below another top-level MIME tree (DWG
    # is commonly reported as ``image/vnd.dwg``), so reclassifying them from
    # the MIME prefix would reject valid inbound documents.  Typed media still
    # require an exact image/audio/video family match.
    if not normalized_mimetype or (kind != "document" and detected_kind != kind):
        raise ValidationError(
            _("The media MIME type does not match its declared kind.")
        )
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
    ):
        raise ValidationError(_("Media size must be a positive integer."))
    if size_bytes > MEDIA_SIZE_LIMITS[kind]:
        raise ValidationError(
            _(
                "The %(kind)s file exceeds the %(limit)s MB limit.",
                kind=kind,
                limit=MEDIA_SIZE_LIMITS[kind] // (1024 * 1024),
            )
        )
    return normalized_mimetype


def enabled_media_kinds(capabilities):
    """Return enabled kinds from the structured provider media contract."""

    if not isinstance(capabilities, dict):
        return ()
    media_capabilities = capabilities.get("media") or {}
    if not isinstance(media_capabilities, dict):
        return ()
    return tuple(
        kind
        for kind in MEDIA_SIZE_LIMITS
        if isinstance(media_capabilities.get(kind), dict)
        and media_capabilities[kind].get("enabled", True) is True
    )


def validate_provider_media_capability(capabilities, kind, mimetype, size_bytes):
    """Validate one media object against a provider-neutral capability contract.

    ``capabilities.media`` is a map of
    ``kind -> {enabled, max_bytes, mimetypes}``. The module hard limits remain an
    upper bound even when a provider advertises a larger value.
    """

    normalized_mimetype = validate_media_metadata(kind, mimetype, size_bytes)
    if not isinstance(capabilities, dict):
        raise ValidationError(_("Provider capabilities must be a JSON object."))
    media_capabilities = capabilities.get("media") or {}
    if not isinstance(media_capabilities, dict):
        raise ValidationError(_("Provider media capabilities must be an object."))

    policy = media_capabilities.get(kind)
    if not isinstance(policy, dict):
        raise UserError(_("The provider does not support this media type."))
    enabled = policy.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError(
            _("The provider media 'enabled' setting must be a boolean.")
        )
    if not enabled:
        raise UserError(_("The provider does not support this media type."))

    caption = policy.get("caption", True)
    if not isinstance(caption, bool):
        raise ValidationError(
            _("The provider media 'caption' setting must be a boolean.")
        )

    max_bytes = policy.get("max_bytes")
    if max_bytes is not None:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
        ):
            raise ValidationError(
                _("The provider media byte limit must be a positive integer.")
            )
        if size_bytes > min(max_bytes, MEDIA_SIZE_LIMITS[kind]):
            raise UserError(_("The media file exceeds the provider size limit."))

    allowed_mimetypes = policy.get("mimetypes") or []
    if not isinstance(allowed_mimetypes, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in allowed_mimetypes
    ):
        raise ValidationError(
            _("Provider media MIME types must be an array of non-empty strings.")
        )
    normalized_patterns = {
        value.split(";", 1)[0].strip().lower() for value in allowed_mimetypes
    }
    if normalized_patterns and not any(
        pattern in ("*/*", normalized_mimetype)
        or (pattern.endswith("/*") and normalized_mimetype.startswith(pattern[:-1]))
        for pattern in normalized_patterns
    ):
        raise UserError(_("The provider does not support this media MIME type."))
    return normalized_mimetype


def validate_provider_recorded_audio_capability(
    capabilities, mimetype, is_voice_note, duration_seconds
):
    """Validate browser-recorded audio against an explicit provider contract."""

    if not duration_seconds and not is_voice_note:
        return True
    if (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or duration_seconds <= 0
    ):
        raise ValidationError(_("Recorded audio requires a positive duration."))
    media_capabilities = (
        capabilities.get("media") if isinstance(capabilities, dict) else None
    )
    audio_policy = (
        media_capabilities.get("audio")
        if isinstance(media_capabilities, dict)
        else None
    )
    if not isinstance(audio_policy, dict):
        raise UserError(_("The provider does not support browser audio recording."))
    recording_mimetypes = audio_policy.get("recording_mimetypes") or []
    voice_note_mimetypes = audio_policy.get("voice_note_mimetypes") or []
    for field_name, values in (
        ("recording_mimetypes", recording_mimetypes),
        ("voice_note_mimetypes", voice_note_mimetypes),
    ):
        if not isinstance(values, (list, tuple)) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValidationError(
                _(
                    "Provider audio %(field)s must be an array of MIME types.",
                    field=field_name,
                )
            )
    normalized_mimetype = (mimetype or "").split(";", 1)[0].strip().lower()
    normalized_recording = {
        value.split(";", 1)[0].strip().lower() for value in recording_mimetypes
    }
    if not normalized_recording or normalized_mimetype not in normalized_recording:
        raise UserError(_("The provider cannot accept this recorded audio format."))
    if is_voice_note:
        normalized_voice_notes = {
            value.split(";", 1)[0].strip().lower() for value in voice_note_mimetypes
        }
        if normalized_mimetype not in normalized_voice_notes:
            raise UserError(_("The provider cannot send this format as a voice note."))
    max_duration = audio_policy.get("max_duration_seconds")
    if (
        not isinstance(max_duration, int)
        or isinstance(max_duration, bool)
        or max_duration <= 0
    ):
        raise ValidationError(
            _("Provider recorded-audio duration must be a positive integer.")
        )
    if duration_seconds > max_duration:
        raise UserError(_("The recording exceeds the provider duration limit."))
    return True


def validate_provider_media_caption_capability(capabilities, kind, has_caption):
    """Reject a caption the provider cannot deliver for this media kind."""

    if not has_caption:
        return True
    media_capabilities = (
        capabilities.get("media") if isinstance(capabilities, dict) else None
    )
    policy = (
        media_capabilities.get(kind) if isinstance(media_capabilities, dict) else None
    )
    if not isinstance(policy, dict):
        raise UserError(_("The provider does not support this media type."))
    caption = policy.get("caption", True)
    if not isinstance(caption, bool):
        raise ValidationError(
            _("The provider media 'caption' setting must be a boolean.")
        )
    if not caption:
        raise UserError(_("This provider cannot send a caption with this media type."))
    return True


def upload_values_from_content(content, mimetype, filename):
    """Build the provider-neutral metadata persisted for one bounded upload."""

    kind = media_kind_for_mimetype(mimetype)
    normalized_mimetype = validate_media_metadata(kind, mimetype, len(content))
    return {
        "kind": kind,
        "mime_type": normalized_mimetype,
        "file_name": (filename or kind)[:255],
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
