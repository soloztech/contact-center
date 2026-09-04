# Verificação do follow-up (M7, M8, M10, M11 + baixos) — base 1.8.0

- Data: 2026-08-24 (madrugada)
- Revisor: Claude (auditor); 6 verificadores adversariais independentes
- Lab: `1.8.0/1.6.0/1.6.0` implantados, health ativo nas 5 caixas, 26 conversas, ~4 000
  eventos de inbox processados

## Vereditos

| Fix                       | Veredito              | Essência                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M7** ordem de mutações  | ⚠️ **Parcial**        | Edit/delete monotônicos, com teste real de corrida (2 cursores, `SerializationFailure` → retry → converge para o texto mais novo). **Bypass na lane de reação**: `_apply_reaction` não escreve a linha do binding lockada, então um worker concorrente com reação mais antiga não sofre erro de serialização e pode aplicar o emoji antigo (ou duplicar linha com emoji diferente). Só há teste de corrida para edit. |
| **M8** ator do eco        | ✅ Verificado         | Ator `from_me` = autor técnico da conta nos dois sentidos (UI também), migração limpa linhas legadas, lanes unificadas. **Defeito novo**: conta sem `technical_author_id` descarta ecos `from_me` (inclusive **delete**) como `unsupported` terminal, sem replay — e edit/delete nem usam o ator. Cosmético: `reacted_by_me` é true para todos os agentes na reação da conta.                                         |
| **M10** memória de mídia  | ✅ Verificado         | `attachment.raw` + um único `b64encode`; pico medido de forma independente ≈ **280 MB** para 50 MB (antes ~450). Restam 2 cópias evitáveis (`requests(json=…)` refaz dumps+encode); sha256 calculado 2× e arquivo lido 2× por tentativa (snapshot+dispatch com commit no meio); **inbound** continua ~5-6× e agora domina o pico. Streaming real segue no backlog.                                                    |
| **M11** merge da timeline | ✅ Verificado (médio) | Resync por bus faz merge incremental preservando páginas antigas e cursor (teste: 150+100→151). **Defeito novo**: corrida na troca de conversa pode mesclar a página da conversa B no array da conversa A (falta carimbo de canal no array). Lacuna >100 mensagens na reconexão continua sem cursor forward (aceito na disposição).                                                                                   |

Baixos declarados fechados: edit-após-delete ✅, correlação por `client_message_id` ✅
(com ambiguidade plausível entre canais: match em `external` de um canal + `client` de
outro → `ValidationError` permanente), bloqueio das reações nativas do Discuss ✅ (o
teste antigo agora afirma o bloqueio), cancelamento de upload ⚠️ (AbortController real,
mas o `abort()` não é exercitado por teste e não há cancelamento servidor), previews
humanos ✅. i18n da UI e teto de caption **não** foram feitos (também não constavam do
incident).

## Defeitos novos a registrar (para a Fase 4)

1. Corrida da lane de reação (M7) — tocar a linha do binding também em `_apply_reaction`
   (ou lock advisory), + teste de corrida para react.
2. Eco `from_me` descartado sem `technical_author_id` — exigir autor técnico quando a
   conexão anuncia react/edit/delete (constraint/onboarding) e dar caminho de replay a
   `unsupported`.
3. Mistura de conversas no merge da timeline — carimbo de canal no array.
4. Ambiguidade de correlação entre canais no lookup de mutação.
5. (lab) 7 ReadReceipts `pending` com 0 tentativas há horas — cron-vassoura para
   reenfileirar `pending` sem job ativo; 2 `dead` por
   `Wrong value for reaction_operation` — investigar payload real; 1 conflito de
   identidade `open` desde 22/08 aguardando decisão manual + replay.

## Estado do repositório

Git ok: branch `16.0`, 2 commits, árvore limpa, tag `16.0.1.8.0-lab`, `.gitignore`
correto, nada de artefato commitado. **Sem remote** — cópia única nesta máquina; criar
`soloztech/contact-center` e fazer push é a única pendência de processo do git.

## Conclusão

As 6 fases de construção (0, 0.1, 1, 2, 2.1, 3) + M4 estão implantadas e verificadas. O
plano marca corretamente: **próxima fase = Fase 4 — Piloto e hardening** (plan.md:906),
com o gate de aceite provisório: 1 número + 1 equipe, ≥5 dias úteis, ~100 mensagens de
texto, zero duplicata visível, nenhum `dead`/`uncertain` sem causa >24 h.
