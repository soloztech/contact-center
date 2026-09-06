Implemented
===========

* Shared, authenticated and deduplicated Meta webhook ingress.
* Messenger and Page-linked Instagram routing.
* Direct inbound text, replies, echoes, referrals and supported state mutations.
* Provider-private inbound media download and identity profile synchronization.
* Direct text and private media outbound with exact provider correlation and a
  24-hour response window, rechecked after the upload.
* Shared-post, reel and story context cards with safe public permalinks or private
  media, preserving text and original attachment slots.
* Revision-fenced health and subscription reconciliation through OCA ``queue_job``.

Next increments
===============

* External acceptance with a managed System User credential and App Review.
* Outbound reactions.
* Explicitly approved Human Agent capability.
* Optional Instagram Login transport without a linked Facebook Page.
