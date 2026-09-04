# Auditoria independente e imparcial — contact-center (base 1.18.0 / wuzapi 1.15.0 / ui 1.15.0)

- Data: 2026-08-25
- Auditor: Claude (coordenação e verificação final)
- Método: 6 auditores independentes por dimensão (segurança, ORM/correção Odoo, adapter
  WuzAPI, frontend OWL, testes, performance/concorrência), **sem acesso a `reviews/`,
  `research/`, `output/` e `plan.md`** para garantir imparcialidade; em seguida, 9
  verificadores adversariais (um por achado P0/P1/P2 estrutural, instruídos a
  **refutar** o achado contra o código real); os 2 achados cuja verificação adversarial
  falhou por limite de sessão (AUD-01 e AUD-02) foram verificados manualmente pelo
  coordenador, linha a linha.
- Escopo: leitura integral de `contact_center_base` (~11k linhas),
  `contact_center_wuzapi` (~4,4k), `contact_center_ui` (JS/XML/SCSS), security, data,
  views, migrations e toda a suíte de testes (~21k linhas).
- Consumidor previsto: **Codex**, para implementar as correções. Cada achado traz
  evidência com arquivo:linha, cenário de falha e correção recomendada acionável.

> **Status:** este arquivo preserva o relatório original e sua classificação sobre o
> snapshot auditado. Ele não é a decisão arquitetural vigente nem uma lista mecânica de
> tarefas. Cada achado e cada solução proposta foram confrontados com o código posterior
> em `reviews/2026-08-25-independent-audit-disposition.md`; nessa revisão, partes foram
> confirmadas, outras receberam solução diferente e outras foram refutadas.

## Sumário executivo

**Veredito: qualidade técnica muito acima da média para módulos Odoo — nenhum P0
confirmado.** Não foi encontrada nenhuma vulnerabilidade de segurança explorável, nenhum
caminho de corrupção de dados, nenhuma duplicação ou perda silenciosa de mensagem já
persistida. As garantias centrais do produto (idempotência ponta-a-ponta, fronteira
durável do outbox anti-duplo-envio, escape de XSS na entrada e na saída, fail-closed de
identidade/saúde) foram confirmadas corretas por auditores distintos de forma
independente.

Os riscos reais concentram-se em **7 achados P1** — todos verificados — que condicionam
a operação do piloto sob carga:

| ID     | Achado                                                                                 | Área          | Verificação                                                      |
| ------ | -------------------------------------------------------------------------------------- | ------------- | ---------------------------------------------------------------- |
| AUD-01 | Deadlock ABBA canal↔binding entre envio do agente e inbound                            | Concorrência  | Dupla descoberta independente + confirmação manual linha a linha |
| AUD-02 | Falha de transporte no envio vira `uncertain` terminal sem recuperação                 | Outbox/WuzAPI | Confirmação manual de todos os 6 elos da cadeia                  |
| AUD-03 | Sync por bus reseta a paginação da lista de conversas                                  | Frontend      | Adversarial: CONFIRMADO (alta)                                   |
| AUD-04 | Transação aberta durante download de mídia (660 s+) + canais de fila sem `capacity`    | Fila          | Adversarial: CONFIRMADO (alta)                                   |
| AUD-05 | Ledgers append-only sem política de retenção                                           | Dados         | Adversarial: CONFIRMADO (alta)                                   |
| AUD-06 | Serving de mídia materializa binário inteiro em RAM, sem cache/ETag                    | HTTP          | Verificado pelo coordenador em `controllers/main.py`             |
| AUD-07 | Camada HTTP do módulo base sem nenhum teste (upload, Range, avatar, bloqueios Discuss) | Testes        | Adversarial: CONFIRMADO (alta)                                   |

Nenhum P1 é um defeito de segurança nem causa perda de dado persistido; são defeitos de
robustez operacional e escalabilidade. A recomendação é tratá-los como **bloqueadores do
aceite da Fase 4** (piloto), na ordem acima.

---

## Achados P1 — bloqueadores do piloto

### AUD-01 · P1 · Deadlock ABBA canal↔binding entre envio do agente e processamento inbound

Encontrado de forma independente por dois auditores (ORM e Performance) com a mesma
evidência; confirmado manualmente pelo coordenador.

**Evidência (ordens de lock verificadas):**

- Caminho de envio do agente (`_send_message`): trava o **binding** primeiro —
  `contact_center_base/models/application.py:2401-2404`
  (`SELECT ... contact_center_channel_binding ... FOR UPDATE`) — e o **canal** depois,
  em `_publish_message_created` (`application.py:2474` → `application.py:86`,
  `SELECT id FROM mail_channel ... FOR UPDATE`). Ordem: **B → C**. O caminho de mutações
  (`_send_message_mutation`) repete o padrão (`application.py:2732`).
- Caminho inbound direto (`_post_inbound`): `_resolve_channel`
  (`application.py:1509-1543`) localiza o binding com `search` puro, **sem lock**; a
  primeira aquisição da transação é dentro de
  `binding._request_identity_avatar_sync(connection)` (`application.py:1706`), que trava
  o **canal** (`identity_avatar.py:209-212`) e **depois** o binding
  (`identity_avatar.py:215-218`) — **antes** do teste de TTL/estado
  (`identity_avatar.py:237-258`), ou seja, mesmo um sync no-op adquire os dois locks.
  Ordem: **C → B**.
