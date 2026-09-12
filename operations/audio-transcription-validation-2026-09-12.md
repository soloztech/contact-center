# Audio transcription validation — 2026-09-12

Audio transcription is included in the existing Base and UI addons, version
`16.0.1.5.0`. The default remains disabled. Configuration selects an OpenAI or
OpenAI-compatible speech service; the latter can wrap faster-whisper. No inference
server, model weights or provider credentials were provisioned.

Development used `feat/audio-transcription-providers` in a separate worktree based on
`fc6a76f207a11f1080ae845982b3c13c9c444ca6`. Existing uncommitted research work in the
original checkout was left with its owner.

## Native and adapter tests

The native Odoo run used source commit `5a08bf63c616c267e2cba1baeab023547c080237`, with
archive SHA-256 `3723d9ce5d62afc3edefca7c4350ba938ed5580a11321f8b31137176650416c7`:

| Suite                      | Passed | Scope                                                                                       |
| -------------------------- | -----: | ------------------------------------------------------------------------------------------- |
| Audio transcription ORM    |     29 | Modes, queue ownership, snapshots, retries, company boundaries, ACL, deletion and retention |
| Existing media security    |     13 | Authorized media delivery and supported media identification                                |
| Existing history retention |     22 | Deletion lifecycle and preserved business references                                        |
| Standalone speech adapters |     16 | Multipart contract, validation, bounded HTTP and sanitized failures                         |

All 64 selected native tests ran, with zero errors or failures. All 16 standalone tests
passed. Provider calls were mocked; these results do not measure recognition accuracy or
confirm access to a real provider account.

The reproducible runner is [`transcription_qa.py`](transcription_qa.py). Evidence is
stored in the infra workspace under
`scans/raw/20260912-contact-center-transcription-qa-native-02/summary.json`. The fresh
synthetic database is `cc_transcription_qa_20260912_184940_60258564f4`; the staged
source is `/tmp/cc-transcription-qa.RozG3BgF` inside the LAB container.

## UI component tests

All 10 QUnit test bodies passed in a Node/jsdom harness, with 54 assertions and zero DOM
errors. The harness loaded the actual Contact Center components and six compiled
templates, using Owl 2.8.1 from the local OCB source. Its Owl SHA-256 is
`c521f96b638d40070f51022730d7cffff6db0a31543c43bd5b5d3f29149f331b`.

The harness uses mocked Odoo test services/helpers and does not exercise the native Odoo
asset loader, browser layout or audio playback. Dialog and DeferredImage must remain
unused in these cases; media pause is shimmed. Node old-space was capped at 384 MiB,
with approximately 151 MiB peak sampled RSS.

The added retry regression failed before the correction and passed afterward:
`failed → pending → failed` now restores the retry button instead of reviving an old RPC
response. Other cases cover redaction, escaped text, copy/expand, duplicate clicks,
authorization descriptors and a late RPC response after a completed bus update.

Results, source hashes and the reproducer are stored under
`scans/raw/20260912-contact-center-transcription-node-dom/`, including `results.json`
and `results-before-fix.json`. The UI changed after the native backend run only to fix
this projection race and add its regression; the backend source is unchanged from the
native tested commit.

Native Odoo/Chromium QUnit remains pending: WSL had less than the required 3 GiB of
available memory. This DOM run does not replace native browser acceptance. The suite
filter is `contact_center_ui transcription`, with 10 expected tests.

## Runtime boundaries

Only SERVIDOR05 was used. The runner created a new empty database through Odoo and
loaded synthetic fixtures, with no HTTP listener, no cron workers and queue capacity
`root:0`. The database and stage remain available for review.

`backup_required: false`: the user explicitly prohibited Odoo backups during this
development. No backup or copy of an existing database was created. The source archive
contains Git source only.

Before/after fingerprints of `odoo16` and `odoo16_dbmanager` matched, including their
container identity, start time, restart count, main PID and configuration hash. No
active service was restarted or upgraded. Production was not accessed, no real audio was
sent, and no authorized fiscal document was changed. The temporary HTTP process, tunnel
and browser session from the initial UI attempt were closed by their owner.

The native run ended at 18:50:06 UTC with both existing services preserved. A separate
read-only observation at 18:57:03 UTC found both containers stopped at approximately
18:52 UTC, with exit code 0 and `OOMKilled=false`. This later stop was not executed by
this task, and no restart was attempted. The observation is recorded in
`scans/raw/20260912-contact-center-transcription-node-dom/lab-state-after-native.json`;
the cause and operator were not investigated.

Source rollback consists of reverting the feature merge; no active deployment needs
database rollback. Installing or enabling the feature in an active instance is a
separate operation. The configuration and provider contract are documented in
[`audio-transcription.md`](audio-transcription.md).

## Review

Independent reviews covered provider configuration, the speech adapter contract, queue
and content lifecycle, and the UI projection. Changes include recovery of interrupted
queue jobs, invalidation after routing or credential changes, archived account company
checks and protection against injected ORM context defaults.

Black 22.8, Isort 5.12, Flake8 5.0, mandatory pylint-odoo 8.0.19 and OCA module checks
v0.0.25 passed for the changed code or affected modules. ESLint 8.24, Prettier 2.7.1
with its XML plugin, XML parsing and `git diff --check` also passed.

The separate
[ad-origin preview study](../research/ad-origin-preview-study-2026-09-12.md) describes
the required capture, thumbnail and UI changes. No ad-preview functionality was
implemented as part of this delivery.
