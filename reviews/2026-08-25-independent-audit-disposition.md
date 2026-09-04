# Disposição crítica da auditoria independente — 2026-08-25

Fonte analisada: `reviews/2026-08-25-independent-audit.md`.

## Critério

A auditoria foi confrontada com o código atual, não aplicada mecanicamente. O relatório
usou versões anteriores (`base 1.18.0`, `wuzapi 1.15.0`, `ui 1.15.0`), enquanto esta
rodada começou em `1.19.2`, `1.16.1` e `1.16.0`. Cada correção preserva quatro
invariantes: autorização no servidor, idempotência, ausência de reenvio ambíguo e replay
do ledger.

## P1

| Achado | Veredito no código atual                                     | Disposição                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------ | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AUD-01 | Confirmado no envio e nas projeções inbound                  | Implantada ordem canônica `mail.channel → channel.binding`, com revalidação após os locks, inclusive em mensagem/echo novo e nos hints de avatar/metadados de grupo. Avatar evita locks quando o sync é no-op. A parte sobre mutação foi refutada: esse fluxo não pede o lock posterior de `mail.channel`, portanto não forma o ABBA descrito. Há teste concorrente envio × inbound com transações reais e testes de ordem nos dois hints.                                 |
| AUD-02 | Parcialmente confirmado                                      | `ConnectTimeout` e a causa tipada `urllib3.NewConnectionError` agora são `transient`; `ReadTimeout`, reset genérico e 5xx continuam `uncertain`. A ação “não encontrou, então reenviar” foi rejeitada: o build pinado não oferece consulta negativa autoritativa por ID, logo isso poderia duplicar mensagem. A UI já distinguia `uncertain`; essa parte do relatório estava desatualizada.                                                                                |
| AUD-03 | Confirmado                                                   | Refresh por bus recarrega a quantidade de páginas já aberta, com cerca de request/cursor, sem truncar a lista.                                                                                                                                                                                                                                                                                                                                                             |
| AUD-04 | Confirmado com solução revisada                              | Timeout de mídia caiu de 660 s para 180 s e o JobRunner foi homologado em runtime com `root:4,root.contact_center.health:2,root.contact_center.media:1`. Capacidade pertence à configuração runtime, não ao XML do canal. Não foi criada uma fronteira de commit para GET de mídia: ela não tem a semântica anti-duplicação do outbound e introduzir commit intermediário/stuck state exigiria outro protocolo de recuperação. O slot único limita o impacto remanescente. |
| AUD-05 | Crescimento real; prioridade e correção propostas rejeitadas | Não há expurgo automático. `blocked`, `dead`, `unsupported` e `uncertain` precisam do bruto para replay; o DTO normalizado sustenta backfills; entregas de grupo e touchpoints sustentam a projeção; o ledger Meta é imutável por contrato. Futuro: compactação auditável, opt-in, por classe de dado e nunca uma exclusão genérica.                                                                                                                                       |
| AUD-06 | Confirmado                                                   | Conteúdo usa `ir.binary`/`Stream` nativo, filestream, Range, ETag/304, cache privado de uma hora, CSP, `nosniff` e autorização antes do stream. Upload e download persistem `raw`, sem cópia base64 do ORM.                                                                                                                                                                                                                                                                |
| AUD-07 | Confirmado                                                   | Adicionado `HttpCase` para Range/304/416/download, isolamento, avatares, upload multipart idempotente e bloqueio das rotas nativas do Discuss.                                                                                                                                                                                                                                                                                                                             |

## P2