- Variante de grupo: `_lock_for_projection` também trava canal antes do binding
  (`group.py:954-966`, canal `FOR UPDATE` → binding `FOR SHARE`; `FOR SHARE` conflita
  com o `FOR UPDATE` do caminho de envio). `group.py:606-616` declara a ordem global
  "mail.channel → group profile", que o caminho de envio não segue.

**Cenário:** cliente e agente interagem simultaneamente na mesma conversa — situação
rotineira. T-envio segura B e pede C; T-inbound segura C e pede B → PostgreSQL aborta
uma das transações após `deadlock_timeout` (~1 s). Sem perda de dados: o RPC re-tenta
(idempotente por `client_request_id`) e o job re-tenta via retry do queue_job. O custo é
pico de latência ≥1 s, consumo de tentativas de job e, em rajadas na mesma conversa,
tempestade de deadlocks.

**Por que P1 e não P0:** recuperação automática nos dois lados, sem perda/corrupção.

**Correção recomendada (pequena e localizada):** unificar a ordem global **canal →
binding** em todos os caminhos: em `_send_message` e `_send_message_mutation`, adquirir
o lock do `mail_channel` imediatamente antes do lock do binding (hoje só o adquire no
publish final). Complementarmente (defesa em profundidade), mover os dois `FOR UPDATE`
de `_request_identity_avatar_sync` para depois dos testes de TTL/estado, de modo que
mensagens rotineiras (sync no-op) não adquiram lock nenhum. Documentar a ordem global
única num comentário canônico.

**Como validar:** estender `test_phase1_concurrency.py` (harness real de threads +
barrier já existente) com um teste envio-do-agente × inbound na mesma conversa; sem a
correção ele deve reproduzir o deadlock, com a correção deve serializar.

### AUD-02 · P1 · Falha de transporte no envio vira `uncertain` terminal, sem caminho de recuperação

**Evidência (cadeia completa verificada manualmente):**

1. Qualquer `requests.RequestException` no POST de envio — inclusive
   `ConnectionError`/`ConnectTimeout`, casos em que o request comprovadamente **nunca
   chegou** ao gateway — vira
   `AdapterResult(status="uncertain", error_code="transport_no_response")`
   (`contact_center_wuzapi/services/adapter.py:2203-2210`). HTTP 5xx (inclusive 502/503
   de proxy) idem (`adapter.py:1387-1394`).
2. `uncertain` finaliza via `_finish_failure` e a mensagem permanece com
   `delivery_state="queued"` (só `dead` marca "failed").
3. `_enqueue` só reprocessa `pending`/`retry`
   (`contact_center_base/models/queue.py:636`).
4. `action_requeue` do administrador só chama `_enqueue` (`queue.py:674-679`) — não
   alcança `uncertain`.
5. `_job_process` retorna cedo para `uncertain` (`queue.py:717-718`).
6. A recuperação pós-health (`_resume_eligible_outbox_commands`,
   `contact_center_base/models/account.py:1763-1777`) também filtra
   `state IN ('pending','retry')`.

A única transição `uncertain → done` é o eco `from_me`/eco de edição
(`application.py:682-731`, `1146-1163`) — que exige que a mensagem tenha de fato chegado
ao WhatsApp. Numa conexão recusada, o eco jamais virá.

**Cenário:** o container WuzAPI reinicia (ou blip de DNS) durante um envio. O texto do
agente fica `uncertain` para sempre, exibido como "queued" na UI, invisível como falha,
e **nem o administrador consegue reprocessá-lo**.

**Correção recomendada:** (a) no adapter, classificar como `transient` as exceções
comprovadamente pré-envio — `requests.exceptions.ConnectTimeout` e `ConnectionError`
cuja causa é `NewConnectionError`/conexão recusada — mantendo `uncertain` apenas para
timeout de leitura/reset pós-envio (quando o request pode ter chegado); (b) criar uma
ação administrativa "verificar e reenfileirar" para comandos `uncertain`: consultar o
provider pelo `Id` cliente (`client_message_id`) e, se não existir lá, retornar o
comando a `retry`; se existir, finalizar `done` com o `external_message_id` obtido.
Preservar rigorosamente a semântica anti-duplicação existente: `uncertain` nunca deve
ser redispatched sem verificação.

**Como validar:** teste unitário no adapter para `ConnectionError`→`transient` e
`ReadTimeout`→`uncertain`; teste da nova ação admin nos dois desfechos.

### AUD-03 · P1 · Sincronização por bus descarta a paginação da lista de conversas

**Verificação adversarial: CONFIRMADO (confiança alta).**

