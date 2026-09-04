# Verificação da disposição do cross-check — 2026-08-25

- Fonte: `reviews/2026-08-25-independent-audit-cross-check-disposition.md`
- Método: verificação direta no código atual, símbolo a símbolo, pelo coordenador.

## Veredito

**Todas as correções alegadas existem e estão corretas.** Nenhuma divergência entre a
disposição e o código foi encontrada. Em três casos a implementação usa exatamente o
mecanismo recomendado no cross-check; em um (XC-02) a solução revisada é melhor que a
recomendação original.

| Achado | Verificado no código                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| XC-01  | `group.py:319-343` — re-search vazio após `IntegrityError` → `TransientAdapterError` (retentável via `queue.py:452`); `ValidationError` só quando `existing` aponta outro participante; comentário do snapshot corrigido também em `_get_or_create` (`group.py:549-553`). Teste determinístico em `test_phase5_groups.py:2016-2019`.                                                                                                                                                                                                                                |
| XC-02  | `_sanitize_referral` (`contact_center_meta/services/webhook.py:97+`) descarta campo inválido sem rejeitar a mensagem (strip, vazio, chars de controle); normalizador degrada via `except DTOValidationError` (`normalizer.py:128`); `_capture_attribution_best_effort` (`queue.py:531-558`) captura só `(DTOValidationError, ValidationError)` — falhas de banco continuam retentáveis — e registra `metadata_json.attribution_capture_failure` com classe do erro, limpo no sucesso posterior. Semântica "telemetria opcional" é superior à recomendação original. |
| XC-03  | `_touchpoint_values` (`attribution.py:461-497`) grava os 3 FKs de projeção como `False` na captura, com comentário explicando o KEY SHARE implícito; vínculo só em `_link_projection`, agora com `self.sudo()` (fecha META-13).                                                                                                                                                                                                                                                                                                                                     |
| XC-04  | `_apply_group_delivery_event` (`application.py:1035-1055`) trava `mail_channel FOR SHARE` e revalida o binding **antes** do `_group_roster_participant(enrich=True)` — ordem canal→perfil restaurada.                                                                                                                                                                                                                                                                                                                                                               |
| XC-05  | `.get(binding.id, empty_profile)` (`application.py:4388-4391`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| XC-06  | `REALTIME_REFRESH_LIMIT = 200` (`store.esm.js:24`) limita `targetCount`; a conversa preservada é excluída da contagem via `preservedConversationChannelId` (`store.esm.js:811-819`).                                                                                                                                                                                                                                                                                                                                                                                |
| XC-07  | `_provider_read_preflight` (`media.py:981-1007`) — active/connected/latch/frescor antes de qualquer I/O, com teste `test_media_pause_preflight_never_calls_provider`. Exatamente a alternativa recomendada (preflight em vez de backoff).                                                                                                                                                                                                                                                                                                                           |
| Meta   | `source_type="ad"` canônico (`normalizer.py:123`); identificadores enriquecidos usam `observed_at` da nova evidência (`attribution.py:566/601/673`); `_link_projection` em ambiente elevado.                                                                                                                                                                                                                                                                                                                                                                        |

Corroborações numéricas: 255 métodos de teste no base, 255+126+40 = **421** integrados,
**75** testes QUnit — batem com os totais declarados (255/255, 421/421, 75/75). Versões
nos manifests: base `16.0.1.20.2`, wuzapi `16.0.1.17.0`, ui `16.0.1.17.1`, meta
`16.0.1.1.1` — conferem.

## Pendências (corretamente declaradas, não regressões)

1. Saída administrativa auditável para `uncertain` (XC-09) — aberta por decisão.
2. Métrica de volume dos ledgers antes de qualquer política de retenção (XC-08/XC-10).
3. **Commit + remote**: a árvore segue não commitada (86 caminhos) em repo sem remote —
   continua sendo a ação de menor custo e maior valor pendente.
4. META-10/META-16 no backlog da Fase 6, com justificativa razoável (fixture real).

Com isso, todos os achados de código do ciclo auditoria → disposição → cross-check →
disposição estão fechados ou formalmente aceitos como pendência de produto. O gate
restante do aceite é operacional (reconexão real, canário `uncertain`, carga), mais o
item de processo do git.
