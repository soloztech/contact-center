# Verificação das correções M1–M6 e M9 (auditoria de 2026-08-22)

> Atualização documental de 2026-08-23: o M4 passou a estar registrado no `plan.md`, no
> README do projeto, na disposição da auditoria e no incident/runbook
> `2026-08-23-odoo16-contact-center-m4-health-recovery.md`. O código continua apenas na
> árvore local; deploy, upgrade, testes remotos e evidências de homologação seguem
> pendentes.

- Data: 2026-08-23
- Revisor: Claude (auditor)
- Código verificado: base `16.0.1.7.0`, wuzapi `16.0.1.5.0`, ui `16.0.1.5.0` (árvore
  local). Lab SERVIDOR05 **inacessível** no momento da verificação (timeout na LAN) —
  estado implantado não confirmado por leitura direta.
- Evidência documental do release anterior: `2026-08-22-phase3-audit-disposition.md` e
  incident `2026-08-22-odoo16-contact-center-json-audit-hardening.md` (base `1.6.0`,
  63/100 testes, QUnit 4+20). O incident/runbook do M4 `1.7.0` foi criado com
  placeholders; evidência remota ainda não existe.

## Resultado por item

| Item                        | Veredito                                                 | Evidência                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M1** retry de mídia       | ✅ Correto                                               | `media.py:392-403`: `seconds=retry_after or None` / `seconds=None` → o `retry_pattern` `{5…3600}` do `queue_job` passa a valer; teto 12 (`:389,400`); `ProviderPausedError` mantém 60 s com `ignore_retry`. Teste `test_media_retry_pattern_and_twelve_attempt_ceiling`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| **M2** recuperação de mídia | ✅ Correto (manual)                                      | `views/media_views.xml`: search/tree/form com filtro `failed` e botão **Retry download** (supervisor); menu em `menus.xml:62`. `action_retry_download` exige grupo supervisor + `check_access_rule`, zera tentativas/erros e reenfileira via `media.sudo().with_delay(...)` — o job roda com `su` preservado e consegue ler o `remote_locator_json` (admin-only). Sem cron automático, por decisão registrada.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| **M3** órfãos               | ✅ Correto no backoff                                    | `application.py:402-406, 446-449`: `retry_after_seconds = 10` removido; `_retry_delay(error)` devolve `None` → pattern do inbox. Teste `test_orphan_receipt_and_mutation_use_inbox_retry_pattern`. Estado terminal `orphan` **não** introduzido (documentado): após 12 tentativas (~2,4 h) ainda vira `dead`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **M4** reconexão            | ✅ Correto no código; ⚠️ **documentado, não implantado** | Cron 1 min `_cron_schedule_health_checks` só **enfileira** jobs (`FOR UPDATE SKIP LOCKED`, jitter 30 s, intervalo 60 s) — HTTP fora do cron, timeout (3,10). Eventos `Connected/Disconnected/ConnectFailure/StreamReplaced/KeepAlive*/LoggedOut/QRTimeout/StreamError/ClientOutdated/TemporaryBan` → estados neutros (`adapter.py:41-53`) → `connection.updated` → `_apply_provider_state_event`. Dedupe por ciclo de transição (`webhook.py:93-114`). Guarda de frescor por `last_state_observed_at` (evento velho não sobrescreve probe novo). Requeue **só** na transição para `connected` e **só** de `pending/retry` sem fronteira de dispatch (`_resume_eligible_outbox_commands`, `FOR UPDATE SKIP LOCKED`) — nunca redespacha `uncertain`. 10 testes em `test_connection_health.py`; scripts de configuração já assinam os eventos de ciclo de vida. **Pendente**: deploy/upgrade, testes remotos e evidência do runbook `1.7.0`. |
| **M5** CSP/download         | ✅ Correto                                               | `main.py:29-56` `_media_response_policy`: `guess_mimetype` sobre o conteúdo; inline **só** para image/audio/video quando o tipo sniffado confirma o `kind`; tudo o mais `attachment`. Headers: `Content-Security-Policy: default-src 'none'; sandbox`, `nosniff`, `private, no-store`. 3 testes em `test_media_security.py`. Sniffing no **download inbound** (gravar MIME real) fica no backlog — aceitável, pois a entrega já é segura. Decodificação integral por request permanece (baixo).                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| **M6** JSONs técnicos       | ✅ Correto                                               | `queue.py:73-80`: `raw_envelope_json` e `normalized_dto_json` com `groups=…admin`; `queue_views.xml` esconde os campos para não-admin. Verificado que **não quebra o processamento**: o inbox é enfileirado via `event.sudo()` e o `queue_job` serializa `uid` **e** `su` (`fields.py:81-82`), então o job lê os campos com sudo. `metadata_json` (só hashes/tamanhos) continua visível ao supervisor. `command_json` da outbox segue sem `groups` — contém texto e referência de anexo, sem segredo.                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| **M9** admin nos ledgers    | ✅ Correto                                               | `contact_center_security.xml:255-266`: `rule_contact_center_inbox_admin_company` e `rule_contact_center_outbox_admin_company` por `company_ids`, em OR com as regras de supervisor.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