| Achado | Veredito                                   | Disposição                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------ | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AUD-08 | Confirmado no código auditado              | Webhook autenticado sem equipe agora é persistido como `blocked`, responde 202, deduplica e pode ser reenfileirado após configurar a equipe.                                                                                                                                                                                                                                                                                                                                                          |
| AUD-09 | Superestimado                              | A imagem é upstream oficial, fixada no commit `9487eca` e por digest; versão/commit e smokes reais já estão documentados. Permanece útil automatizar uma fixture real reproduzível de react/edit/delete.                                                                                                                                                                                                                                                                                              |
| AUD-10 | UX confirmada, regra correta               | Offboarding continua fail-closed, sem remoção automática de grants. A mensagem agora informa usuário, equipe, papel, causa e ação necessária.                                                                                                                                                                                                                                                                                                                                                         |
| AUD-11 | Confirmado                                 | Lista e timeline fazem prefetch em lote; preview da lista é compacto; permissões invariantes de grupo são resolvidas uma vez por binding. Os dois últimos C901 foram quebrados em helpers.                                                                                                                                                                                                                                                                                                            |
| AUD-12 | Parcialmente confirmado                    | `mail.message` agora pré-filtra `mail.channel` e calcula bindings/externos uma vez por lote. O curto-circuito sugerido para `ir.attachment.res_model` foi rejeitado: anexos M2M podem proteger uma mensagem mesmo com outro `res_model`.                                                                                                                                                                                                                                                              |
| AUD-13 | Confirmado                                 | `last_seen_at` é tocado no máximo a cada cinco minutos, mas mudança material e promoção protocolar de escopo/confiança continuam imediatas; `observed_name_at` mantém sua cerca monotônica. Recibos bloqueiam bindings por `id`. Jobs pausados usam jitter positivo, determinístico e limitado, separado da política de TTL do AUD-15. Backoff crescente foi rejeitado nesse estado: health/lifecycle já acorda a outbox e mídia/metadados precisam continuar sondando sem consumir o teto de falhas. |
| AUD-14 | Corrida existe; correção sugerida refutada | `savepoint + re-search` e advisory lock tardio não renovam snapshot `REPEATABLE READ`. Consumir uma tentativa transacional continua sendo a recuperação correta e segura.                                                                                                                                                                                                                                                                                                                             |
| AUD-15 | Confirmado, requer política de produto     | Não foi escolhido TTL arbitrário. A implementação futura precisa gravar `expires_at` imutável no comando, ser configurável por conta/tipo e expirar somente antes da fronteira de dispatch, com falha visível ao agente.                                                                                                                                                                                                                                                                              |
| AUD-16 | Confirmado                                 | Indicador discreto de bus, polling apenas offline e bootstrap concorrente com primeira renderização/skeleton. RPC nunca finge que o bus voltou.                                                                                                                                                                                                                                                                                                                                                       |
| AUD-17 | Confirmado                                 | Overlap usa igualdade real de `message_id`, nunca diferença numérica global. Sem overlap seguro, a timeline reancora na página recente em vez de unir duas ilhas; respostas obsoletas não podem reancorar outra seleção.                                                                                                                                                                                                                                                                              |
| AUD-18 | Confirmado como lacuna de cobertura        | Cobertura ampliada para HTTP, webhook vazio/não objeto/depth/content-type/404, mídia feliz/hash/tamanho, limites de resposta e limpeza seletiva de uploads. A fronteira da outbox agora tem teste em duas transações reais: uma simula morte depois de o provider aceitar e antes da persistência local; a segunda prova transição para `uncertain` sem redispatch. Smokes externos de mutações e ensaio prolongado de JobRunner continuam operacionais, não unitários.                               |
| AUD-19 | Confirmado                                 | QUnit segue no bundle e é executado no laboratório, mas falta um tour autenticado/OWL montado que rode por `test-tags` em CI. Mantido no backlog.                                                                                                                                                                                                                                                                                                                                                     |

## P3 e observações menores

- Corrigidos: ORM-06, ORM-08, ORM-09, ORM-12, ORM-13; WUZ-05 e WUZ-10;
  UI-05/06/07/08/09/10/12; SEC-04; TST-08; PERF-13 e PERF-15.
- WUZ-11 foi mitigado no limite controlável pelo addon: no stack Odoo 16/Werkzeug
  validado no SERVIDOR05, `application/json` chega ao controller como `LimitedStream`
  sem `_cached_data`, e requisições sem `Content-Length` são lidas incrementalmente até
  1 MiB + 1 byte. Se um middleware futuro materializar o corpo antes do controller,
  nenhuma rotina do addon poderá desfazer essa alocação; nessa topologia o mesmo limite
  deve existir no proxy/middleware anterior. Portanto a afirmação original de correção
  absoluta foi rejeitada.