**Evidência:** `contact_center_ui/static/src/js/contact_center_store.esm.js` —
`scheduleSynchronization` (`:1499-1525`, debounce de 120 ms) chama
`loadConversations({reset: true, silent: true})` (`:1511`); com `reset` o cursor é
zerado (`:640`) e pede-se `LIST_LIMIT=50` (`:22`, `:643`); `applyConversationPage` no
ramo reset **substitui** `state.conversations` pela página 1 (`:549-550`), preservando
apenas a conversa selecionada (`:556-562`). `SYNCHRONIZING_EVENTS` (`:30-39`) inclui
`delivery_updated`, e `onNotification` (`:1489-1495`) agenda o sync para evento de
**qualquer** canal do qual o agente é membro — na prática, todo o inbox visível. O ramo
de merge que preserva páginas existe (`:563-573`) mas só é usado pelo "Carregar mais"
(`:669-677`). Não há tratamento de scroll em `conversation_list.esm.js`, então o
encolhimento do array salta o scroll.

**Cenário:** agente com >50 conversas carrega mais duas páginas e rola até o fim; chega
um tick de entrega em qualquer conversa → 120 ms depois a lista encolhe para 50 e o
scroll salta. Com tráfego real, o "Carregar mais" é efetivamente inutilizável.

**Correção recomendada:** no sync silencioso, mesclar sem truncar — recarregar o mesmo
número de páginas já abertas (guardar `pagesLoaded` e repetir o cursor), ou aplicar a
página 1 pelo ramo de merge (`:563-573`) preservando o restante do array e o cursor.
Contraste disponível no próprio código: a timeline já preserva cursor no refresh
(`:758-763`).

**Como validar:** teste QUnit no store — carregar 2 páginas, disparar evento de sync,
afirmar que `conversations.length` não regride e o cursor é mantido.

### AUD-04 · P1 · Fila: transação aberta durante I/O de mídia (660 s+) e canais sem `capacity`

**Verificação adversarial: CONFIRMADO (confiança alta).**

**Evidência:** (a) o job de download inbound escreve `state="downloading"` e chama o
adapter **na mesma transação** (`contact_center_base/models/media.py:990-993`); não há
nenhum `cr.commit()` em media.py; `_MEDIA_DOWNLOAD_TIMEOUT = (5, 660)`
(`contact_center_wuzapi/services/adapter.py:145`) com `stream=True`, e como o read
timeout vale **por leitura de socket**, uma resposta gotejante pode reter a transação
por mais de 660 s. Contraste: o outbox comita antes do I/O (`queue.py:744-758`, `:875`)
e é o único com `allow_commit=True` (`contact_center_base/data/queue_job.xml:61`). (b)
Todos os canais de fila são criados **sem** `capacity`
(`contact_center_base/data/queue_job.xml:3-36` e também
`contact_center_wuzapi/data/queue_job.xml:3-12` — canal `wuzapi_webhook`);
`readme/USAGE.rst:46` sugere `channels = root:2`.

**Cenário:** com a configuração documentada, **2 downloads de mídia lentos param toda a
fila** (inbox, outbox, health) por 11+ minutos, cada um segurando conexão do pool e
transação longa (row lock + snapshot antigo → autovacuum bloqueado → bloat).

**Correção recomendada:** (1) declarar capacidades por canal em produção e no USAGE.rst
como requisito verificável — por exemplo
`root:8,root.contact_center.media:2,root.contact_center.outbox:2,root.contact_center.inbox:3,root.contact_center.health:1`;
(2) reduzir o read-timeout de download (660 s é desproporcional ao teto de 50 MB;
120–180 s cobre o caso real com folga); (3) avaliar replicar o padrão de fronteira
durável do outbox no job de mídia (commit após `state="downloading"`, antes do HTTP),
com `allow_commit` na função da fila.

**Como validar:** teste do timeout novo; verificação de deploy (script/checklist) da
configuração de canais.

### AUD-05 · P1 · Ledgers append-only sem política de retenção

**Verificação adversarial: CONFIRMADO (confiança alta), com grep exaustivo.**

**Evidência:** 5 modelos persistentes crescem sem nenhum expurgo:
`contact.center.inbox.event` persiste `raw_envelope_json` (required, `queue.py:101`)
**e** `normalized_dto_json` (gravado em todo processamento, `queue.py:339`; o write de
`done` em `queue.py:349-356` não limpa nenhum JSON), mais `metadata_json`;
`contact.center.outbox.command` persiste `command_json` + `provider_request_json` +
`provider_response_json` (`queue.py:404/425/428`, gravações em `:883/:1431/:1505`) e
`last_error_message` (até 4000 chars); `contact.center.delivery.event`
(`message.py:559`), `contact.center.message.mutation` (`mutation.py:8`) e
`contact.center.group.delivery.event` (`group_delivery.py:29`). Os ledgers de entrega
têm dedupe por mensagem (`group_delivery.py:98-104` ≤2 linhas por participante;
`message.py:588-599`), mas o crescimento agregado segue ilimitado — em grupos é
multiplicativo (mensagens × participantes). Existem só 4 `ir.cron` no repositório e o
único que deleta algo é o de uploads (`media.py:1298-1309`). Nenhum `@api.autovacuum`,
nenhum `_gc_*`.

