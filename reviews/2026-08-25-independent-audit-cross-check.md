# Cross-check da disposição da auditoria independente — 2026-08-25

- Fontes confrontadas: `reviews/2026-08-25-independent-audit-disposition.md` e o marco
  de `plan.md` (linhas 1583-1615), contra o **código atual** (árvore de trabalho sobre
  `89133a3`: 65 arquivos modificados, 19 caminhos novos, ~6 370 linhas inseridas).
- Método: 7 verificadores independentes (6 blocos de afirmações + 1 auditoria do código
  novo), instruídos a **não confiar** nos números de linha da disposição, a localizar os
  símbolos e a julgar cada refutação pelo mérito técnico; mais verificação manual do
  coordenador nos pontos de maior risco (primitiva de lock, cron de recuperação, serving
  de mídia, contagem de testes).
- Total: **65 afirmações verificadas**.

## Placar

| Veredito                            |  Nº |
| ----------------------------------- | --: |
| Implementado e correto              |  26 |
| Implementado com lacuna             |  23 |
| Refutação do Codex procede          |  11 |
| Não implementado (declarado ou não) |   3 |
| Refutação do Codex **não** procede  |   2 |

**Veredito geral: a disposição é honesta e tecnicamente competente.** Nenhuma afirmação
de "corrigido" se mostrou falsa: os 19 P3 alegados existem nos símbolos corretos, e as
correções centrais (ordem canônica de locks, streaming de mídia, testes HTTP,
persistência `blocked`, prefetch em lote, jitter, recuperação de ledgers órfãos) são
reais e, em vários casos, **melhores que o recomendado pela auditoria**. Das 13
refutações, 11 procedem — em dois casos com argumento mais forte do que o próprio Codex
enunciou.

Mas o cross-check encontrou **dois P1 novos** (um deles exposto pela própria lógica de
refutação do Codex, e outro no código novo que nunca passou por auditoria), **três
regressões introduzidas nesta rodada** e **duas refutações que não se sustentam**.

---

## Achados novos que exigem ação

### XC-01 · P1 · Corrida benigna de alias de grupo vira evento `dead` com diagnóstico falso

**Este defeito é consequência direta do argumento que o Codex usou para refutar o AUD-14
— e está no código que eles mantiveram.**

A refutação está tecnicamente certa: sob `REPEATABLE READ` (o padrão dos cursores Odoo
16), `savepoint + rollback + re-search` **não** vê a linha commitada concorrentemente,
porque o snapshot é fixado na primeira sentença da transação e o rollback ao savepoint
não o renova. Um `FOR UPDATE` ou advisory lock adquirido depois também não renova.

Aplicando esse mesmo argumento a `contact_center_base/models/group.py:305-334` — o
padrão que a auditoria elegeu como exemplar e que a disposição preservou:

1. Dois eventos de grupo do mesmo participante novo processados em paralelo (o canal de
   inbox roda com concorrência 3 na própria homologação).
2. T2 tenta criar o alias → `unique_violation` em `profile_address_unique`
   (`group.py:1537-1543`).
3. Savepoint faz rollback; o re-search (`group.py:320-327`) retorna **vazio por
   construção**.
4. `existing.participant_id != participant` é **True** (recordset vazio ≠ participante).
5. `raise ValidationError("The newly observed group alias belongs to another participant.")`
   (`group.py:328-334`).
6. `queue.py:461-462` trata `ValidationError` como **terminal**: o evento de inbox vai a
   `dead`, sem retry.

Alcançável pelo pipeline inbound em `application.py:378-384` (mutação de grupo),
`:461-467` (alvo observado) e `:1033-1039` (recibo de grupo).

Resultado: uma corrida **benigna** (mesmo participante, alias idêntico) vira evento
morto, com diagnóstico **falso** de conflito, exigindo replay manual. É estritamente
pior que o comportamento do `_enrich_*` do AUD-14, que ao menos degrada para
`RetryableJobError` e se auto-recupera.

