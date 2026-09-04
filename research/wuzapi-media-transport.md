# WuzAPI media transport

- Status: implemented Phase 3 baseline
- Reviewed: 2026-08-21
- Baseline: WuzAPI `v1.0.8`, commit `9487eca`

## Decision

Keep WuzAPI running with `--skipmedia=true`. Full media files are fetched after the
webhook commit, so the Contact Center does not depend on WuzAPI's native S3 delivery.
Provider payloads may still contain inline thumbnails, previews or base64 fragments; the
sanitizer discards them and no inline binary is persisted.

For every supported media message, the adapter persists only bounded metadata and the
provider locator required by the pinned download API. After the webhook commits, the OCA
`queue_job` worker calls the matching endpoint:

| Kind     | Endpoint                 |
| -------- | ------------------------ |
| Image    | `/chat/downloadimage`    |
| Audio    | `/chat/downloadaudio`    |
| Video    | `/chat/downloadvideo`    |
| Document | `/chat/downloaddocument` |

The worker validates kind, MIME type, maximum size and SHA-256 before creating a private
`ir.attachment` linked to the canonical `mail.message`. Provider locators stay
restricted to Contact Center administrators. The UI serves attachments through an
authenticated Odoo route protected by ACLs and record rules; operational users are
scoped by native channel membership.

## Why native S3 is not the baseline

The pinned WuzAPI implementation was reviewed as part of Phase 3. Its S3 path does not
provide the access-control and retention semantics required by the Odoo domain: object
visibility is not private-by-default, the observed expiration setting is metadata rather
than verified deletion, and upload failures are not a durable delivery ledger. Those
properties make it unsuitable as the canonical attachment store for this phase.

This is a decision about the pinned revision, not a permanent rejection of object
storage. A future provider revision or a dedicated private MinIO/S3 service can be
adopted after contract tests prove private access, lifecycle deletion, per-account
isolation and retry/reconciliation behavior.

## Persistence boundary

The durable webhook envelope must never retain any of the following:

- `data:` URIs or base64 media bodies;
- thumbnails or preview blobs;
- waveform arrays;
- nested raw-message copies that duplicate the envelope.

It may retain only the provider fields needed to reproduce a later authenticated
download, plus declared MIME, size, hash, dimensions, duration and filename. The DTO
rejects binary values, inline data URIs, oversized locator strings and excessive
nesting.

## Outbound

The browser uploads one file to an authenticated Odoo route. Odoo stores a temporary
private attachment and returns an opaque UUID. Sending consumes that UUID atomically
with `mail.message`, message binding and outbox creation. Only the asynchronous provider
worker reads the attachment and encodes the provider request; binary content never
enters `CommandDTO` JSON, the outbox JSON, bus events or logs.

The current Phase 3 implementation supports one attachment per outbound message,
optionally with a caption. Provider capabilities advertise supported kinds, MIME
allow-lists and size limits so invalid files are rejected before an outbox command is
created.

## Implemented validation

The SERVIDOR05 acceptance run exercised real WuzAPI outbound media, reply, edit,
reaction and delete requests with HTTP 200 and every outbox created by that smoke in
`done`. The authenticated Odoo content route returned `206` for a valid Range request
and `416` for an invalid range. No tested media or mutation remained in a failed state.
Real inbound media was covered by fixtures and integrated tests rather than an external
device event in this run.

## Revalidation gate

Before changing the WuzAPI image, commit, media flags or storage mode:

1. refresh anonymized webhook and download fixtures;
2. re-run the adapter contract and size/hash failure cases;
3. audit webhook sanitization against the new payload shape;
4. validate all four download endpoints without persisting inline media;
5. document any storage or retention change here and in `plan.md`.