**Correção recomendada:** cron de retenção com janelas configuráveis
(`ir.config_parameter`): (a) inbox `done` com mais de N dias → limpar
`raw_envelope_json`/`normalized_dto_json` mantendo metadados de auditoria
(`content_sha256` já existe); (b) outbox `done` → compactar `provider_request_json`; (c)
delivery/group-delivery com mais de M dias → agregar ou expurgar; (d) processar em lotes
com `limit` + commit por lote. Estados não terminais (`blocked`, `uncertain`, `dead`)
nunca são expurgados.

**Como validar:** teste do cron com registros nas duas janelas; conferir que estados não
terminais e evidência mínima de auditoria sobrevivem.

### AUD-06 · P1 · Serving de mídia: binário inteiro em RAM por request, sem cache

**Verificado diretamente pelo coordenador em `controllers/main.py`.**

**Evidência:** `content = media.attachment_id.sudo().raw or b""`
(`contact_center_base/controllers/main.py:399`) carrega o arquivo completo mesmo para
requests `Range` (slice em memória, `:433`); resposta com
`Cache-Control: private, no-store` (`:441`) impede reuso pelo navegador; não há ETag/304
no endpoint de conteúdo, embora `sha256` exista no modelo (`media.py:836`) e o padrão
ETag já esteja implementado para avatares (`main.py:133-170`). A limitação é reconhecida
em comentário (`main.py:396-398`).

**Cenário/limiar:** um vídeo de 50 MB com N seeks do player → N leituras de 50 MB no
worker HTTP síncrono; workers ocupados servindo mídia elevam a latência do webhook
(mesmo pool de workers). Degrada quando usuários simultâneos × tamanho de arquivo se
aproxima de RAM/workers.

**Correção recomendada (incremental):** (1) ETag pelo `sha256` + `If-None-Match`/304 +
`Cache-Control: private, max-age=3600` (o conteúdo é imutável por construção — hash
verificado no download); (2) para requests com `Range`, ler apenas a faixa pedida do
filestore (`attachment.store_fname` → open/seek/read) em vez de materializar tudo; (3)
opcional futuro: X-Sendfile/X-Accel atrás do proxy.

**Como validar:** os testes HTTP novos de AUD-07 cobrem 304, 206 com leitura parcial
e 416.

### AUD-07 · P1 · Camada HTTP do módulo base sem nenhum teste

**Verificação adversarial: CONFIRMADO (confiança alta), com grep exaustivo.**

**Evidência:** o único `HttpCase` do repositório é
`contact_center_wuzapi/tests/test_webhook.py:12`. Zero ocorrências, em toda a suíte, de
chamadas a `/contact_center/media/upload`, `media_content`, avatares,
`mail_chat_post`/`mail_chat_history` ou `_lock_media_upload_reference`
(`client_upload_id` aparece somente no controller). `test_media_security.py` cobre
apenas os helpers puros importados (`:4-9`). Sem teste ficam: multipart+CSRF, o advisory
lock de idempotência de upload (`main.py:124-131`) e a comparação de reuso (`:316-331`),
parsing de Range/206/416 (`:410-434`), ETag/304 de avatar (`:133-170`), e os bloqueios
das rotas nativas do Discuss para canais `contact_center` (`:173-221`) — regressão de
segurança silenciosa possível.

**Correção recomendada:** criar `contact_center_base/tests/test_http_endpoints.py`
(`HttpCase`, `@tagged("post_install", "-at_install")`, `self.authenticate`): upload
feliz + replay idempotente com mesmo `client_upload_id` + UUID inválido + arquivo acima
do limite; download com `Range: bytes=0-4`, `bytes=-5`, range inválido (416),
`download=1`; avatar com `If-None-Match` (304); `POST /mail/chat_post` em canal
contact_center retornando `False`. Para a corrida do advisory lock, teste unitário de
`_find_or_create`/dois uploads simulando `IntegrityError`.

---

## Achados P2 — corrigir durante o piloto

### Backend / fila

**AUD-08 (WUZ-02) · Webhook responde 503 sem persistir quando a conta não tem equipe.**
`contact_center_wuzapi/controllers/webhook.py:222-228` — verificado pelo coordenador. Se
o gateway não retentar (semântica de retry do build pinado não é verificável no repo),
mensagens de clientes somem sem rastro além de um warning. Correção: persistir o evento
no inbox em estado `blocked` (mecanismo de replay auditável já existente) em vez de
recusar; documentar a semântica de retry real do build.

**AUD-09 (WUZ-03/04/13) · Contrato com o WuzAPI existe apenas em mocks do fork pinado.**
Premissas críticas não verificáveis contra o upstream: HMAC-SHA256 em
`x-hmac-signature` + JSON (o WuzAPI upstream/asternic envia form-encoded sem
assinatura), `data.jid` em `/session/status` (sem ele o outbound nunca abre), convenções
de `/chat/react` (`Body:"remove"`, `Id:"me:<id>"`), `state` do ReadReceipt lido do
envelope, unwrap de edições (`Info.ID` = alvo; `adapter.py:681-699`), erros terminais
detectados por substring de mensagem humana (`group.py:44-55`). Correção: suíte de smoke
de contrato contra a instância real da homologação cobrindo esses pontos + fixture real
de edição recebida de outro aparelho; declarar no README que o adapter exige o
fork/commit específico.