**Correção mínima, alinhada à doutrina do próprio Codex:** quando o re-search retorna
vazio após `IntegrityError`, **não concluir conflito** — levantar erro retentável
(consumir a tentativa transacional, que roda com snapshot novo). Reservar a
`ValidationError` para o caso em que `existing` de fato existe e aponta outro
participante.

**Secundários da mesma família** (benignos, mas registrados): `_find_or_create_inbox`
(`contact_center_wuzapi/controllers/webhook.py:176-187`) devolve HTTP 500 espúrio em vez
de `200 duplicate` na corrida; mesmo padrão em `contact_center_meta/models/delivery.py`
e `contact_center_meta/controllers/webhook.py`. E o comentário de `group.py:540-542`
("Lock the canonical channel first") sugere uma proteção que sob RR não existe —
corrigir o comentário para não induzir o próximo leitor ao erro.

### XC-02 · P1 · Referral Meta malformado descarta a mensagem do cliente (código novo, remoto)

Divergência de contrato entre o saneador do webhook Meta e o DTO:

- `contact_center_meta/services/webhook.py:61-62` (`_bounded_text`) **aceita** `\n`,
  `\r`, `\t` e não faz `strip` — logo aceita string só-espaço em `referral.ref` e
  `referral.ad_id`.
- `contact_center_base/services/dto.py:186-191` (`AttributionDTO`) **rejeita**
  exatamente esses valores.
- `contact_center_base/models/queue.py:432-447` executa `_capture_attribution` num
  savepoint **sem `except` próprio**, no mesmo `try` da normalização.

Cadeia: `ref=" "` ou `ref="a\tb"` passa pelo webhook, é persistido, estoura
`DTOValidationError` na normalização → o evento de inbox vai a `dead` com
`max_retries=0` → **a mensagem do cliente nunca chega ao agente**, e `action_requeue`
refaz o mesmo caminho e morre igual.

Gatilho remoto trivial: `m.me/<page>?ref=%20`.

**Correção (as duas partes são necessárias):** (a) alinhar o saneador ao contrato do DTO
(`strip` + rejeitar vazio/whitespace em `ref`/`ad_id`); (b) dar `except` próprio à
captura de atribuição, de modo que **falha de telemetria nunca descarte mensagem** — o
comentário em `queue.py:437-439` já declara essa intenção para o sentido inverso, mas o
código não a implementa neste sentido.

### XC-03 · P2 · Nova inversão de locks introduzida pela captura de atribuição

A correção do AUD-01 ordenou os locks **explícitos**, mas a captura de atribuição
introduziu um lock **implícito** de FK antes deles, na mesma transação do job de inbox:

- `queue.py:439-444`: `_capture_attribution` roda **antes** de `_process_normalized`
  (savepoint não libera row locks).
- `contact_center_base/models/attribution.py:149-155`: `channel_binding_id` é FK real
  (`ondelete="restrict"`); o INSERT/UPDATE dispara o RI check do PostgreSQL
  (`SELECT 1 FROM contact_center_channel_binding ... FOR KEY SHARE`), mantido até o
  commit.
- Só depois o fluxo chega a `_post_inbound` → `_lock_inbound_projection_binding` →
  `mail_channel FOR UPDATE`.

Ordem efetiva: **binding (KEY SHARE) → canal (FOR UPDATE)** — inversa do caminho de
envio do agente. `FOR UPDATE` conflita com `FOR KEY SHARE`.

Cenário: conversa **já existente**, cliente chega por anúncio Click-to-WhatsApp enquanto
o agente envia mensagem na mesma conversa — exatamente o fluxo de aquisição que a
feature existe para medir. Mensagens comuns não tocam o binding
(`attribution.py:608-609` retorna cedo sem atribuição), e o primeiro anúncio de conversa
nova resolve `channel_binding_id = False`.

