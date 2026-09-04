# Revisão do `plan.md` — Contact Center (Odoo 16)

- Data: 2026-08-21
- Revisor: Claude (papel: auditor)
- Alvo: `plan.md` (448 linhas) + `research/*.md`
- Evidência verificada no fonte: Odoo 16.0 (checkout local `/tmp/odoo16-mail-cjnUDi`,
  `addons/mail`) e WuzAPI @ `919c72c9` (`handlers.go`, `wmiau.go`).

## Veredito

**GO para a Fase 0, com três correções antes de escrever código** (P0 abaixo). O plano é
consistente, as fronteiras provider/core/UI estão bem traçadas, o modelo guest-first
está correto em relação ao fonte do Odoo 16 e a pesquisa de JID/LID é de boa qualidade
(commits pinados, regras de evidência). Os problemas encontrados são lacunas
operacionais e dois efeitos colaterais do Discuss nativo que o plano não antecipa — não
são erros de arquitetura.

## Afirmações do plano verificadas no fonte

| Afirmação do plano                                                                  | Resultado                                                                                                                                        | Evidência                                                                                                           |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `message_post()` só grava `author_guest_id` com public user + guest no contexto     | **Confirmado**                                                                                                                                   | `mail_thread.py:2076-2082`; `mail_guest.py:49-54` lê `context['guest']`                                             |
| `channel_type` novo via `selection_add` com conversão para `group` na desinstalação | **Viável** — `ondelete={'contact_center': 'set group'}` é política aceita                                                                        | `odoo/fields.py:2678` (`startswith('set ')`); precedente `im_livechat` usa `selection_add` + `ondelete`             |
| `group_public_id` não quebra em tipo novo                                           | **Confirmado**                                                                                                                                   | `_compute_group_public_id` zera para tipo ≠ `channel` (`mail_channel.py:196-199`); CHECK constraint só exige NULL   |
| Canais `contact_center` não aparecem no Discuss                                     | **Parcial** — não entram no `init_messaging`/sidebar, **mas** chegam ao cliente via bus (ver P0-1)                                               | `res_partner.py:163-180` filtra `channel/group/chat`; `discuss_sidebar_category.js:137` filtra por tipos suportados |
| ACL por empresa/equipe via record rules                                             | **Atenção** — a regra nativa de `mail.channel` para tipo ≠ `channel` é `is_member = True`; `mail.message` herda (leitura = leitura do documento) | `mail_security.xml:5-21`; `mail_message.py:347-354`                                                                 |
| Worker por `ir.cron`                                                                | Viável, mas latência ≥ 60 s sem `_trigger()`                                                                                                     | `ir_cron.py:469` (`_trigger` existe no 16)                                                                          |
| WuzAPI: webhook autenticado                                                         | **Confirmado** — HMAC (`callHookWithHmac`), corpo `jsonData/userID/instanceName`, token **não** vai no corpo                                     | `wmiau.go`                                                                                                          |
| WuzAPI: webhook não transporta binário                                              | **Depende de configuração** — entrega `base64 \| s3 \| both`                                                                                     | `wmiau.go` (`processMedia`, `s3cfg`)                                                                                |
| WuzAPI: edit/delete                                                                 | **Confirmado** (`SendEditMessage`, `DeleteMessage`). `markread`/`react` não confirmados na leitura (arquivo truncado) — verificar                | `handlers.go`                                                                                                       |
| WuzAPI: ID de mensagem                                                              | **Novo fato útil**: todos os handlers de envio aceitam `Id` opcional; se vazio usam `GenerateMessageID()`; resposta devolve `Id` + `Timestamp`   | `handlers.go` (`textStruct.Id`, `if t.Id == ""`)                                                                    |

## P0 — corrigir antes da Fase 0

### P0-1. O Discuss do agente vai abrir uma janela de chat a cada mensagem inbound

Cadeia verificada:

1. `mail.channel._notify_thread` emite `mail.channel/new_message` no bus do canal para
   **qualquer** tipo (`mail_channel.py:549-565`, `:704-716`).
2. `ir_websocket._build_bus_channel_list` inscreve o agente em **todos** os
   `partner.channel_ids`, sem filtrar tipo (`ir_websocket.py:22-38`). Como o plano faz
   os agentes membros do canal, eles recebem o evento.
3. No cliente, `_handleNotificationChannelMessage`
   (`messaging_notification_handler.js:282-340`) para tipo desconhecido: chama
   `channel_info`, **pina** o canal, insere a mensagem, `markAsFetched()` e, para
   `channel_type !== 'channel'` em desktop, **`chatWindowManager.openThread()`**.
4. `channel.correspondent` (`channel.js:91-103`) trata tipo ≠ `channel` como chat: com
   guest + 1 agente, o "correspondente" vira o outro agente; `displayName` cai em
   `thread.name`.