**AUD-10 (ORM-02) · Offboarding bloqueado com mensagem enganosa.** Adversarial:
CONFIRMADO. `account.py:350-369` + `_check_contact_center_groups` (`account.py:171-212`;
filtro de inativos em `:183-188`, mensagem em `:196-201`). Arquivar um agente ainda
presente em `agent_ids` levanta "These users are not Contact Center agents" — o bloqueio
fail-closed é deliberado e testado (`test_contact_center.py:583-584`, sequência
sancionada em `:627-634`), mas a mensagem não aponta a causa nem a solução e não há
fluxo assistido. Correção: diferenciar a mensagem por causa (inativo em equipe X vs. sem
grupo vs. share) e/ou oferecer ação de remoção assistida do roster no arquivamento.

**AUD-11 (PERF-06/07, ORM-03) · N+1 na serialização de conversas e timeline.**
`list_conversations`: ~5-10 queries por canal da página (member, last_binding,
last_outbox, group_profile — `application.py:3286-3322`, `:3768`), 300-1000 queries por
chamada. `get_timeline` já faz batching (`:3825-3846`), mas restam o binding do pai por
mensagem com reply (`:3009-3011`) e a resolução de participante próprio por mensagem de
grupo (`:3073-3090`) que é invariante por canal. Correção: replicar o batching do
timeline na lista e mover a resolução de participante para fora do loop.

**AUD-12 (PERF-12, ORM-04) · Guards globais em `mail.message`/`ir.attachment` sem
curto-circuito.** Adversarial: CONFIRMADO (não existe curto-circuito escondido).
`message.py:107-157` (search incondicional de bindings em todo write; O(N²) em lote via
`:132`), `message.py:205-247` (todo write/unlink de attachment), `:179-194` (toda reação
da base). Custo por operação baixo (indexado, tabela vazia retorna rápido) mas global e
incondicional. Correção barata: pré-filtrar por `message.model == "mail.channel"` /
`res_model` antes do search de bindings (invariante já garantido por `_check_binding`).

**AUD-13 (PERF-09/10/11) · Higiene de concorrência sob volume.** (a) 3-5 UPDATEs de
linhas quentes por mensagem inbound (aliases `last_seen_at`, identidade PushName com
`FOR UPDATE` que serializa inbound do mesmo contato — `application.py:1376-1381`,
`identity.py:310-356`): atualizar com granularidade (só se mudou ou se a observação
anterior for mais antiga que N minutos). (b) Recibos multi-mensagem iteram na ordem do
payload e travam binding por item (`application.py:888-905`, `:1018-1046`): ordenar por
`id` antes do loop de locks. (c) `ProviderPausedError` → retry fixo de 60 s sem teto nem
jitter (`queue.py:719-726`): adicionar jitter ±30% e backoff limitado (a recuperação
ativa pós-health já existe).

**AUD-14 (ORM-05) · Enriquecimento de aliases sem savepoint.**
`application.py:1353-1405` e `:1490-1555` fazem search-then-create sem
savepoint/IntegrityError; corrida entre eventos simultâneos do mesmo contato novo
consome uma tentativa inteira do job. Correção: replicar o padrão savepoint +
IntegrityError + re-verificação já usado em `group.py:305-334`.

**AUD-15 (WUZ-12) · Fila outbound sem TTL durante desconexão.** Desconectado, comandos
retentam a cada 60 s sem teto e ao reconectar (dias depois) tudo dispara de uma vez.
Correção: política de idade máxima configurável (falhar `pending` com mais de N horas,
com aviso ao agente na conversa).

### Frontend

**AUD-16 (UI-02/03) · Sem indicador de tempo real, sem fallback de bus, boot sem
skeleton.** Adversarial: CONFIRMADO com correções — a UI permanece interativa sem bus (o
que cessa são as atualizações automáticas, sem qualquer aviso); são exatamente 3 RPCs
sequenciais aguardados no boot (`app.esm.js:27`; cadeia `start():327→365→733→804`); o
skeleton (`contact_center_app.xml:5-13`) só é alcançável no retry. Correção: renderizar
`realtimeStatusMeta` (já existe no modelo, `contact_center_model.esm.js:1220`, label
`:157`); agendar `scheduleSynchronization` periódico enquanto
`state.realtime === "offline"`; não aguardar a cadeia inteira no `onWillStart` (montar
após o bus e deixar as fases do store dirigirem a UI).

**AUD-17 (UI-04) · Lacuna invisível na timeline após reconexão longa (>100 mensagens).**
`refreshLatestTimeline` busca as 100 mais recentes e mescla sem detectar gap
(`store:24, 830-836, 758-763`). Cruzamento: a limitação foi **aceita** na disposição de
2026-08-24 (`reviews/2026-08-24-followup-verification-disposition.md`) — mas a auditoria
recomenda ao menos **sinalizar** o gap (comparar menor `message_id` da página nova com o
maior local; havendo gap, divisor "mensagens não carregadas" ou reancorar com reset),
pois hoje o histórico exibido fica factualmente errado sem qualquer indicação.

