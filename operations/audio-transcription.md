# Audio transcription

Transcription is built into `contact_center_base` and `contact_center_ui` and is
disabled by default. No additional Odoo addon is required. Incoming voice notes and
audio files use the same pipeline for WuzAPI and Meta.

## Configuration

1. In **Configuration → Transcrição de áudio**, create a provider in the inbox's
   company. Only Contact Center administrators can manage these configurations.
2. Choose **OpenAI** or **OpenAI-compatible service**, enter the model and API key.
   OpenAI uses the fixed `https://api.openai.com/v1` endpoint. A compatible service
   requires its API base URL, including a prefix such as `/v1` where applicable.
3. Set the language hint (`pt` by default, empty for detection), optional vocabulary,
   timeout and maximum known audio duration.
4. In the inbox's **Transcrição de áudio** tab, choose the provider and **Manual** or
   **Automatic** mode. Manual mode exposes **Transcrever** beneath eligible audio.
   Automatic mode queues transcription when an incoming audio download completes.
   Enabling it does not scan or bill the existing history. Older available audio can be
   requested manually.

The API key field is masked and accessible only to administrators. Alternatively,
specify an environment variable starting with `CC_TRANSCRIPTION_`; this takes precedence
over the stored key. All Odoo processes that execute transcription jobs must receive
that variable. Credentials are never included in queue arguments, transcript snapshots,
operational DTOs, or provider error messages.

This feature needs a **speech-to-text model**. A text-only LLM cannot be selected as a
substitute for an audio transcription engine.

## Local faster-whisper

`faster-whisper` is a Python inference library. To select it from these settings, run an
HTTP service that wraps it and implements:

- `POST {base_url}/audio/transcriptions`, multipart upload;
- fields `file`, `model`, `response_format=json`, optional `language` and `prompt`;
- optional bearer authentication;
- a JSON response such as `{"text": "Preciso de duas peças.", "language": "pt"}`.

Use **OpenAI-compatible service** with the wrapper's model identifier, such as
`large-v3` if that service accepts it. Installing the Odoo code does not install
Whisper, download model weights, allocate a GPU or start an inference server. Run
inference in a separately sized service. LAN HTTP is accepted for an explicitly
configured service; HTTPS is required for the official OpenAI endpoint. Redirects and
URL credentials/query strings/fragments are rejected.

## Extending providers

Services using the multipart contract above need only a new configuration record.
Different APIs can register a Python adapter from an installed addon:

```python
from odoo.addons.contact_center_base.services.transcription import (
    TranscriptionAdapter,
    TranscriptionResult,
    transcription_registry,
)


@transcription_registry.register("vendor_speech", label="Vendor Speech")
class VendorSpeech(TranscriptionAdapter):
    def transcribe(self, config, request):
        self.validate_config(config)
        # Use the vendor client with bounded I/O and sanitized exceptions.
        text = vendor_client_transcribe(config, request)
        return TranscriptionResult(text=text)
```

Import the registration module from the addon's `__init__.py`. Registration makes the
provider available in the selection without changing the inbox, media model, queue
orchestration or frontend. Raise `TranscriptionError` using a documented symbolic code
for failures; never include the audio, transcript or credential in exceptions or logs.
The example's `vendor_client_transcribe` is supplied by the extension, not by Contact
Center.

## Processing and lifecycle

- The job stores the selected provider/model and non-secret request settings when
  queued. Editing the model affects new requests. Completed transcripts remain linked to
  their original model and audio; they are not automatically regenerated.
- Switching an inbox to another provider or disabling the inbox/provider before
  execution skips an already queued request. There is no automatic provider fallback
  that could send audio to an unselected external service.
- Changing a provider's endpoint, backend, key or key reference invalidates pending
  requests through a routing revision. A new credential is never combined with an old
  queued endpoint. Environment-variable value rotation should retain the same provider
  identity; change the configured reference when changing providers.
- Transcription uses `root.contact_center.transcription`. Reserve a small capacity such
  as `root.contact_center.transcription:1` alongside the existing JobRunner
  configuration. It performs no provider I/O in webhook ingress or UI requests.
- Requests deduplicate by media identity and active job. Successful results are reused.
  A lost or terminated queue job becomes available for manual retry when the
  conversation is loaded again; it does not remain stuck in the processing state.
  Transient failures have bounded retries honoring `Retry-After`. An HTTP timeout or a
  database retry after external I/O can still repeat a billed request; the integration
  does not promise exactly-once provider billing.
- Files are capped at 25,000,000 bytes and provider responses at 1 MiB; transcript text
  is capped at 100,000 characters. Known duration above the configured limit is skipped.
  Duration metadata can be absent, so this is not a hard monthly spending quota or a
  guarantee of audio duration. Larger inputs are not chunked.
- Supported Ogg audio is uploaded as Ogg, without conversion. Other input formats depend
  on the selected adapter. The API-compatible service must actually support the received
  codec/container. No FFmpeg process runs in Odoo.
- Transcript text is rendered as escaped text, separately from the customer's original
  message. It does not send a message, mark a conversation read, change its responsible
  agent or change the original audio.
- Conversation permissions apply to requesting and reading transcripts. Redacted message
  deletion erases derived text. Retained struck-through content follows the existing
  inbox policy. History retention removes the media's transcript and its transcription
  job history, and defers deletion of jobs that are running.

## Validation

Adapter tests are standalone and use no API credentials or external HTTP calls:

```bash
python3 -m unittest discover -s contact_center_base/tests \
  -p test_transcription_adapters_standalone.py
```

Native Odoo tests use synthetic conversations and mocked speech calls. QUnit tests are
grouped under `contact_center_ui transcription`. Real accuracy, provider account access
and performance of a local inference server require a later pilot with an explicitly
selected provider and representative audio.

Sources:
[OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).