Consequências: popup nativo a cada mensagem do cliente, contador de não lidas do Discuss
poluído, risco de o agente responder pela janela nativa (mensagem que **nunca** sairá,
pois `message_post` genérico não gera outbox — por decisão correta do plano).

Recomendação: para `channel_type = 'contact_center'`, sobrescrever `_notify_thread` (ou
`_channel_message_notifications`) para **não** emitir `mail.channel/new_message` a
membros partner — a UI própria já usa bus próprio. Manter o restante nativo (seen/unread
por membro, reactions, attachments). Acrescentar ao aceite da Fase 0: "nenhuma janela de
chat do Discuss abre; zero erros JS no cliente do agente com um canal `contact_center`
pinado".

### P0-2. Latência do worker: `ir.cron` sem `_trigger()` = até 60 s por salto

Inbound tem dois saltos (webhook→inbox→worker) e outbound também. Sem gatilho, a
conversa fica com 1–2 min de atraso. `ir.cron._trigger()` existe no 16
(`ir_cron.py:469`): chamar após persistir o inbox (controller) e após o commit de
`send_message()`. Registrar também a dependência de `--max-cron-threads` no SERVIDOR02
(verificar valor em prod antes do piloto) e um alvo de aceite mensurável (ex.: p95
webhook→`mail.message` < 5 s em homolog).

Detalhe de webhook: se a persistência do inbox falhar, responder 5xx (WuzAPI faz retry);
nunca 2xx "fail-open".

### P0-3. Chave da conversa em DM: endereço ou identidade?

O plano resolve a conversa por `contact.center.channel.alias` (endereços PN/LID do
chat). Dois primeiros eventos sem alternativo — um com chat PN, outro com chat LID —
criam **duas identities e dois canais**. Quando depois chega o mapeamento PN↔LID, o
plano cobre a consolidação de identidade (conflito explícito), mas **não diz o que
acontece com os dois canais** (e o `UNIQUE(channel_binding_id, external_message_id)` não
pega duplicatas entre eles).

Recomendação: para conversas diretas, definir a conversa como `(account, identity)` — um
canal aberto por identidade por conta — e tratar `channel.alias` como
evidência/roteamento, não como chave. Definir operação explícita de consolidação de
canais (sobrevivente + redirect + fechamento do outro), mesmo que manual no MVP.

## P1 — resolver antes da Fase 1

### P1-1. Usar `Id` pré-atribuído da WuzAPI (capability `client_message_id`)

Todos os envios aceitam `Id`. Gerar o ID externo no core **antes** de chamar o provider
e gravá-lo no `message.binding` na mesma transação do outbox. Ganhos diretos:

- o eco `from_me` do webhook casa com o binding mesmo se chegar antes da resposta HTTP
  (hoje essa corrida produziria uma mensagem `external_device` duplicada);
- `uncertain` (timeout ambíguo) passa a ter reconciliação: procurar eco/receipt pelo ID;
- retry com o mesmo `command_id` vira idempotente também no lado do provider.

Sem essa capability (outros providers), o worker de inbox deve **adiar** eventos
`from_me` com ID desconhecido enquanto houver comando `processing`/`uncertain` na mesma
conversa.

### P1-2. `CommandDTO` não carrega endereços

O plano diz que "o adapter escolhe PN/LID", mas o `CommandDTO` só tem
`conversation_ref`. Ou o adapter consulta modelos do core (acoplamento proibido pelo
próprio plano) ou o DTO carrega `conversation.addresses[]` + snapshot protocolar do
`reply_to`. Incluir no DTO.

### P1-3. Retenção e LGPD ausentes

