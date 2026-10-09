# Contact Center event taxonomy

The existing bus transport retains `schema_version: 1` and existing event names.
Metadata is an opt-in meaning of `conversation_updated`, not a second event. Only
`update_scope_version: 1` with a recognized `update_scope` and an exactly equal
singleton `changed_fields` list permits the metadata path. Recognized scopes are
`identity_avatar`, `group_metadata`, `identity_name` and `identity_aliases`. Missing
versions, mixed fields, unknown scopes and unknown events retain the existing full
refresh behavior.

| Writer or cause                                                              | Existing event and scope                                                                       | List/store handling                                                                                          | Systray handling                     |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| `identity_avatar.py` accepted avatar projection                              | `conversation_updated`, exact `identity_avatar`                                                | Existing `get_conversation_avatars` lane; no extra list/delta read                                           | Ignore exact metadata                |
| `group.py::_notify_updated` provider group profile projection                | `conversation_updated`, exact `group_metadata`                                                 | Authorized metadata delta or list fallback; selected detail refreshed, timeline preserved for metadata alone | Ignore exact metadata                |
| `identity.py::_contact_center_sync_managed_name` name-only writer            | `conversation_updated`, exact `identity_name`                                                  | Authorized metadata delta or list fallback; selected detail refreshed                                        | Ignore exact metadata                |
| `application.py::_enrich_identity_aliases` aliases-only material observation | `conversation_updated`, exact `identity_aliases`                                               | Authorized metadata delta or list fallback; selected detail refreshed                                        | Ignore exact metadata                |
| `application.py::_finish_identity_resolution` changes both alias and name    | One legacy `conversation_updated` per affected channel, mixed `changed_fields`                 | Existing full list/selected detail/timeline path                                                             | Existing refresh                     |
| Portable identity merge, including subsequent alias/name projection          | One legacy `conversation_updated` per affected channel, final `identity_id`, no metadata scope | Existing full path; identity ID changes require fresh detail                                                 | Existing refresh                     |
| Assignment, ACL, account policy or other conversation mutation               | Legacy `conversation_updated`                                                                  | Existing full path                                                                                           | Existing refresh                     |
| Human message create/update/delete                                           | `message_created/message_updated/message_deleted`                                              | Existing full path and selected timeline when relevant                                                       | Existing unread/summary refresh      |
| Conversation deletion                                                        | `conversation_deleted`                                                                         | Existing full path and selection removal                                                                     | Existing refresh                     |
| Current actor read pointer                                                   | `member_seen`                                                                                  | Existing monotonic read handling and refresh                                                                 | Existing unread handling             |
| Another actor read pointer                                                   | `member_seen`                                                                                  | Ignore for this actor's unread count                                                                         | Ignore for this actor's unread count |
| Delivery status                                                              | `delivery_updated`                                                                             | Existing delivery path                                                                                       | Existing handling                    |
| Personal pin or mute                                                         | `conversation_preference_updated`                                                              | Existing personal revision and full list path                                                                | Existing handling                    |
| Identity/contact link, reaction, media, productivity, unknown invalidation   | `identity_updated/reaction_updated/media_updated` and existing event names                     | Existing full path, including selected detail/timeline when relevant                                         | Existing refresh                     |
| Reconnect                                                                    | Existing bus reconnect signal                                                                  | Full request wins, clears metadata IDs; repair remains active                                                | Existing refresh                     |

Group metadata contains provider display/profile data; it does not represent
conversation membership or account access changes. Name and alias changes can affect
server search membership, so the client never treats those events as an authoritative
local name patch. Active search/unread filters, pagination, stale tails, uncertain bulk
reads, an empty or over-100 window and selected conversations preserved outside the
domain use the full path.

The identity resolver defers name and alias notifications until convergence. Private
process-local context tokens distinguish aggregation and merge; JSON RPC context cannot
provide those tokens. Name writers inside merge honor the existing
`contact_center_skip_name_notification` context. At completion the resolver emits
exactly one legacy invalidation per affected channel after aliases, bindings and the
final name have converged. It never labels an identity rewire as metadata. Material
alias observations emit one aliases event; timestamp-only observations emit none.
Standalone portable merge also emits one legacy event per channel.

Metadata fallback refreshes list and selected detail without adding a selected timeline
request unless another event has escalated the cycle. Message, reaction, media,
identity, mixed and unknown classes retain their established timeline behavior. An
invalid delta envelope or a changed window schedules the full request in the same
refresh cycle; an incomplete requested full refresh cannot be reported as a successful
cycle. The native 30-second/5-minute repair cadence remains in place.

Native tests in `test_identity_name.py` check exact singleton name/aliases scopes, mixed
convergence and merge followed by rename/alias publication once per channel with the
final identity. UI dispatch and RPC-count tests verify consumer routing independently;
the event shape alone does not establish that proof.