### Testes

**AUD-18 (TST-02/03/04/06) · Lacunas de cobertura em caminhos críticos.** (a) Ramos de
erro do webhook sem teste: 404 chave desconhecida, 415, corpo vazio, envelope
não-objeto, aninhamento >32, 503 `account_not_ready`, corrida `IntegrityError` de
`_find_or_create_inbox`, teto do corpo lido (chunked). (b) Caminho de **sucesso** do
download de mídia inbound sem teste (`media.py:990-1035`), incluindo os checks negativos
de sha256/size divergentes. (c) Fronteira durável do outbox nunca exercitada entre
transações reais — todos os testes mockam `_commit_job_transaction` → a garantia "nunca
enviar duas vezes" não tem teste com dois workers reais; estender o harness de
`test_phase1_concurrency.py` para o outbox (2 workers no mesmo comando → exatamente 1
`execute_command`; worker morto pós-boundary → segundo marca `uncertain` sem
redispatch). (d) Cron `_cron_cleanup_expired` sem teste (inversão do filtro apagaria
anexos de mensagens já enviadas).

**AUD-19 (TST-05) · Testes QUnit não rodam via test-tags dos módulos; nenhum componente
OWL montado.** Os 61 testes JS (3,7k linhas) estão no bundle correto mas só rodam na
suíte do módulo `web`; nenhum smoke-tour nem `browser_js`. Um typo num template XML
quebraria o app inteiro sem nenhum teste dos módulos falhar. Correção: smoke-tour
(`web_tour`) que abre o client action e vê a lista de conversas; opcionalmente
`browser_js` post_install.

---

## Achados P3 — backlog de higiene

### Segurança (postura geral: excepcional; nenhum P0/P1/P2 explorável)

| ID     | Achado                                                                                                                       | Referência                              | Correção sugerida                                             |
| ------ | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------- |
| SEC-01 | Sem rate limiting (webhook público, upload 50 MB autenticado)                                                                | `webhook.py:169-205`, `main.py:224-261` | Documentar `limit_req` no proxy como requisito de deploy      |
| SEC-02 | Segredos WuzAPI em texto claro no banco (mitigados por `groups=`, `copy=False`, widget password, ausentes de logs/snapshots) | `provider_connection.py:98-117`         | Documentar cifração de backups + rotação; opcional: vault/env |
| SEC-03 | `wuzapi_base_url` aceita `http://` (token no header em claro)                                                                | `provider_connection.py:716-726`        | Exigir https salvo loopback/RFC1918 ou flag explícita         |
| SEC-04 | `original_body = Html(sanitize=False)` latente (hoje só recebe HTML já escapado, nunca renderizado)                          | `message.py:317`                        | Remover `sanitize=False` ou documentar o invariante           |
| SEC-05 | Oráculo 404 vs 401 no webhook + `event_id` sequencial na resposta                                                            | `webhook.py:189-205, 254-257`           | Opcional: 404 uniforme pré-HMAC; devolver sha256 em vez de id |
| SEC-06 | TOCTOU de DNS no fetch de avatar (mitigado por allowlist de domínios Meta, https, sem redirect, 2 MB, sniffing)              | `group.py:410-431, 851-859`             | Opcional: pinning do IP validado; ou aceitar documentadamente |

### ORM / modelos

| ID     | Achado                                                                                                          | Referência                                        |
| ------ | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| ORM-06 | `invalidate_cache()` deprecado no dispatch (resto do arquivo já usa o padrão novo)                              | `queue.py:683`                                    |
| ORM-07 | Computes de monitoramento sem depends (valores congelam no cache da sessão)                                     | `account.py:907-941`                              |
| ORM-08 | `_suggested_phone` pode sugerir "+<lid>" como telefone (allowlist de namespaces)                                | `application.py:3191-3214`                        |
| ORM-09 | Views expõem campos protegidos como editáveis → AccessError ao salvar (pôr `readonly="1"`)                      | `identity_views.xml:37-47`                        |
| ORM-10 | `check_company=True` inerte em `mail.channel` (sem `company_id`) — remover                                      | `channel.py:158-171`                              |
| ORM-11 | Backfill 1.17.0 sem batching (upgrade lento + pico de memória em base grande)                                   | `migrations/16.0.1.17.0/` + `identity.py:370-430` |
| ORM-12 | `_summary_by_binding` sem `flush_model` antes de SQL (hazard latente; `group.py:1269-1293` é o exemplo correto) | `group_delivery.py:332-348`                       |
| ORM-13 | `UserError(str(error))` propaga texto técnico em inglês ao agente                                               | `application.py:1727-1728`, `media.py:1252-1253`  |

### Adapter WuzAPI

