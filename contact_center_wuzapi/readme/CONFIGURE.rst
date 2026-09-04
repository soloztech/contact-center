Create a WhatsApp account under *Contact Center > Configuration > Accounts*. Then open
*Contact Center > Configuration > Provider Connections*, create a connection and select
``WuzAPI`` in the *Provider* field. The provider selector is owned by the generic
Contact Center model; installing this addon adds the WuzAPI choice and injects a
provider-specific *WuzAPI* tab into the same form.

Configure the service URL, API token and webhook HMAC secret in that tab. The webhook
routing key is generated automatically, while the validated provider version and
commit are read-only. There is no separate WuzAPI configuration model or menu. Future
provider addons must extend the same form with their own conditional tab.

Only Contact Center administrators can access provider connections and their
credentials. Do not copy real credentials into fixtures, logs, DTOs or bus events.

Webhook request limit
=====================

The addon accepts at most 1 MiB per webhook. On the validated Odoo 16/Werkzeug stack,
JSON bodies reach the controller as a bounded stream, including requests without a
``Content-Length`` header. If a reverse proxy or WSGI middleware is configured to
buffer request bodies before Odoo, configure the same 1 MiB limit on that earlier
layer: a controller cannot undo memory already allocated upstream.

Group metadata subscriptions
============================

For inbound, read-only group metadata, include ``GroupInfo``, ``JoinedGroup`` and
``Picture`` in the WuzAPI webhook subscriptions alongside the existing message and
lifecycle events. They are hints only: after the signed webhook commits, an idempotent
OCA job performs the authoritative ``GET /group/info?groupJID=...`` pull. Do not map
the event body directly into the roster.

The pinned effective contract uses ``POST /user/avatar`` with a JSON ``Phone`` and
``Preview=true`` even though older provider examples show another method. Group
metadata reads require an active, healthy, identity-verified provider connection. The
complete snapshot TTL is six hours; partial results retry after 15 minutes. Avatar
downloads are limited to 2 MiB and never expose the provider token or remote URL to the
browser.
