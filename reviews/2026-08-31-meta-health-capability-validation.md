# Meta health by capability — validation record

Date: 2026-08-31

## Decision

`pages_read_engagement` is not an operational messaging prerequisite. Missing Page
profile metadata must not turn Messenger or Instagram into disconnected channels.

The canonical states are now:

- `connected + healthy`: messaging and optional metadata verified;
- `connected + metadata_limited`: messaging verified, optional Page metadata absent;
- `degraded`: an operational permission, subscription or conclusive identity proof is
  missing;
- `authentication_required`: the credential is invalid or expired.

## Production-independent provider evidence

The authenticated Meta App dashboard was inspected without changing its configuration:

- the expected Facebook Page is attached to the Messenger use case;
- all eight Messenger fields required by the adapter are subscribed;
- all five Instagram messaging fields required by the adapter are subscribed;
- Meta identifies `pages_messaging` as the permission used to send and receive Messenger
  messages;
- `pages_read_engagement` is described for Page content, follower/profile data and
  metadata.

The current Page token was inspected without exposing the token:

- it is valid, belongs to the configured App and has type `PAGE`;
- its `profile_id` is the configured Page;
- granular `pages_messaging` and `pages_manage_metadata` targets are the configured
  Page;
- granular `instagram_basic` and `instagram_manage_messages` targets are the configured
  Instagram account;
- `pages_read_engagement` is absent;
- both `/{page-id}?fields=id,name` and `/me?fields=id` are rejected by Graph without
  that optional permission, while the page subscription edge is available.

No Meta token was regenerated and no additional permission was requested.

## Implementation

- `contact_center_meta.services.health` uses `debug_token.profile_id`, granular scope
  targets and subscription readback as operational proof.
- Page name, tasks and linked Instagram metadata are best-effort and recorded through
  `page_metadata_state`.
- `contact.center.provider.connection` preserves `metadata_limited` while the canonical
  state remains `connected`.
- outbound credential policy degrades health only when that connection is actually an
  active outbound route; an inbound-only Messenger/Instagram inbox remains connected,
  while activation of outbound continues to fail closed through the outbound gate.
- the UI counts those channels as connected and exposes a provider-neutral advisory.
- a terminal provider exception no longer masquerades as a proven identity mismatch.

## Validation

- targeted pre-commit gates: passed;
- canonical isolated base suite: `365` tests, `0` failures, `0` errors;
- canonical isolated integrated suite: `741` tests, `0` failures, `0` errors;
- targeted `TestMetaPhase65Health`: `27` tests, `0` failures, `0` errors;
- deployed source integrity on SERVIDOR05: `257` files, tree
  `d2b065d8602bd3392e32ed391b10967ac2f602e43ccb5c3ff9ee3ef748b09824`;
- live authorization observation: `valid`, subscription `in_sync`, Page identity
  confirmed and metadata `limited`;
- live provider projection: Messenger and Instagram both `connected + metadata_limited`,
  with the previous identity safety latch cleared;
- fleet snapshot: `7/7` connected, `0` degraded, `0` disconnected and `0`
  authentication-required.

Evidence directories:

- integrated suites:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260831T175250543701Z`;
- targeted Meta suite:
  `scans/raw/20260831-meta-health-inbound-only-targeted/20260831T180230920916Z`;
- final source deploy:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260831T180135282197Z`.

Production was not accessed or changed.

## Follow-up: operational Attention semantics

Operator review of the deployed `16.0.1.18.8` UI found that the advisory looked like a
connection error because it increased the Attention badge and appeared beside real
authentication failures. The local follow-up changes only the UI classification:

- `metadata_limited` remains a connected health detail and remains available for
  diagnostics;
- only a non-connected canonical state enters the Attention filter;
- optional metadata alone does not degrade the aggregate fleet indicator;
- a regression models the live shape with two Meta connections and one disposable WuzAPI
  migration connection requiring login.

This follow-up is not part of the deployed source or validation counts above. A new
laboratory release and authenticated QUnit/browser acceptance remain pending explicit
deployment authorization.