| ID     | Achado                                                                                                                                      | Referência                                |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| WUZ-05 | JIDs de classe desconhecida (`@newsletter` etc.) viram conversa "direct" — allowlist de sufixos                                             | `adapter.py:421-448`                      |
| WUZ-06 | Localização/contato/enquete/sticker caem em `unsupported` invisível ao agente — monitorar contagem por conexão; projeção mínima na conversa | `adapter.py:597-621`                      |
| WUZ-07 | Zero logging no adapter; 401/404 do webhook silenciosos — log com rate-limit para falhas de assinatura                                      | `webhook.py:190, 202-205`                 |
| WUZ-08 | Sem `requests.Session`/pool (um handshake TCP+TLS por chamada)                                                                              | `adapter.py:1418` etc.                    |
| WUZ-09 | Mídia em memória com múltiplas cópias (limitada por tetos; aceitável, monitorar OOM)                                                        | `adapter.py:1705-1725`                    |
| WUZ-10 | Resposta de `_post_command` lida sem teto de tamanho (aplicar leitor limitado de 64 KB)                                                     | `adapter.py:2211-2223`                    |
| WUZ-11 | Corpo do webhook lido integralmente antes da autenticação (chunked sem Content-Length) — rejeitar ou ler com teto incremental               | `webhook.py:193-205`                      |
| WUZ-14 | Impossível limpar `wuzapi_base_url` via write (bloqueia conversão de adapter)                                                               | `provider_connection.py:256-260, 700-703` |

### Frontend

| ID    | Achado                                                                                                | Referência                                                   |
| ----- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| UI-05 | `stop()` do recorder não libera o mic se `onstop` nunca disparar — timeout de segurança               | `voice_recorder.esm.js:268-286`                              |
| UI-06 | Rótulo fixo "Limite de 15 minutos" ignora o limite do provider                                        | `message_composer.xml:140`                                   |
| UI-07 | `t-key` de mídia pode colidir (`'id:'+id` vs `'idx:'+i`)                                              | `message_content.xml:17`                                     |
| UI-08 | Acesso não defensivo a `message.author.name` em 3 templates (usar os helpers já defensivos)           | `conversation_timeline.xml:74,108`, `message_composer.xml:9` |
| UI-09 | Erro 403 de upload mostra "forbidden" cru — mapear para pt-BR                                         | `contact_center_store.esm.js:46-58`                          |
| UI-10 | Input de busca de contato sem `aria-label`                                                            | `contact_panel.xml:209-213`                                  |
| UI-11 | Timeline sem memoização de runs (2 parses Luxon ×2 por item por render) — primeiro gargalo a memoizar | `conversation_timeline.esm.js:251-284`                       |
| UI-12 | Mensagem enviada pelo próprio agente pode virar "1 nova mensagem" com viewport rolado                 | `conversation_timeline.esm.js:151-169`                       |

### Testes / performance menores

| ID      | Achado                                                                                                               | Referência                                 |
| ------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| TST-07  | `SavepointCase` deprecado em 12 classes (removido no Odoo 17) — trocar por `TransactionCase`                         | `common.py` + 10 arquivos                  |
| TST-08  | `TestWuzapiWebhook` (HttpCase) sem `@tagged("post_install","-at_install")`                                           | `test_webhook.py:12`                       |
| TST-09  | Adapter `test.fake` registrado por efeito colateral de import (dependência de ordem) — mover para `fake_adapters.py` | `test_contact_center.py:61`                |
| TST-10  | Ramos menores do adapter sem teste (Retry-After HTTP-date; `code` não-int; truncamento de stream no send)            | `adapter.py:825-835, 1754-1757, 2040-2061` |
| TST-11  | Cleanup dos testes de concorrência sem guarda contra exceção parcial                                                 | `test_phase1_concurrency.py:812-901`       |
| TST-12  | Duplicação de setup (~400 linhas) em ~8 arquivos do base — criar `common.py`                                         | vários                                     |
| PERF-14 | Dedupe de lifecycle com `=like` sem índice de prefixo (flap + tabela grande = varredura)                             | `webhook.py:103-110`                       |
| PERF-15 | Cron de limpeza de uploads sem batch                                                                                 | `media.py:1300-1308`                       |
| PERF-16 | `search_partners` com 4× `ilike` por tecla (trigram se a base for grande)                                            | `application.py:4150-4164`                 |
| PERF-08 | Busca de conversas com OR de 12 `ilike` + `search_count` extra — pg_trgm + debounce + omitir total                   | `application.py:3698-3790`                 |
| PERF-13 | Upload/download criam attachment via `datas=b64encode` — usar `raw=content` (Odoo 16) e eliminar a cópia             | `main.py:341`, `media.py:1013`             |
| PERF-17 | Fan-out de bus por membro do canal (relevante só com times grandes) — monitorar                                      | `application.py:2692-2718`                 |

---

## Cruzamento histórico com as pendências conhecidas (reviews/2026-08-24-followup-verification.md)

A auditoria foi cega às revisões anteriores; o cruzamento abaixo é do coordenador.

1. **Corrida da lane de reação (M7)** — não reencontrada pela auditoria (nenhum auditor
   mergulhou na lane de reação especificamente). **Permanece aberta** como registrada; a
   correção proposta lá (tocar a linha do binding em `_apply_reaction`
   - teste de corrida) continua válida e é congruente com AUD-13(b).
