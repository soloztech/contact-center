# Ad preview and conversation-scoped transcription validation

> **Registro histórico arquivado em 15/09/2026.** Este texto descreve a revisão ou o
> plano da época. Não define o estado atual nem autoriza operações. Consulte o
> [README do projeto](../../../README.md) e o [roadmap atual](../../roadmap.md). O
> arquivamento não declara todos os achados resolvidos.

Validated on 2026-09-12 using synthetic data and blocked external networking.

Tested Contact Center commit: `6a9bf27cd15743f1f9d43466dcda1b494371a3ba`. Tested
Marketing Center commit: `24e48041c92800a63cc526f92d8ed9d7a5abccec`.

## Results

- **161 native Odoo tests passed**, zero failures/errors, every selected method
  confirmed in the test log. This includes 33 ad-preview core cases, 9 provider cases,
  25 Marketing cases, 40 transcription cases, 18 attribution cases, 13 media permission
  cases, 22 retention cases and one native asset integration case.
- **26 standalone tests passed** on Python 3.10.20: 10 bounded thumbnail transport tests
  and 16 transcription adapter tests. All HTTP responses and images were synthetic.
- **23 UI tests passed**, 169 assertions, using the actual Owl components and QUnit test
  bodies in Node/jsdom. Source SHA256 values are recorded.
- The native asset test separately resolves Odoo's installed asset graph, applies the
  backend XML inheritance through `AssetsBundle.xml()`, checks the actual JS transpiler
  and confirms the card does not remove the audio player or transcript. DOM tests do not
  replace browser layout/playback validation.
- Black, isort, Flake8, mandatory pylint-odoo, OCA module checks and relevant frontend
  ESLint/Prettier checks passed. Model import order is explicit because Odoo extensions
  must follow the models they inherit.

## Environment and evidence

The runner created a fresh local PostgreSQL cluster with a private Unix socket, 32 MiB
shared buffers and JIT disabled, plus a separate Odoo data directory. Odoo ran inside an
isolated network namespace with no external connectivity, workers or cron processing.
Odoo's test framework can start an internal HTTP listener despite `--no-http`; it
remained on a loopback interface inside that namespace. All task-owned processes were
stopped after the run.

No existing Odoo database, filestore or shared server instance was changed. No Odoo
backup was created. No real provider call, credential validation, speech accuracy
measurement or Marketing API permission test was performed.

Evidence under the infrastructure workspace:

- `scans/raw/20260912-contact-center-ad-origin-qa-native-06/summary.json` and
  `tests.log`: native results and exact source hashes.
- `scans/raw/20260912-contact-center-ad-origin-standalone/summary.json`: Python 3.10
  transport/adapter results; source hashes match the final services.
- `scans/raw/20260912-contact-center-ad-origin-node-dom/results.json`: UI results,
  runtime versions, source hashes and harness limitations.

The code is prepared for installation through the normal release process. Audio
transcription modes and optional Marketing enrichment remain disabled by default. See
[audio configuration](../../../operations/audio-transcription.md) and
[ad preview operation](../../../operations/ad-origin-preview.md) for activation and
limits.