**Correção natural:** linkar o touchpoint ao binding somente em `_link_projection`
(`queue.py:544`), já sob os locks canônicos, gravando `channel_binding_id=False` na
captura.

### XC-04 · P2 · Inversão canal↔group_profile em recibos de grupo

Mesma classe (lock implícito de FK), contra a ordem global que o próprio código declara
em `group.py:606-608`:

- `_apply_group_delivery_event` (`application.py:1033-1039`) chama
  `_group_roster_participant(..., enrich=True)` **antes** de qualquer lock de canal; o
  alias criado (`group.py:296-318`) tem `group_profile_id` como related **stored** com
  FK (`group.py:1487-1492`) → `FOR KEY SHARE` no perfil.
- Só depois `_record_receipt` adquire canal `FOR SHARE` e perfil `FOR SHARE`
  (`group_delivery.py:243-275`).

Ciclo com `_request_sync` (canal `FOR UPDATE` → perfil `FOR UPDATE`). Frequência menor
(só na primeira observação de cada alias), mas determinístico quando ocorre.

### XC-05 · P2 · Regressão do AUD-11: uma conversa de grupo sem perfil derruba a página inteira

`application.py:4366-4370` injeta
`prefetched["group_profile_by_binding"].get(binding.id)` **sem default** → `None` quando
não há linha de perfil; `application.py:3826-3830` desreferencia `group_profile.name`
**sem guarda**. Os demais usos no mesmo retorno são guardados
(`application.py:3974-4001`).

Antes da otimização o valor era sempre recordset vazio (`.name` → `False`), degradando
graciosamente. Agora um único canal de grupo sem perfil produz
`AttributeError: 'NoneType' object has no attribute 'name'` — **erro 500 na página
inteira** de `list_conversations`, não apenas no item.

Alcançabilidade hoje é baixa (o perfil nasce na mesma transação do binding), mas basta
um backfill, um merge ou um `unlink` administrativo. **Correção:**
`.get(binding.id, empty_profile)`. Não há teste para binding de grupo sem perfil.

### XC-06 · P2 · Regressão do AUD-03: `targetCount` cresce sem teto

`refreshLoadedConversations` (`contact_center_store.esm.js:792-856`) preserva N páginas
corretamente, tem cerca de `listRequest` e teste QUnit dedicado. Mas o push de
`previousConversation` em `applyConversationPage:684` **realimenta** `loadedCount`:
enquanto a conversa selecionada estiver fora da janela (filtro de estado/busca, ou
empurrada para baixo por conversas mais novas), `targetCount` cresce **+1 a cada evento
de bus**, escalando até recarregar a tabela inteira.

Somado à ausência de teto em `targetCount` e ao reinício por `listRequest` a cada
evento, produz amplificação de RPC O(N) e possível **starvation**: com N páginas
(refresh de ~600 ms+) e eventos a cada ~120-250 ms — cenário realista no laboratório com
223 conversas — a lista pode congelar enquanto RPCs abortados consomem trabalho no
servidor.

**Correção:** teto explícito em `targetCount` e não realimentar `loadedCount` com a
conversa selecionada re-injetada.

### XC-07 · P2 · Lane de mídia martela 401 indefinidamente na rotação de credencial

A rejeição do backoff crescente (AUD-13) é **defensável em metade do escopo e
indefensável na outra**, e a disposição não distingue as duas.

Defensável: quando `ProviderPausedError` vem de checagem **local** (`queue.py:1065`,
`group.py:779`, `identity_avatar.py:431`), cada retentativa custa uma transação curta e
nenhuma chamada ao provider.

Indefensável — lane de mídia:

1. **Não há preflight**: `media.py:981-991` chama o provider incondicionalmente (grep
   por `connected|is_available|observation_is_fresh` em `media.py` não retorna nada, ao
   contrário de grupo e avatar).
2. Um 401/403 real vira `ProviderPausedError` **depois** do I/O
   (`contact_center_wuzapi/services/adapter.py:2552-2554`).