Inbox guarda payload bruto obrigatório (PII, corpo, talvez mídia), sem política de
expurgo. Era guardrail da decisão de 2026-08-04 ("retenção/expurgo desde o dia 1, crons
LGPD próprios") e sumiu. Definir por tabela: inbox `done` (ex.: 30 dias), `dead`
(retenção para operação), outbox, delivery events; limite de tamanho do payload; caminho
para pedido de titular (anonimizar identity/guest/aliases sem quebrar FK). Fase 0 deve
pelo menos reservar os campos e o cron.

### P1-4. Mídia no webhook da WuzAPI

Para honrar "webhook não transporta binário", a conexão precisa estar em modo `s3` (ou o
adapter deve remover/descarregar o base64 **antes** de persistir o payload bruto).
Registrar como requisito de configuração da conexão + teste de fixture com base64
presente.

### P1-5. Controle de acesso: membership é o gate real

Verificado: regra nativa de `mail.channel` para tipo ≠ `channel` = `is_member`;
`mail.message` lê se lê o canal; bus também é por membership; `base.group_system` vê
tudo. Implicações:

- uma record rule "por equipe" adicionada a um grupo é **OR** com a nativa — só amplia;
  para restringir abaixo da membership seria preciso regra global, o que briga com
  bus/Discuss;
- portanto isolamento por equipe = **sincronização de membership** (atribuir/transferir
  ⇒ adicionar/remover membros partner) — o plano não descreve esse ciclo de vida;
- supervisor pode ser uma regra de grupo OR (ampliação intencional);
- o `message_unread_counter` de quem sai do canal e a visibilidade histórica após
  transferência precisam de decisão explícita.

### P1-6. Subtype, data e notas internas

- `message_post` sem `subtype_id` usa `mail.mt_note` (interno) —
  `mail_thread.py:2086-2087`. Mensagens externas devem usar
  `subtype_xmlid='mail.mt_comment'` explicitamente (guardrail antigo que também sumiu).
- Passar `date=occurred_at` para preservar o timestamp do provider (necessário para
  reconciliação fora de ordem na Fase 3).
- Definir política de **nota interna** na conversa: `mt_note` sem binding = nunca
  enviada. Isso dá uso legítimo ao `message_post` genérico e fecha o buraco de "mensagem
  órfã" do P0-1.

### P1-7. Anti-ban e tempestade de retry

Outbox sem limite de taxa por conexão; `connection.updated`
(Disconnected/LoggedOut/StreamReplaced) deveria **pausar** a fila em vez de queimar
tentativas de todos os comandos. Backoff exponencial com teto e jitter; throttle por
conexão com parâmetros na conexão.

### P1-8. Teste de concorrência não cabe em `TransactionCase`

"Dois webhooks concorrentes convergem por constraint e retry" exige dois cursores
(`registry.cursor()`) ou ao menos teste do caminho
`IntegrityError → savepoint → reload`. O worker deve processar cada linha em
savepoint/commit próprio para uma falha não derrubar o lote.

## P2 — melhorias e registro de decisão

- **Supersessão explícita.** O runbook
  `infra/runbooks/odoo-omnichannel-cockpit-architecture-2026-08-04.md` ainda diz "não
  usar `mail.channel`/`discuss.channel` como core" e descreve sidecar
  `engagement.message`; a decisão posterior (C-refinada, 2026-08-04 à noite) e este
  plano adotam `mail.channel` canônico + bindings. Adicionar ao plano uma linha
  "substitui o runbook X" e marcar o runbook como superado. O mesmo para OCA
  `mail_gateway` 16 (antes cogitado): registrar em "Não usar" com os motivos já
  conhecidos (envio síncrono no `message_post`, sem ID externo persistido,
  `ir_websocket` inscrevendo todo gateway_user em todos os canais).
- **Estratégia 16→18.** Odoo 16 está em fim de vida (3 anos desde out/2022). A decisão
  anterior chamou o 16 de "andaime descartável" e o 18 de destino; o roadmap "depois do
  piloto" lê como longo prazo no 16. Explicitar. Mitigações baratas: nomes neutros nos
  bindings (`channel_id`, não `mail_channel_id`), não expor `mail.channel.member` na
  UiDTO, nota de porte listando `mail.channel→discuss.channel`,
  `mail.channel.member→discuss.channel.member`, `message_format→Store`.
- **Receipts fora de ordem na Fase 1.** `delivery.updated` para mensagem ainda
  desconhecida deve ir para `retry` com TTL, não `dead`.
- **`mail.channel.name` é obrigatório** (`mail_channel.py:48`): definir regra de nome e
  se acompanha mudança de `display_name` da identity.
- **Proteger `mail.guest`** de `unlink` enquanto houver identity (o `restrict` cobre FK;
  um override explícito dá mensagem melhor).
- **Métricas mínimas na Fase 1** (contagem por estado, idade do `pending` mais antigo)
  em vez de só na Fase 4 — custo baixo, valor alto no piloto.
- **Aceites sem números**: adicionar latência, vazão e duração/tamanho do piloto.
- **Homolog**: o websocket do `odoo16-teste` estava quebrado (registro de 2026-08-04) —
  consertar antes da Fase 2.
- **Inconsistência de escopo**: research fala em `company_id` como escopo da identity;
  plano usa `account_id` nos aliases e "empresa" na identity. Está coerente (identity
  por empresa, alias por conta), mas vale uma constraint
  `identity.company_id == alias.account_id.company_id`.

## O que está bom e deve ser preservado

- Separação provider/core/UI e a proibição de HTTP síncrono no `message_post`.
- Guest-first com `message_post()` nativo em vez de `mail.message.create()`.
- Conflito de identidade explícito, sem merge automático; promoção como vínculo.
- Política `from_me` (eco × outro dispositivo) e `unsupported` para grupos.
- Pesquisa com commits pinados e lista de testes de contrato.
- `channel_type` próprio reservando `whatsapp` para o módulo oficial.
