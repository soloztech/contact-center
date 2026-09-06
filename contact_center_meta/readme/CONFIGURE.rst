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
