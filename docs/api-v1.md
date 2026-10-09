# Conversation list API v1

`contact.center.ui.api.list_conversations(limit=50, offset=0, filters=None, cursor=None, projection=None)`
retains its full DTO default for integrations and bulk-read helpers.
`projection='list_v1'` selects a dedicated compact serializer; any other non-`None`
projection raises `ValidationError`. The page retains `schema_version: 1`, `items`,
`total`, `has_more` and `next_cursor`, and the opt-in page adds `projection: 'list_v1'`.

The effective domain is the native member, active company and filter domain. Ordering
remains personal pins followed by descending activity and channel ID. The existing
segment-aware cursor and limit of 100 remain in force. `total` is the native
`search_count` on the initial page and remains `False` on cursor pages.

## Compact rows

A `list_v1` row has exactly these top-level keys:

```text
projection, channel_id, conversation_type, name, state, ignored, unread_count,
first_unread_message_id, preference, last_activity_at, account, platform,
provider, responsible, tags, last_message, identity, group, capabilities
```

`account` contains `id/name/platform`; `identity` contains `id/name/avatar_url` or is
`False`. Identity and conversation names resolve the same linked partner name as the
full serializer. A group name resolves its profile name, then channel name, exactly as
the full serializer does. `responsible`, `tags`, personal preference, unread anchor and
the existing compact message preview are retained.

The group projection retains all seven existing list fields:
`display_name/avatar_url/participant_count/admin_count/own_role/metadata_state/ last_synced_at`.
This group projection was already compact; there is no claimed roster removal or
invented byte saving from roster data.

`capabilities` contains only boolean `delete_conversation` and `ignore_conversation`
policies, computed once per account and operation with the native management helper.
These flags support row menus. Each endpoint still authorizes its actor and channel. A
compact row cannot authorize sending, composition, attribution, retention or
partner/company operations. Those require the full authorized `get_conversation` result
for the current opening operation.

The compact serializer does not build a full DTO and discard fields. Its prefetch skips
partner/company projections and group sender protocol resolution; it never serializes
full identity aliases, partner contacts, provider connection detail, access grants,
sending capabilities or retention. The default prefetch and list serializer remain full,
including `mark_conversations_read` responses.

## Authorized reconciliation

```text
reconcile_conversations(channel_ids, window_ids, filters=None,
                        projection='list_v1')
```

Both ID arguments must be lists of 1–100 positive exact integers; booleans, strings and
fractional values fail validation. Duplicates are removed without reordering. Neither
client IDs nor extra filter keys act as an authorization proof, raw domain, offset or
SQL input.

The server recomputes the first `len(window_ids)` authorized IDs using the same private
window helper as `list_conversations`. The helper chooses channels, preferences, count
and cursor without serializing rows or calculating their exact unread counters or
first-unread IDs. An effective unread-only domain can still use its existing membership
unread lookup. The UI excludes active search, unread-only, empty, paginated or uncertain
windows from the metadata delta lane.

Any ordered ID mismatch, including access loss, filter membership, pin changes, new
entries or reordering, returns only:

```json
{"schema_version": 1, "refresh_required": true, "items": []}
```

No replacement IDs or invisible DTOs are disclosed by this fallback. A matching window
returns `refresh_required: false`, the identical authorized `window_ids`, `items` only
for `affected ∩ window` in native list order, and authoritative
`total/has_more/next_cursor`. Prefetch and unread projection cover only that
intersection. An affected channel outside the window produces no row.

The request does not contain previous `has_more`, `total` or cursor values, so the
server cannot classify a metadata-only change to previous page metadata as a window
mismatch. It returns the newly authoritative metadata when IDs match, as required by the
binding F05 success envelope. Native tests cover a changed total and a changed
`has_more` outside an unchanged window.

## Reproducible proofs

`contact_center_base/tests/test_list_projection.py` covers the exact row contract,
recursive shared-field parity (including different linked partner and group profile
names), detail-prefetch exclusion, batched menu policies, unchanged default/full
bulk-read responses and invalid opt-in projections. Its synthetic 50-row benchmark logs
`E2_SYNTHETIC_LIST_BENCHMARK` with full/compact bytes and SQL counts after equivalent
cache invalidation. These are synthetic observations, not production percentages.

`contact_center_base/tests/test_conversation_delta.py` covers native envelope and row
parity, strict IDs, member/company reauthorization, ordering/filter/pin/new entry
fallbacks, changed outside-window metadata and bounded unread SQL. Native execution
remains required; syntax and diff checks do not replace it.