## Observações

1. **M4 precisa fechar o ciclo operacional**: rodar dry-run/test/upgrade/validate no lab
   e preencher as evidências do incident/runbook. A verificação acima é do código, não
   do ambiente.
2. Ao implantar o M4, o cron de 1 minuto passa a gerar um job de health por conexão
   ativa por minuto (5 caixas = 5 jobs/min no canal `contact_center.health`) — conferir
   a capacidade `ODOO_QUEUE_JOB_CHANNELS` (`root:1` hoje) para não atrasar inbox/outbox;
   recomendável dar capacidade própria ao canal de health ou subir o intervalo.
3. M3 sem estado terminal próprio: receipts/mutações de mensagens conhecidamente
   `unsupported` ainda consomem 12 tentativas (agora ao longo de ~2,4 h em vez de 2
   min). Um curto-circuito quando o `external_message_id` já está em inbox `unsupported`
   evita esse custo.
4. Pendências de processo inalteradas: **git** (agora ~17k linhas untracked), **incident
   da Fase 1**, **expurgo de PII** do lab, **mídia inbound real** nunca exercitada.

---

## Adendo (2026-08-23, noite) — M4 verificado como implantado e correto

O ciclo do M4 foi fechado depois da verificação acima: incident
`2026-08-23-odoo16-contact-center-m4-health-recovery.md`, evidências em
`scans/raw/20260823-odoo16-contact-center-m4-health-recovery/`, plano atualizado (seção
"Health e recuperação" + `research/wuzapi-session-lifecycle.md`).

Verificação do healthcheck (código + lab ao vivo):

- **Implantado e operando**: base `1.7.0` no lab; cron a cada 1 min ativo; as 5 conexões
  sondadas no último minuto (`last_state_source=health_job`, latência 60–80 ms,
  `healthy`).
- **Cron nunca faz HTTP**: só agenda um job OCA idempotente por conexão devida
  (`FOR UPDATE SKIP LOCKED`, jitter determinístico `(id*17)%30`, dedupe por
  `health_job_uuid`); o probe roda no worker (`GET /session/status`, timeout (3,10)),
  falha isolada e sanitizada.
- **Fail-closed com frescor único**: UI e dispatch usam o mesmo limite de 180 s;
  observação stale ⇒ estado `unknown` e `_contact_center_outbound_is_available()`
  bloqueia o boundary. Implicação operacional documentada: JobRunner parado > 3 min ⇒
  envio para (comandos ficam `pending`, sem perda).
- **Trava de identidade**: o probe compara o JID próprio configurado com o observado
  (normalização PN/`@c.us`); mismatch ⇒ `degraded` + `identity_mismatch_latched`, que só
  um health `connected` com `identity_matches=true` libera; lifecycle não limpa. Protege
  contra troca de instância/número na WuzAPI.
- **Lifecycle**: os 11 eventos assinados nas 5 instâncias (evidência
  `webhooks/20260823T215919…/result.json`: 13 eventos incluindo
  `Connected…TemporaryBan`). `Connected`/`KeepAliveRestored` viram
  `degraded/identity_unverified` e apenas agendam confirmação por health — o webhook
  nunca libera outbound sozinho. Dedupe por ciclo de transição; empates no mesmo segundo
  resolvidos pelo ID durável do inbox e pelo estado menos permissivo. Zero eventos
  lifecycle no inbox até agora = nenhuma queda de sessão desde o deploy (esperado).
- **Recuperação segura**: só na transição do predicado completo para disponível; somente
  `pending/retry` sem `dispatch_job_uuid`/`dispatch_started_at`; nunca
  `uncertain`/terminais; conexão standby não recupera; hint `Connected` exige
  confirmação do probe (testes 868/889).
- **Capacidade do runner ajustada**: `root:4,root.contact_center.health:2` validado em
  runtime (resolvia o risco apontado na verificação anterior).
- **Testes**: 92/92 base, 141/141 integrado, 28 QUnit/168 asserções; 29 testes dedicados
  em `test_connection_health.py` (frescor compartilhado, latch, revisão de configuração,
  rate-limit com `Retry-After`, monotonicidade cross-source, bus restrito ao roster).

Ressalvas menores:

1. `contact_center_ui` local está em `1.5.3` e o lab em `1.5.2` — uma iteração local
   ainda não implantada (drift pequeno; mais um argumento para o Git).
2. O ensaio de capacidade do JobRunner para a topologia do piloto está listado como
   passo pendente no próprio incident (item 8) — não aprovar a topologia sem ele.
3. Git e expurgo de PII continuam pendentes.
