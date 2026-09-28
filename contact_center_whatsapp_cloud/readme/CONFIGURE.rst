Create the resources in the shared Meta core:

#. Create one ``meta.api.app`` with the external App ID, Graph version and an App
   secret reference, and one ``meta.webhook.endpoint`` with a verify-token reference.
#. Create a ``meta.webhook.page`` with owner kind *WhatsApp Business Account*, the
   business account ID and the system-user token reference (environment variable or
   mounted file). Its WhatsApp asset is created automatically and stays
   *Configured on activation*.
#. Create an inbox with platform ``whatsapp``.
#. In *Contact Center > Configuration > Provider Connections*, select *WhatsApp Cloud
   API*, the business account asset and the phone number ID. The Graph version
   defaults to ``v26.0``.
#. Click **Register WhatsApp Consumer** to add this addon's ``messages``
   subscription on the business account owner.
#. Subscribing the App on the business account (``subscribed_apps``) and the
   callback configuration are activation steps outside Odoo and require explicit
   authorization.
#. Run the provider health check (a read of the phone number) and then enable
   inbound/outbound traffic.

The shared webhook accepts bodies up to 3 MiB; proxies in front of the callback must
accept the same size. The route (asset and phone number ID) is immutable: archive
the connection and create another one to change it.

Failures
========

*Contact Center > Configuration > WhatsApp Cloud Failures* lists failed delivery
statuses and business account webhook errors (code and title only) for
administrators of the connection's company. The list is read-only.