3. Com `ignore_retry=True` (`media.py:1036-1041`) e atraso **fixo** determinístico
   (61-78 s), cada mídia pendente repete indefinidamente uma requisição 401 — para
   sempre, sem escalar, sem marcar falha, sem parar. Com N mídias pendentes o regime é
   ~N/65 requisições 401 por segundo, justamente durante um incidente de credencial.

O argumento "mídia precisa continuar sondando sem consumir o teto de falhas" é um
**falso dilema**: backoff crescente **com teto** — reutilizando
`PROVIDER_PAUSED_RETRY_MAX_SECONDS` (`services/job.py:7`), que já existe e hoje só
clampa `Retry-After` — preserva as duas propriedades desejadas e reduz o martelo em
~55×. Alternativa independente: dar ao lane de mídia o mesmo preflight que grupo e
avatar já têm.

Nota factual: o jitter é **positivo** (0 a +30%, 61-78 s) e determinístico por
`(lane, id)`, não ±30% — desfasa a frota, mas não re-desfasa as retentativas do mesmo
job. A disposição descreve isso honestamente.

### XC-08 · P2 · A refutação do AUD-05 responde a uma proposta que não foi feita

Três argumentos da refutação são válidos e verificados: (i)
`blocked`/`dead`/`unsupported` precisam mesmo do bruto — o replay em `queue.py:318-330`
depende disso; (ii) o ledger Meta é imutável por contrato **aplicado em código**
(`contact_center_meta/models/delivery.py:183-193`); (iii) "nunca uma exclusão genérica"
é política correta e madura.

O problema é de escopo: a recomendação original **já excluía explicitamente os estados
não terminais** e pedia apenas limpar os JSONs de eventos `done` antigos preservando
metadados de auditoria. Responder "os não terminais precisam do bruto" refuta uma
proposta que não foi feita.

Para `done`, o próprio código prova que não há leitor:

- `raw_envelope_json` só é lido em `queue.py:516` (dentro de `_normalize_one`,
  alcançável apenas por processamento/replay), e o replay manual (`queue.py:318-330`) só
  reseta `blocked`/`unsupported`/`dead`. Para `done`: **zero leitores**.
- `normalized_dto_json` de `done` tem um único leitor histórico — `identity.py:650-670`,
  chamado uma vez pela migração `16.0.1.17.0`. Nenhum leitor em runtime.
- Em operação saudável praticamente todo evento termina em `done` — ou seja, **o estado
  que domina o volume é exatamente aquele para o qual o argumento não se aplica**.

Obstáculo técnico real que nem a auditoria nem a disposição mencionaram:
`raw_envelope_json` é `required=True` (`queue.py:183-186`), então limpá-lo exige tornar
o campo opcional. Esse seria o argumento legítimo de custo — não foi o usado.

**O que falta hoje não é o cron: é qualquer medição.** Não há contador nem alerta de
tamanho de tabela, então a decisão de retenção está sendo adiada sem o dado que
permitiria tomá-la depois. Mitigação mínima antes do piloto, sem exclusão genérica e sem
tocar o contrato Meta: (a) expor tamanho por ledger no contrato de runtime já existente;
(b) limpar `normalized_dto_json` de `done` com mais de N dias (campo já opcional, sem
migração); (c) deixar `raw_envelope_json` para depois.

### XC-09 · P2 · AUD-02 permanece aberto para a classe ambígua

A parte implementada é sólida e melhor do que eu esperava: validado no stack pinado
(requests 2.31.0 / urllib3 2.0.7) que **DNS inexistente, conexão recusada e falha de
proxy caem em `transient`**, enquanto `ReadTimeout` e reset em voo permanecem
`uncertain`, sem falso positivo, com dois testes dedicados (`test_adapter.py:2486` e
`:2502`).

