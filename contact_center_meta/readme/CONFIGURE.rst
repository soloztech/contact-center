Create the Meta resources in the shared integration core:

#. Create one ``meta.api.app`` with the external App ID, Graph version and an App
   secret reference.
#. Create one ``meta.webhook.endpoint`` with a verify-token reference.
#. Create each ``meta.webhook.page`` with its Page-token reference and add linked
   Instagram assets when needed.
#. In *Contact Center > Configuration > Provider Connections*, select ``Meta`` and
   bind the connection to the exact messaging asset.
#. Click **Configure Meta Webhook** and wait for the endpoint and Page to report a
   fresh ``In Sync`` read-back.
#. Run the provider health check and then enable inbound/outbound traffic.

Credential fields contain environment-variable or mounted-file references only.
Changing an external route requires archiving the connection and creating another
one; the asset binding and logical account identity are immutable.

Media sending requires the Page token and App Review permissions for the selected
transport. For Page-linked Instagram, verify the upload grants listed by Meta
(``instagram_basic``, ``instagram_manage_comments``, ``instagram_manage_messages``
and ``pages_messaging``) and the Page's MESSAGING task. Validate acceptance with the
dedicated App before enabling production traffic; a local test does not grant these
permissions.

The effective limits are 16 MiB for Messenger images and audio, 8 MB for Instagram
images, 16 MiB for Instagram audio, and 25 MB for video and PDF in either transport.
Messenger accepts JPEG/PNG/GIF; Instagram accepts JPEG/PNG. Supported audio formats
are AAC, M4A/MP4 and WAV, plus MP3/OGG for Messenger. Supported video formats are
MP4, OGG, AVI, MOV and WebM. Documents are limited to PDF. Captions and voice notes
are not enabled for Meta media sends.

Routing and attribution diagnosis
=================================

Start with the shared endpoint's authenticated delivery and consumer result. Then
verify the exact Page/Instagram messaging asset, logical account, primary connection
and Contact Center inbox event/job. Unknown or ambiguous assets have no fallback
inbox. Shared webhook acceptance alone does not prove messaging delivery succeeded.

For an outbound failure, verify health, token grants, asset identity and the current
24-hour response window before inspecting the outbox result. An uncertain send
requires provider evidence; it must not be retried as a new business request.

For an ad-origin card, inspect the referral's observed identifiers and evidence level.
A safe title or thumbnail does not identify a campaign by itself. Optional catalog
matching and CRM attribution follow the separate
`Marketing Center contract
<https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md>`_.
