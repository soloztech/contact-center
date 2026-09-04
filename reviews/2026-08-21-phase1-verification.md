# Verificação do que foi feito — Fases 0, 0.1 e 1 (texto)

- Data: 2026-08-21 (noite)
- Revisor: Claude (auditor)
- Escopo: código em `contact-center/` (3 addons, ~11k linhas), scripts de laboratório,
  evidências em `scans/raw/20260821-odoo16-contact-center-*`, incidents, e leitura
  somente-leitura (JSON-RPC) do laboratório SERVIDOR05 `odoo16` :8169.
- Referência: `2026-08-21-plan-review.md` (P0/P1/P2) e
  `2026-08-21-plan-review-disposition.md` (decisão do operador).

## Veredito

**Fase 1 (texto ponta a ponta) está funcionando de verdade**, não só em teste: o
laboratório já processou webhooks reais da WuzAPI v1.0.8 (conversa direta com inbound
guest-authored, ecos `from_me` do celular importados como `outbound/external_device`,
receipts `sent→delivered`), além do smoke sintético `passed`. Testes: 35/35 base e 63/63
integrado, sem falhas. Sintaxe Python e XML válidos localmente.

Os três P0 da revisão foram implementados. Dos P1, a maioria foi implementada; os itens
abaixo ficaram parciais ou ausentes, e há três pendências de processo (incident da Fase
1, código não versionado, PII real no lab).

## Rastreio P0/P1 → código

| Item                           | Estado                                               | Evidência                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------ | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0-1 popup do Discuss          | **Feito**                                            | `channel.py:638-646` suprime `_channel_message_notifications` para `contact_center`; `_channel_fetch_message` → `[]`; `channel_fold/pin/rename/...` rejeitados; `mail.channel.member.write` bloqueia ponteiros seen/fetched. Teste só chama o override direto (`test_contact_center.py:671`), não captura `bus.bus._sendmany`                                    |
| P0-2 latência                  | **Feito (com mudança de arquitetura)**               | Trocou `ir.cron` por OCA `queue_job` 16.0.3.0.2; `inbox.create()` → `with_delay(identity_key=…)`; runner em `workers=0`, `--load=base,web,queue_job`, `root:1`. Crons legados removidos na migração 1.1.0                                                                                                                                                        |
| P0-3 DM por identidade         | **Feito**                                            | `application.py:492-530` resolve por `(account, identity, 'direct')`; índice único parcial em `channel.py:955-963`; `merged_into_id` em binding e identity; alias de conversa em outro binding → `IdentityConflictError` (`:540-563`)                                                                                                                            |
| P1-1 `Id` pré-atribuído        | **Feito**                                            | `derive_client_message_id` + `"Id": client_message_id` no envio (`wuzapi/adapter.py:590-603`); eco reconcilia por `external_message_id` OU `client_message_id` (`application.py:109-139`); índice único parcial por conexão                                                                                                                                      |
| P1-2 CommandDTO com endereços  | **Feito**                                            | `CommandDTO.conversation: ConversationDTO` + `target_address` + `reply_to` + `client_message_id` (`dto.py:311-355`)                                                                                                                                                                                                                                              |
| P1-3 LGPD/retenção             | **Não adotado por decisão do operador** (disposição) | Só limite de 1 MiB no webhook. Ver "Pendências de processo"                                                                                                                                                                                                                                                                                                      |
| P1-4 mídia/base64              | **Parcial**                                          | Mídia em DM → `unsupported`; tamanhos observados no lab (1–15 KB) mostram que não há base64 no webhook hoje. Mas `raw_envelope_json = envelope` integral, sem stripping de thumbnails/base64 se a config da WuzAPI mudar                                                                                                                                         |
| P1-5 membership = gate         | **Parcial**                                          | Fábrica remove membership implícita do criador; membros = responsável + agentes + supervisores do time; `_contact_center_reconcile_members` existe. **Não existe API de atribuição de responsável nem transferência de equipe**, e `mail.channel.write` não reconcilia membros mesmo com token                                                                   |
| P1-6 subtype/date/nota interna | **Feito**                                            | `message_type='comment'`, `mail.mt_comment`, `date=occurred_at`; `message_post` sem token só aceita `mt_note` sem destinatários/autor; nota nunca gera outbox                                                                                                                                                                                                    |
| P1-7 pausa/throttle/backoff    | **Parcial**                                          | Pausa por conexão antes do adapter (`queue.py:558-565`, `ignore_retry`); backoff `{5s…3600s}` via `retry_pattern`. **Sem jitter, sem throttle por conexão**; cap `>= 12` hard-coded em 4 pontos; **sem re-enfileiramento automático após reconexão**; estado da conexão só muda por botão manual (sem cron, `Connected/Disconnected` do webhook → `unsupported`) |
| P1-8 concorrência              | **Feito**                                            | `test_phase1_concurrency.py` usa `registry.cursor()` em 2 threads com `Barrier`; converge para 1 identity/guest/canal/mensagem e 2 aliases. Colisão `IntegrityError` é re-tentada pelo `queue_job` (transação nova), não localmente                                                                                                                              |
| HMAC                           | **Feito**                                            | SHA-256 sobre bytes crus, `x-hmac-signature`, `compare_digest`, fail-closed, segredo ≥ 32 chars; rota por chave opaca por conexão; 413 acima de 1 MiB; 503 se conta sem equipe                                                                                                                                                                                   |
| Segredos                       | **OK**                                               | Token/HMAC só em campos `groups=admin` e header HTTP; `provider_response` persistido por allow-list; logs sem credenciais                                                                                                                                                                                                                                        |