Permanece descoberto: **handshake TLS falho e 5xx de reverse proxy** continuam
`uncertain` terminal — logo o cenário original ("container WuzAPI reinicia") só está
resolvido quando o Odoo fala **direto** com o gateway, sem proxy no meio.

A refutação do mecanismo proposto **procede** (o build pinado não expõe consulta de
mensagem por ID; o whatsmeow não guarda histórico server-side). Mas ela foi usada para
encerrar o achado inteiro, e alternativas não-duplicantes nunca foram consideradas:
escalar para `dead`/falha visível após carência sem eco, ou ação admin "marcar como
falha". Hoje, para um comando `uncertain`: `delivery_state` fica `queued` **para
sempre** (envenenando `oldest_pending_at` e qualquer métrica de fila), o admin só vê um
contador, e a única recuperação real é o agente redigitar a mensagem — **contra a
instrução explícita da própria UI** ("não reenviar").

### XC-10 · P2 · LGPD: nenhuma retenção, nenhum apagamento, `unlink` bloqueado em toda parte

`grep` por `lgpd|anonym|erasure|forget|purge|consent` nos módulos retorna **zero**
ocorrências. Simultaneamente todo `unlink` está bloqueado (`attribution.py:304-307` e
`831-834`; `delivery.py:190-191` e `485-486`). Os ledgers
`contact.center.meta.webhook.delivery.raw_envelope_json`,
`contact.center.meta.webhook.item.raw_event_json` e
`contact.center.inbox.event.raw_envelope_json` guardam texto de mensagem e crescem sem
expurgo.

Ou seja: hoje o produto **não tem como atender a um pedido de apagamento** e o conteúdo
de mensagem é retido em triplicata sem opção de truncamento ou redação. A minimização de
**saída** é boa (`_safe_projection_for_binding` omite `source_url`,
`provider_extensions_json` e `identifier.value`, com teste em
`test_attribution.py:353-372`); o problema é a retenção **em repouso**.

---

## Refutações do Codex que procedem (a auditoria estava errada)

Registro explícito, porque o valor de um cross-check está tanto em confirmar quanto em
corrigir:

| Refutação                                 | Veredito                             | Observação                                                                                                                                                                                                                                                                            |
| ----------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **AUD-01 (mutação)**                      | Procede                              | Mapeei toda a cadeia de `_send_message_mutation` incluindo locks implícitos: nenhuma tabela escrita (`message_mutation`, `outbox_command`, `queue_job`, `bus_bus`) tem FK para `mail_channel`, e não há write ORM em `mail.channel`. O fluxo pega B e nunca pede C — não fecha ciclo. |
| **AUD-14 (savepoint)**                    | Procede                              | O mecanismo `REPEATABLE READ` está correto; a recuperação por consumo de tentativa transacional (job com snapshot novo) é verificadamente adequada. **A correção que a auditoria recomendou teria sido inócua.**                                                                      |
| **AUD-12 (`ir.attachment`)**              | Procede — mais forte que o enunciado | O curto-circuito por `res_model` que a auditoria sugeriu **teria sido regressão de segurança**: os anexos do produto nascem com `res_model='mail.message'`/`contact.center.media.upload`, nunca `mail.channel` — o filtro desproteria 100% das mídias.                                |
| **ORM-07**                                | Procede                              | Compute não armazenado recalcula em RPC novo.                                                                                                                                                                                                                                         |
| **ORM-10**                                | Procede                              | `check_company` é inerte por dois motivos independentes.                                                                                                                                                                                                                              |
| **WUZ-14**                                | Procede                              | `adapter_key` é imutável; trocar provider exige nova conexão.                                                                                                                                                                                                                         |
| **AUD-04 (fronteira de commit em mídia)** | Procede                              | Coerente: sem commit intermediário, um worker morto rola de volta a `pending` limpo, recuperável pelo cron novo. Adicionar commit criaria um estado `downloading` preso exigindo outro protocolo.                                                                                     |
| **AUD-02 (consulta negativa)**            | Procede no mecanismo                 | Ver XC-09: o mecanismo é impossível, mas o achado foi encerrado sem considerar alternativas.                                                                                                                                                                                          |
| **AUD-02 (UI)**                           | Procede                              | `uncertain` já era distinguido na UI desde 2026-08-23 com rótulo, tom e ícone próprios — **essa parte do meu relatório estava desatualizada**.                                                                                                                                        |
| **WUZ-11**                                | Procede                              | A "correção absoluta" era mesmo impossível: o ramo `_cached_data` está fora do alcance do addon.                                                                                                                                                                                      |

