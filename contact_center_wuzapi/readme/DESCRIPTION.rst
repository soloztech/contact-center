This addon provides the WuzAPI-specific configuration and provider adapter seam for
``contact_center_base``.

It registers ``WuzAPI`` in the generic provider selector and extends
``contact.center.provider.connection`` with a conditional WuzAPI configuration tab.
The fields remain owned by this addon; there is no provider-specific configuration
model or menu in parallel with *Provider Connections*. This is the extension pattern
expected from future provider addons.

The adapter authenticates inbound webhooks, normalizes supported events and performs
provider HTTP reads and commands only from the asynchronous boundaries owned by the
base addon. WuzAPI payloads and credentials never become the domain API exposed to the
browser.

The compatibility baseline is WuzAPI ``v1.0.8`` at commit ``9487eca``. Behaviour from
other revisions must be revalidated before it is incorporated.

For inbound groups, ``GroupInfo``, ``JoinedGroup`` and group ``Picture`` webhooks are
treated only as synchronization hints. The adapter obtains the authoritative snapshot
with ``GET /group/info?groupJID=...`` and resolves the preview locator with
``POST /user/avatar``. It normalizes complete or partial technical rosters, participant
roles and PN/LID aliases without creating Odoo people or channel memberships. Avatar
bytes are bounded to 2 MiB and returned to the core for private local persistence;
provider/CDN URLs are not exposed to the UI.

Acquisition evidence is separate from CRM intake
================================================

The addon depends on ``contact_center_base`` and has no CRM dependency. WhatsApp
``externalAdReply``, entry-point and UTM observations are normalized into Base's
attribution DTOs. An isolated ``conversionSource=fbads`` hint is classified as
``paid_ad_signal`` with ``provider_hint`` evidence; it neither identifies a campaign
nor qualifies the conversation for CRM.

A supported ad referral with an identifier, or an explicit CTWA entry point, can
carry stronger provider evidence. Identifiers such as an ad source and a CTWA click
ID retain distinct roles. Even stronger evidence does not create a lead. Commercial
actions and optional Marketing Center attribution have their own policies.
