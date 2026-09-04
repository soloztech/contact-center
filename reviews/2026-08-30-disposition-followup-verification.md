# Verificação do follow-up R1–R7 — base 1.24.5 · wuzapi 1.19.2 · meta 1.6.2

- Data: 2026-08-30
- Revisor: Claude (auditor); 1 leitor independente para R2/R3/R5/R6 e refutações
- Entrada: `reviews/2026-08-29-service-split-disposition-verification-response.md`;
  commit `3c70ba9`
- Evidência conferida (cadeia consistente): `final-source-deploy-2` tree `23bb787a…`
  (00:35 UTC) → `tests-final-2` **312/312 + 588/588** (00:38) → `validate-final`
  (00:38). O `release-apply` de 00:25 era a árvore anterior (`1bbe6acc…`), não a final.
  Lint limpo; 588 testes contados (312 base + 136 wuzapi + 140 meta); nenhuma asserção
  enfraquecida no diff de testes.
- Lab (30/08 01:01 UTC, snapshot salvo em scratchpad/lab_snapshot_1245.json): versões
  1.24.5/1.19.2/1.6.2; inbox 15 280 done / 15 168 unsupported / 925 dead / 3 pending;
  **0 waiters presos, 0 `blocked`, 0 `dead` novos desde 22:00 UTC**; conflito
  `resolved`; 7 conexões saudáveis (Meta `degraded` por pendências externas).

## Veredito

**Rodada bem executada: R1, R2, R4, R5, R6, R7 verificados; R3 parcial.** As seis
refutações são, em geral, procedentes — a #4 (autor que apaga e sai continua autorizado
pela correlação ator↔alvo) foi confirmada linha a linha, e a #2 (o reset de `attempts` é
do _profile_, não do inbox) também. A #3 (sem TTL) fica **parcialmente** sustentada: a
alternativa proposta — "converge para `dead` no teto de 12" — **não ocorre** quando o
profile do grupo entra em `metadata_state='failed'` (ver D1). A #6 procede: desta vez o
snapshot do lab foi arquivado.

| Item                                   | Veredito                   | Observação                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **R1** 429 no health Meta              | ✅                         | `except ProviderRateLimitError` antes de `ProviderPausedError`, `provider_paused_retry_seconds` + `ignore_retry=True`; teste roda `job.perform()` real e afirma `job.retry` preservado e `validation_state == "valid"`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| **R2** escopo inválido → `unsupported` | ✅                         | `_finalize_invalid_group_roster_waiters` com `FOR UPDATE OF ledger SKIP LOCKED`, lote ≤ 500, predicado estrutural (profile/binding/canal/conexão/conta ausentes ou inativos, rotação, tipo ≠ group, `merged_into_id`), saúde do provider **fora** do predicado; `_finish_unavailable` também finaliza. Testes: sentinela do `extra_where`, 8 escopos (7 finalizados, `disconnected` permanece). Ressalvas: decisão só por SQL sem segunda guarda ORM (janela de desarquivamento concorrente); `paused` não exercitado (irrelevante ao predicado).                                                                                                                                             |
| **R3** contadores + teto               | ⚠️ Parcial                 | `group_roster_wait_count` e `first_group_roster_wait_at` duráveis e visíveis (admin); release e cron **não** zeram `attempts`; `_request_sync(force=True)` zera só `profile.attempts` (refutação #2 confirmada); coalescing sem escrita. **Mas** o teto conta `attempts` (qualquer origem transitória), não ciclos de defer — `group_roster_wait_count` é só observável; e **D1**: profile `failed` (erro permanente de DTO/adapter) nunca reenfileira o waiter (`extra_where` exige `ready`) nem o finaliza (invalidez só reconhece `UnsupportedEventError`) → `pending` para sempre com `attempts` congelado. Teste do teto usa atalho (`attempts=11`), sem ciclo real defer→release→defer. |
| **R4** `except UserError`              | ✅ (melhor que o sugerido) | Inbound chama `required=False` e trata só a **ausência** do participante próprio como transitória; `ValidationError` do resolver propaga como permanente.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| **R5** savepoint na liberação          | ✅                         | `try/with savepoint/except Exception` + log; teste com `ValidationError` mockado mantém `ready`. Menor (D2): o flush implícito do `cr.savepoint()` fica dentro do `try` — se falhar, a transação já está abortada e o método devolve `True`.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| **R6** 429 na config de webhook WuzAPI | ✅                         | Produtor `ProviderRateLimitError` + `Retry-After`; consumidor testa `isinstance(ProviderRateLimitError)` **antes** do classificador genérico (o risco de virar não-retryável está fechado); teste com `job.perform()` real, `retry == 4` preservado, contra-teste 503 consome o teto.                                                                                                                                                                                                                                                                                                                                                                                                         |
| **R7** evidência por slot              | ✅                         | `_sanitizer_rejected_attachments` restringe a `attachments`/`attachment:*`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

## Defeitos novos

| #      | Sev.  | Defeito                                                                                                                                | Correção                                                                                                                                                                                   |
| ------ | ----- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **D1** | Médio | Waiter cujo profile está `metadata_state='failed'` não reenfileira, não finaliza e não morre — fura a promessa "converge para `dead`". | Incluir `profile.metadata_state='failed'` (estado terminal) no `invalid_scope_clause` **ou** finalizar por `first_group_roster_wait_at` quando o profile estiver terminal; teste dedicado. |
| D2     | Baixo | Flush implícito do `savepoint()` dentro do `try` (R5).                                                                                 | `flush` explícito antes do `try`.                                                                                                                                                          |
| D3     | Baixo | Finalização por escopo inválido sem revalidação ORM sob lock (só o ledger travado).                                                    | Segunda guarda ORM simétrica à de `_group_roster_wait_is_satisfied`.                                                                                                                       |
| D4     | Info  | `last_error_class='UnsupportedEventError'` pegajoso por até 24 h mata waiters novos criados após a sessão voltar.                      | Limpar o campo ao recuperar a conexão ou ao agendar sync.                                                                                                                                  |
| D5     | Info  | Cada defer pode zerar o orçamento do profile (até 12×).                                                                                | Telemetria.                                                                                                                                                                                |

## Lacunas de teste

Ciclo real defer→release→defer acumulando `attempts`/`group_roster_wait_count`;
`attempts` sobrevivendo a `_release_roster_waiters`; falha de banco (não mock) na
liberação; estado `paused`; **D1** (profile `failed` com waiter).

## Processo

Tag `16.0.1.24.5-lab` ✅, plano e resposta técnica registrados ✅, evidência encadeada
✅; `git remote` continua ausente.