## Correções confirmadas como implementadas e corretas

Verificadas por símbolo (não por número de linha, que sofreu drift):

- **Ordem canônica de locks** — `_contact_center_lock_channel_then_binding`
  (`channel.py:1092-1118`) com revalidação pós-lock, usada no envio, inbound direto,
  inbound de grupo, echo `from_me` novo e nos dois hints. Teste de concorrência **real**
  (threads, cursores separados, commits, barreira) em `test_phase1_concurrency.py:1237`.
- **Avatar no-op sem locks** — testes de TTL/estado precedem os locks
  (`identity_avatar.py:219-244`) e são repetidos sob eles. Remove o gatilho de alta
  frequência do ABBA original.
- **Serving de mídia** (melhor que o recomendado) — autorização **antes** do `sudo()`,
  `ir.binary`/`Stream` com filestream, sniffing sobre apenas 4 KB de cabeçalho, ETag por
  sha256, `conditional=True`, CSP e `nosniff` preservados, e checagem extra de
  integridade `stream.size` vs `size_bytes` (`controllers/main.py:384-435`).
- **Cron de recuperação de ledgers órfãos** — a mudança de maior risco desta rodada, e
  está correta: a lane de outbox exclui `dispatch_job_uuid IS NOT NULL` e
  `dispatch_started_at IS NOT NULL`, respeitando a fronteira durável; `SKIP LOCKED`,
  carência de 300 s, teto por lane, readoção por `identity_key`, `flush_model` antes do
  SQL cru e cinco testes. **Nenhum vetor de duplo envio.**
- **Testes HTTP** — `test_http_endpoints.py` (457 linhas, 7 testes, `post_install`,
  `url_open` real autenticado) cobrindo multipart/CSRF, idempotência, Range/206/416/304,
  avatares e bloqueio das rotas nativas do Discuss.
- **19 de 19 P3 alegados existem de fato** — nenhum "corrigido no papel".
- **Promoção de alias (achado novo deles)** — era bug real; escopo e confiança agora são
  promovidos como invariante única, com teste entre duas contas da mesma empresa.
- **Contagens de teste conferem**: 250 métodos no base; 250+126+37 = 413 integrados —
  exatamente os totais declarados.
- **plan.md atualizado com honestidade**, separando implementado / parcial / rejeitado /
  backlog e afirmando explicitamente que a homologação técnica **não** substitui o
  aceite operacional.

## Lacunas menores registradas

- **PERF-13 incompleto**: `group.py:1214` e `identity_avatar.py:597` ainda criam anexos
  com `datas=base64.b64encode` (avatares, pequenos).
- **PERF-15**: cron de hora em hora com lote de 500 → drenagem máxima de 500 uploads/h.
- **AUD-10**: a mensagem nova cobre as quatro causas, mas o caminho de membro-de-canal
  (`channel.py:572-593`) ainda emite texto genérico, e nenhum teste afirma o texto.
- **AUD-13a**: aliases foram throttled a 5 min e testados, mas o `FOR UPDATE` + UPDATE
  por mensagem em `contact_center_identity` continua intacto
  (`observed_name_inbox_event_id` muda a cada evento).
- **AUD-19**: nenhum tour, nenhum `browser_js` — confirmado como backlog. Agravante: os
  templates ganharam bindings novos (`realtimeMeta` em `conversation_list.xml:13-22`)
  sem nenhum teste montado que exercite a renderização.
