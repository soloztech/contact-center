# Disposição da verificação de follow-up — base 1.8.1

- Data: 2026-08-24
- Fonte: `reviews/2026-08-24-followup-verification.md`
- Escopo: confirmar os achados contra o código, corrigir somente os confirmados e
  validar no SERVIDOR05.

## Disposição

| Achado                                 | Decisão             | Resultado                                                                                                                                                                                                        |
| -------------------------------------- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| M7, corrida de reactions               | Aceito              | Cada projeção incrementa uma revisão persistida no binding bloqueado. Um teste com dois cursores comprova convergência monotônica também para reactions.                                                         |
| M8, self edit/delete sem autor técnico | Aceito parcialmente | Edit e delete `from_me` não exigem autor porque não possuem ator visual. Reaction continua exigindo `technical_author_id`, necessário para uma lane canônica.                                                    |
| Payload semântico de mutação           | Aceito              | `reaction_operation`/emoji existem somente em reaction e `new_text` somente em edit. A migração `1.8.1` limpa valores legados.                                                                                   |
| M10, cópias residuais de mídia         | Backlog             | O ganho do outbound `1.8.0` foi confirmado. Streaming e redução das cópias restantes, principalmente inbound, exigem alteração de transporte e benchmark próprio.                                                |
| M11, troca concorrente de conversa     | Aceito              | A timeline passou a carregar o `channel_id` proprietário, limpa imediatamente a projeção anterior e rejeita respostas ultrapassadas. QUnit cobre reset e refresh invertidos.                                     |
| Correlação ambígua entre canais        | Aceito e ampliado   | Mutation, receipt e eco `from_me` resolvem primeiro o canal por aliases ou `conversation_ref` e só então correlacionam IDs da mensagem.                                                                          |
| Replay de terminal                     | Aceito com guarda   | Administrator pode reenfileirar `blocked`, `unsupported` ou `dead`, com confirmação na view, lock, recusa de job ativo, recusa de conflito aberto e auditoria do replay. Não foi criado replay automático amplo. |
| “7 pending sem job” e cron-vassoura    | Não reproduzido     | A leitura atual encontrou zero evento não terminal sem job ativo. Um cron adicional não foi criado sem evidência atual.                                                                                          |

## Validação

- Fonte implantada: tree hash
  `1241fbd58337f571756f05b85d17f9d134ef92171d7b31ccede271ab86a51b95`.
- Versões: base `16.0.1.8.1`, WuzAPI `16.0.1.6.0`, UI `16.0.1.6.1` e `queue_job`
  `16.0.3.0.2`.
- Testes Odoo isolados: **110/110** no core e **165/165** integrados, sem falha ou erro;
  os bancos temporários foram removidos.
- QUnit autenticado em minificado e `debug=assets`: UI **33/33**, 222/222 asserções;
  viewer base **4/4**, 15/15 asserções.
- Browser real: 26 conversas, frota 5/5 conectada e zero erro/warning de console.
- Os dois inbox `dead` causados por `reaction_operation='delete'` foram selecionados
  explicitamente, reenfileirados uma vez e concluíram em `done`; suas duas mutações
  estão `applied`, com operação de reaction vazia e mensagem em tombstone. Os 28
  receipts antigos e o conflito de identidade aberto não foram reenfileirados.
- Upgrade feito no banco neutralizado do SERVIDOR05 sem backup, conforme decisão para o
  laboratório descartável. Produção não foi acessada.

## Estado operacional observado

- Mídia inbound real já foi exercitada: 16 anexos `ready` e íntegros (imagem, áudio,
  vídeo e documento).
- Cinco conexões estão saudáveis; queda/autenticação/reconexão real e capacidade com
  aproximadamente 20 números ainda precisam de ensaio.
- O volume observado ainda não comprova o gate de cinco dias úteis do piloto.
- O filesystem do SERVIDOR05 estava em 97% de uso, com aproximadamente 2 GiB livres;
  liberar espaço é um gate operacional antes de ampliar carga.
- Limitar a concorrência pesada de mídia e medir memória continuam necessários para o
  piloto; streaming permanece backlog.