## Estado observado no laboratório (somente leitura, 2026-08-21 ~21:50 UTC)

- Inbox: 83 eventos — 17 `done`, 62 `unsupported`, 4 `dead`.
  - 54 `unsupported` = grupos/broadcast (correto: registrados, não descartados).
  - 2 `unsupported` = mídia em DM (imagem, áudio) — esperado na Fase 1.
  - 6 `unsupported` = `ReadReceipt` com estado `ReadSelf`.
  - 1 `unsupported` = `from_me` para conversa inexistente (política do plano).
  - 4 `dead` = receipts dessa mesma mensagem `from_me` sem conversa: 12 tentativas em
    ~15 min e morte. **Nunca correlacionariam**; deviam ser classificados como órfãos
    quando a mensagem referenciada é conhecida como `unsupported`.
- Outbox: 1 comando (smoke) `done`, `client_message_id` = `external_message_id`.
- Canais: 2 (contato de laboratório, smoke); membros = partner do agente + guest. Nenhum
  responsável atribuído (sem API).
- Delivery events: append-only; receipts reprocessados às 21:37 geraram `delivered`
  duplicado para 3 bindings (histórico, aceitável; considerar dedupe por
  `external_event_id`).
- Conexão `connected`, `last_health_at` 21:11 (só atualiza por botão).

## Gaps de código (ordem de prioridade)

1. **Atribuição/transferência inexistente** — bloqueia a Fase 2 (UI de atendimento):
   implementar `action_assign(responsible)` / `action_transfer(team)` no core chamando
   `_contact_center_reconcile_members`, e decidir visibilidade histórica de quem sai.
2. **Conexão não se recupera sozinha**: sem cron de health, sem tratar
   `Connected/Disconnected/LoggedOut` do webhook, sem requeue ao voltar a `connected`.
   Hoje uma queda exige clique manual em "check health" + `action_requeue`.
3. **Receipts órfãos viram `dead` após 12 tentativas** — classificar como `unsupported`
   quando o `external_message_id` referenciado está em inbox `unsupported`, ou aplicar
   TTL curto.
4. **Sem throttle/jitter por conexão** (anti-ban) — a disposição dizia "adotado".
5. **Raw payload integral** — adicionar stripping de `JPEGThumbnail`/base64 antes de
   persistir, independente da config da WuzAPI.
6. **Inbox sem `FOR UPDATE`** (outbox tem) — depende de `identity_key` + uuid guard.
7. Cap de tentativas hard-coded (`>= 12`) — promover a parâmetro da conexão.
8. Testes faltantes: captura real do bus provando ausência de
   `mail.channel/new_message`; duplicata inbound preservando 1 mensagem e atualizando
   aliases; guest preservado após promoção.
9. Sem override defensivo de `channel_info`/`_get_channels_as_member` (ocultação no
   Discuss depende de `is_pinned` nativo).

## Pendências de processo

1. **Incident da Fase 1 não existe** em `odoo16/incidents/` — deploy, upgrade (sem
   backup, opt-in do lab), configuração da WuzAPI e smoke foram aplicados às 21:0x–21:2x
   UTC com evidência em
   `scans/raw/20260821-odoo16-contact-center-phase1-{text,config,smoke}`. O `AGENTS.md`
   exige o registro.
2. **Nada versionado**: `odoo16/addons/` inteiro está untracked no `infra-ai-ops` e o
   repo `soloztech/contact-center` (declarado no `.copier-answers.yml`/README) não
   existe como git. Risco real de perda de ~11k linhas + scripts de lab. Decidir: repo
   próprio (recomendado, branch `16.0`) ou subdiretório do `infra-ai-ops`.
3. **PII real no laboratório**: a conversa de teste contém mensagens pessoais reais
   (guest de laboratório; PN/LID sanitizados neste relatório). LGPD ficou fora de escopo
   por decisão, mas o banco do lab e os dumps em
   `/home/administrador/odoo16/backups/contact-center/` agora carregam esse conteúdo —
   não copiar/compartilhar sem expurgo.
4. Disposição diz "usar `-skipmedia` no piloto" — não há evidência de configuração nem
   verificação no `phase1_configure.py` (só assina `Message`,`ReadReceipt`). O
   comportamento observado é compatível, mas não está garantido por script.

## Divergências em relação ao plano (registrar no `plan.md`)

- `ir.cron` → OCA `queue_job` (dependência nova, `--load` e runner no Compose; impacta o
  deploy em produção no SERVIDOR02, que hoje não tem `queue_job`).
- `contact.center.wuzapi.config` foi absorvido em `contact.center.provider.connection`
  (migração 1.1.0/1.2.0).
- Baseline WuzAPI: `v1.0.8` / `9487eca` (não `919c72c9` da revisão) — fixtures pinadas.