- **WUZ-11 (chunked)**: a leitura crua de `wsgi.input` não decodifica framing chunked,
  então um remetente legítimo com `Transfer-Encoding: chunked` falharia na assinatura.
- **META-11**: o mesmo conceito normaliza para tokens diferentes conforme o provedor no
  ledger dito "provider-neutral" (`source_type` "ads" no Meta vs "ad" no WuzAPI). Quanto
  mais tarde padronizar, maior a janela de dados heterogêneos.
- **META-12**: `observed_at` dos identificadores usa `self.occurred_at` do touchpoint,
  que nunca é atualizado no enriquecimento — análises de janela temporal ficarão erradas
  para touchpoints enriquecidos.
- **META-13**: `_link_projection` recebe recordset sem `sudo` (union rebrowseia no env
  do receptor), tornando falsa a asserção "o ledger só é escrito por sudo" em um dos
  dois caminhos.
- **META-16**: a chave canônica não inclui a conversa quando `source_kind == "message"`.
- **META-10**: `referral` standalone (sem `message`) vira `unsupported` — dívida
  rastreada, recuperável enquanto não houver expurgo.

## Observação sobre o módulo novo

`contact_center_meta` (16.0.1.1.0) e `contact_center_base/models/attribution.py` (846
linhas) **entraram nesta rodada sem nunca terem passado por auditoria** — a auditoria de
25/08 analisou o snapshot anterior. A auditoria feita agora encontra código de qualidade
acima da média (webhook fail-closed, HMAC verificado byte a byte antes do parse de JSON,
ACLs de menor privilégio, imutabilidade com guarda de token de processo, idempotência
com advisory lock, ~1 500 linhas de teste bem direcionadas), com os defeitos XC-02,
XC-10 e as lacunas META-\* acima.

Correção de premissa útil para o plano: **`contact_center_meta` não é o ledger de
atribuição.** É o módulo de ingestão de webhooks Messenger/Instagram. O ledger de
atribuição está em `contact_center_base/models/attribution.py`, e o vetor
Click-to-WhatsApp é alimentado pelo adapter **WuzAPI**, não pelo Meta. Qualquer review
futuro que trate os dois como a mesma coisa vai auditar o arquivo errado.

## Risco de processo

Todo o trabalho desta rodada — 65 arquivos modificados, 19 caminhos novos incluindo um
módulo inteiro, ~6 370 linhas — está **não commitado**, num repositório que segue **sem
remote** (pendência aberta desde 24/08). A homologação declara hash de árvore e
evidências, mas a árvore existe em cópia única nesta máquina. Commitar e criar o remote
é a ação de menor custo e maior valor pendente.

## Recomendação

**Antes do aceite operacional do piloto:**

1. **XC-01** — re-search vazio não é conflito (`group.py:328-334`); levantar erro
   retentável. É o único achado que produz evento morto com diagnóstico falso.
2. **XC-02** — alinhar saneador Meta ao contrato do DTO **e** isolar a captura de
   atribuição com `except` próprio, para que telemetria nunca descarte mensagem.
3. **XC-05** e **XC-06** — as duas regressões de superfície (`.get` sem default;
   `targetCount` sem teto), ambas de correção trivial.
4. **Commitar o trabalho e criar o remote.**

**Durante o piloto:** XC-03 e XC-04 (inversões por lock implícito de FK — a lição
estrutural é que a ordem canônica precisa considerar FKs, não só locks explícitos),
XC-07 (backoff com teto ou preflight no lane de mídia), XC-08 (instrumentar volume dos
ledgers antes de decidir retenção), XC-09 (caminho de saída para `uncertain` ambíguo),
XC-10 (política de retenção/apagamento).

Os ensaios operacionais que a disposição lista como pendentes — queda/reconexão real,
canário `uncertain`, carga — permanecem corretamente identificados como o gate restante.