- Refutados: ORM-07 (`non-stored` recalcula em novo RPC; `depends` não atravessa
  transações), ORM-10 (`check_company` é redundante, não defeito), WUZ-14 (`adapter_key`
  é imutável e mudar provider exige nova conexão).
- Mantidos para medição/decisão: rate limit no proxy, política de segredos/HTTPS,
  métricas para `unsupported`, pool HTTP por worker, amplificação base64 inevitável no
  wire WuzAPI, batching das migrações antigas, índices de busca e memoização da
  timeline.
- Não será adicionado log por request inválido no webhook público sem agregação/rate
  limit; isso transformaria 401/404 em vetor de log flood. Proxy e métricas são a camada
  adequada.

## Achados adicionais da revisão crítica

- A primeira disposição ainda deixava uma janela `binding → canal` no inbound novo e nos
  hints de avatar/grupo. Todos esses caminhos agora entram pela mesma primitiva canônica
  antes de escrever aliases ou projeções.
- Uma promoção de alias de `account/observed` para `company/protocol` atualizava apenas
  o escopo. Isso tornava o alias invisível ao resolvedor portável e podia criar outro
  guest em uma segunda caixa. Escopo e confiança agora são promovidos como um único
  invariante, com teste entre duas contas da mesma empresa.
- Ledgers `pending/retry` sem job ativo podiam ficar presos após perda/corrupção da
  referência do `queue_job`. Um cron limitado, com carência e `SKIP LOCKED`, recupera
  somente inbox, outbox ainda anterior à fronteira de dispatch e mídia ainda `pending`;
  estados ambíguos nunca são reenviados.
- O reply de grupo ainda tinha um ramo residual que mostrava `str(AdapterError)` ao
  agente. Ele agora registra somente a classe técnica e devolve mensagem genérica,
  coberta por regressão sem criação de mensagem/outbox.

## Resultado desta rodada

Homologação técnica concluída no SERVIDOR05 com `contact_center_base 16.0.1.20.0`,
`contact_center_wuzapi 16.0.1.17.0`, `contact_center_ui 16.0.1.17.0`,
`contact_center_meta 16.0.1.1.0` e OCA `queue_job 16.0.3.0.2`.

- Suítes isoladas: **250/250** testes base e **413/413** integrados, sem falhas ou erros
  e com remoção dos bancos temporários.
- QUnit autenticado: **73/73** testes e **616/616** asserções da UI nos bundles
  minificado e `debug=assets`; JSON viewer **4/4**, **15/15**. O primeiro passe revelou
  cinco fixtures ainda neutralizando `loadConversations()` após a troca legítima para
  `refreshLoadedConversations()`; apenas as fixtures foram corrigidas e ambos os bundles
  passaram novamente.
- Browser autenticado: 223 conversas, realtime ativo, cinco de cinco conexões saudáveis,
  conversa/timeline/perfil/atribuição renderizados e zero erro ou warning no console.
- JobRunner observado executando `health(C:2)` e `media(C:1)` sob o teto global quatro.
- Hash da árvore funcional validada antes deste fechamento exclusivamente documental:
  `b5f997e81af86e304791099e9804fe29b9c7ec1c9ce42a3abf70efb932fd47f7`.
- Evidências: deploy `deploy/20260825T153106694730Z`, testes
  `test/20260825T151422134428Z`, upgrade `upgrade/20260825T151908851207Z`, validate
  `validate/20260825T154011918820Z`, todos sob
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/`; navegador em
  `output/playwright/independent-audit-20260825-final/`.

A homologação técnica deste alicerce não substitui o aceite operacional do piloto:
queda/reconexão real, canário `uncertain`, carga e políticas de retenção/TTL continuam
decisões ou ensaios próprios.