2. **Eco `from_me` descartado sem `technical_author_id`** — não reencontrada.
   **Permanece aberta.** Ganha relevância extra com AUD-02: o eco é hoje o único caminho
   de saída de `uncertain`, então contas sem autor técnico agravam o beco.
3. **Mistura de conversas no merge da timeline** — o auditor de frontend encontrou o
   **carimbo de canal na timeline já implementado** e o listou como ponto forte
   ("carimbo de canal na timeline impedindo mescla de conversas"). **Aparentemente
   resolvida** desde 24/08; recomenda-se apenas confirmar que existe teste QUnit
   cobrindo a troca rápida de conversa.
4. **Ambiguidade de correlação entre canais no lookup de mutação** — não reexaminada.
   **Permanece aberta** como registrada.
5. **Cron-vassoura para ReadReceipts `pending` sem job ativo** — a auditoria confirmou
   por grep exaustivo (AUD-05) que não existe nenhum cron novo além dos 4 conhecidos.
   **Permanece aberta.**
6. **Lacuna >100 na reconexão** — aceita na disposição de 24/08; ver AUD-17 para a
   recomendação mínima de sinalização.

Higiene de processo (fora de código): repositório segue **sem remote** (pendência de
24/08) e há um diretório `setup/` não rastreado no working tree (`git status`:
`?? setup/`) — commitar ou ignorar explicitamente.

## O que está comprovadamente bem — não "corrigir"

Registrado para evitar regressões por excesso de zelo do implementador:

- **Webhook**: HMAC-SHA256 constant-time sobre o corpo bruto, chave de rota
  `secrets.token_urlsafe(32)` com UNIQUE, teto de 1 MB, sanitização recursiva com limite
  de profundidade, dedupe por sha256 com savepoint/IntegrityError,
  persistir-primeiro/processar-assíncrono. Payload malformado nunca gera 500.
- **Outbox**: fronteira durável (commit antes do I/O), `uncertain` nunca redispatchado
  sem verificação (AUD-02 pede um caminho de **verificação**, não um redispatch cego),
  correlação por `Id` cliente conferida na resposta, ordem de locks
  account→connection→outbox documentada.
- **XSS**: zero `t-raw`/`innerHTML`; escape na entrada (`html_escape` em todos os
  caminhos) e `t-esc` na saída; allowlist dupla de URLs de mídia no cliente; CSP
  `default-src 'none'; sandbox` + `nosniff` no serving.
- **SQL 100% parametrizado**; ~248 usos de `sudo()` todos atrás de
  `_authorized_channel`/`_check_agent`/capability tokens de identidade de objeto (não
  forjáveis via RPC); Discuss nativo integralmente bloqueado para canais contact_center.
- **ACLs/ir.rules**: menor privilégio (agente só lê; escrita via serviço), 30+ rules com
  empresa + escopo de team/membership, campos sensíveis com `groups=` admin.
- **Constraints SQL** cobrindo exatamente as chaves quentes (dedupe de inbox,
  `external_message_id` por conversa, aliases por conta, uma conexão outbound por
  conta); `model_create_multi` em todos os overrides; zero `onchange`; migrations
  idempotentes.
- **Testes de concorrência** com transações PostgreSQL reais, threads e barriers —
  qualidade de referência; mocking disciplinado só na fronteira `requests`.
- **Acessibilidade** do frontend acima do padrão (menus com navegação por teclado,
  aria-live, inert, prefers-reduced-motion).

## Plano original sugerido (histórico; não executar sem consultar a disposição)

**Onda 1 — antes do aceite do piloto (P1):**

1. AUD-01 — unificar ordem de locks canal→binding (+ teste de corrida no harness
   existente).
2. AUD-02 — `ConnectionError`/`ConnectTimeout` pré-envio → `transient`; ação admin
   "verificar e reenfileirar" para `uncertain`.
3. AUD-03 — sync silencioso da lista sem truncar paginação (+ teste QUnit).
4. AUD-04 — capacities por canal (deploy + USAGE.rst) e read-timeout de download
   reduzido; avaliar commit pré-I/O no job de mídia.
5. AUD-05 — cron de retenção dos ledgers com janelas configuráveis.
6. AUD-06 — ETag/304 + leitura parcial de Range no serving de mídia.
7. AUD-07 — `test_http_endpoints.py` (valida também AUD-06).

**Onda 2 — durante o piloto (P2):** AUD-08 (persistir `account_not_ready`), AUD-09
(smoke de contrato na homologação + fixture real de edição), AUD-16 (indicador de
realtime + polling de contingência + boot), AUD-18 (lacunas de teste, prioridade para a
fronteira durável do outbox), AUD-11/12/13/14 (N+1, guards, higiene de concorrência),
AUD-10, AUD-15, AUD-17, AUD-19.

**Onda 3 — backlog:** P3s das tabelas acima, começando por SEC-03 (https), ORM-06,
TST-07/08 (dívidas de migração Odoo 17) e PERF-13 (`raw=` em vez de `datas=`).

Cada correção da Onda 1 deve vir com teste que falharia sem ela; as validações por
achado estão descritas nos próprios itens.
