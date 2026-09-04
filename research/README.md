# Contact Center Research

This directory records implementation research that supports the Contact Center
architecture for Odoo 16. Research notes explain protocol and framework behavior;
normative project decisions remain in [`plan.md`](../plan.md).

## Topics

| Document                                                                         | Purpose                                                                                                                                               |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`odoo-mail-guest-identity.md`](odoo-mail-guest-identity.md)                     | Defines how external participants are represented with `mail.guest`, resolved through a canonical identity, and optionally linked to `res.partner`.   |
| [`whatsapp-jid-lid-identifiers.md`](whatsapp-jid-lid-identifiers.md)             | Documents PN JIDs, LIDs, device JIDs, provider payload fields, alias persistence, resolution rules, and required test cases.                          |
| [`wuzapi-media-transport.md`](wuzapi-media-transport.md)                         | Records the pinned WuzAPI media-download contract, webhook sanitization boundary, attachment flow and S3 revalidation gate.                           |
| [`wuzapi-session-lifecycle.md`](wuzapi-session-lifecycle.md)                     | Records the exact lifecycle events, polling contract, own-identity validation, cycle deduplication, safe recovery and the ~20-number fleet baseline.  |
| [`wuzapi-event-coverage.md`](wuzapi-event-coverage.md)                           | Classifies every pinned WuzAPI webhook family as projected, intentionally unsupported or deferred, including the observability contract.              |
| [`wuzapi-group-metadata.md`](wuzapi-group-metadata.md)                           | Records authoritative group metadata pulls, hint events, technical PN/LID rosters, complete/partial snapshots, avatar bounds and the aggregate UI.    |
| [`inbox-owner-team-access.md`](inbox-owner-team-access.md)                       | Defines the union of direct inbox ownership and shared team access, including the optional authoritative CRM roster projection.                       |
| [`service-pipeline-core.md`](service-pipeline-core.md)                           | Defines provider-neutral service pipelines and cases, immutable transitions, and the optional CRM bridge contracts.                                   |
| [`meta-click-to-whatsapp-attribution.md`](meta-click-to-whatsapp-attribution.md) | Audits Meta/CTWA webhook metadata and defines the provider-neutral DTO, immutable touchpoint ledger, dedupe/enrichment and future CRM bridge.         |
| [`meta-messenger-instagram.md`](meta-messenger-instagram.md)                     | Records official Messenger/Instagram contracts and the implemented Phase 6.1–6.5 shared-runtime architecture, greenfield validation and future gates. |

## Architectural boundary

```text
Provider payload
    -> durable sanitized inbox envelope (no credentials or inline binary)
    -> OCA queue_job
    -> provider adapter normalization
    -> versioned DTO
    -> identity resolver
    -> mail.guest / mail.channel / mail.message
```

Provider adapters must preserve all observed identifiers and their roles. They must not
select a single phone number as the canonical person key. The core owns identity
resolution and the creation of Odoo domain records. Persisting the sanitized envelope
before normalization allows replay and forensic inspection even if an adapter or DTO
mapping fails, without retaining credentials or inline binary.

## Source policy

- Prefer primary source code over provider documentation or marketing material.
- Pin source links to reviewed commits whenever practical.
- Record the review date because unofficial WhatsApp protocol behavior changes.
- Revalidate dependency versions and payload fixtures before implementation or an
  adapter upgrade.
- Preserve representative raw payload fixtures in adapter tests; never infer a contract
  from one observed payload.

Last consolidated review: **2026-09-01**.
