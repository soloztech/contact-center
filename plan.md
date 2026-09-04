# Contact Center — plano

> Contact center multi-provider e multicanal para Odoo 16. Primeiro adapter: WuzAPI para
> WhatsApp.

## Objetivo

Centralizar conversas externas no Odoo sem acoplar o domínio ao provider nem à
plataforma. O mesmo core deverá atender WhatsApp, Instagram, Messenger, Telegram e
outros canais.

O escopo atual possui cinco addons:

- `contact_center_base`
- `contact_center_wuzapi`
- `contact_center_meta`
- `contact_center_crm`
- `contact_center_ui`

WAHA e Evolution entrarão depois como novos adapters do mesmo contrato.

## Referências de pesquisa

- [Identidade externa e `mail.guest`](research/odoo-mail-guest-identity.md)
- [Identificadores WhatsApp: JID, PN e LID](research/whatsapp-jid-lid-identifiers.md)
- [Transporte de mídia WuzAPI](research/wuzapi-media-transport.md)
- [Lifecycle e health da sessão WuzAPI](research/wuzapi-session-lifecycle.md)
- [Cobertura de eventos WuzAPI](research/wuzapi-event-coverage.md)
- [Metadata de grupos WuzAPI](research/wuzapi-group-metadata.md)
- [Atribuição Meta Click-to-WhatsApp](research/meta-click-to-whatsapp-attribution.md)
- [Mensagens Meta: Messenger e Instagram](research/meta-messenger-instagram.md)
- [Acesso da caixa: dono individual e equipe](research/inbox-owner-team-access.md)
- [Pipeline de atendimento no core](research/service-pipeline-core.md)
- [Disposição native-first, roadmap e UX de 2026-09-02](reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md)
- [OCA: contato com posições em várias empresas parceiras](https://github.com/OCA/partner-contact/tree/16.0/partner_contact_in_several_companies)

Esses documentos registram evidências e restrições para a implementação. As decisões
normativas permanecem neste plano.

Onde a pesquisa usa o nome legado `provider_account_id`, a implementação deve ler
`account_id` da conta lógica. A conexão descartável do provider é
`provider_connection_id` e nunca define o escopo canônico de identidade ou conversa.

## Decisão vigente e portabilidade

Este plano substitui, quanto ao core de conversa, o runbook
`infra/runbooks/odoo-omnichannel-cockpit-architecture-2026-08-04.md`: a decisão vigente
usa `mail.channel` como conversa canônica, sem `engagement.conversation` paralelo.

O OCA `mail_gateway` e o Discuss Hub permanecem referências, não dependências. Seus
fluxos acoplados ao Discuss, ao websocket global e ao envio por `message_post()` não
atendem à UI própria nem à outbox assíncrona deste projeto.

O Odoo 16 é a primeira implementação. DTOs, UiDTO e nomes dos campos próprios devem
permanecer neutros (`channel_id`, `message_id`) e não expor `mail.channel.member`. O
porte futuro troca internamente `mail.channel` por `discuss.channel` e seus membros, sem
alterar a API da UI.

## Arquitetura

```text
Inbound

Provider webhook
    → contact_center_wuzapi (autentica e persiste o envelope sem binário inline)
    → commit e resposta 2xx
    → OCA queue_job / canal contact_center.inbox
    → adapter normaliza para EventDTO v1
    → AttributionDTO[] → ledger imutável de evidência operacional (antes da projeção)
    → identidade/guest → mail.channel → mail.message + bindings
    → vínculo opcional do touchpoint à mensagem/conversa resolvida
    → após commit: bridge opcional pode traduzir a evidência para o Marketing Center
    → se houver mídia: OCA queue_job / canal contact_center.media
    → adapter baixa e valida o binário → ir.attachment privado

Outbound

Upload opcional autenticado → anexo temporário + referência opaca
Composer do contact_center_ui
    → API de aplicação do contact_center_base
    → mail.message + binding + mídia + outbox na mesma transação
    → commit
    → OCA queue_job / canal contact_center.outbox
    → CommandDTO v1
    → contact_center_wuzapi
    → Provider API

Health e recuperação

Cron de 1 minuto (somente agenda)
    → OCA queue_job / canal contact_center.health / prioridade 30
    → jitter determinístico de 0–29 segundos por conexão
    → adapter consulta o status do provider
    → estado canônico + evento de bus próprio
    → observação com mais de 180 s vira unknown e bloqueia dispatch
    → mismatch da identidade própria arma um latch durável e bloqueia dispatch
    → ao reconectar com identidade comprovada: retoma somente outbox pré-boundary elegível
```

Regras:

- O provider apenas valida, traduz e transporta.
- O core nunca conhece payloads, endpoints ou campos específicos do provider.
- Provider e plataforma são dimensões distintas da conta: por exemplo, WuzAPI/WhatsApp
  ou Meta/Instagram.
- O DTO contém apenas conceitos comuns; diferenças são declaradas por capabilities e
  extensões com namespace.
- O provider nunca cria conversa, identidade ou `mail.message` diretamente.
- `mail.channel` e `mail.message` são os registros canônicos de conversa e mensagem.
- Todo participante remoto nasce como `mail.guest`; `res.partner` nunca é criado
  automaticamente. Um evento direto `from_me` sem conversa prévia pode criar o guest
  remoto exclusivamente pelos endereços da conversa; o ator do evento representa a
  própria conta e nunca fornece o nome dessa pessoa. Em grupos, uma mensagem humana
  `from_me`, não correlacionada a uma saída local e observada no dispositivo externo,
  pode iniciar a conversa quando o opt-in de recebimento estiver ativo. Essa projeção
  nunca cria guest/identity para o próprio número e agenda o roster de forma assíncrona.
- `contact.center.identity` resolve o guest por aliases externos e pode ser vinculado
  posteriormente a um `res.partner`.
- Um `whatsapp.pn` fornecido com evidência protocolar é portável no escopo da empresa: a
  mesma pessoa reutiliza uma única identity/guest em várias caixas, mas cada conta
  mantém seu próprio canal direto, equipe, responsável e estado operacional.
  `whatsapp.lid`, JIDs opacos, PSID e IGSID permanecem restritos à conta que os
  observou; empresas diferentes nunca compartilham identity ou guest.
- Toda conversa usa `mail.channel.channel_type = 'contact_center'`. Plataforma e
  provider nunca entram em `channel_type`; `channel_type = 'whatsapp'` e
  `message_type = 'whatsapp_message'` ficam reservados para eventual convivência futura
  com o módulo oficial do Odoo.
- Mensagens humanas usam `mail.message.message_type = 'comment'`; direção, plataforma,
  provider e tipo de conteúdo ficam nos bindings.
- Somente a API explícita de envio do core cria comandos outbound. Eventos inbound e
  chamadas genéricas a `message_post()` nunca podem retornar à outbox.
- Inbox e outbox permanecem ledgers de domínio persistentes; OCA `queue_job` executa,
  serializa concorrência e agenda retries.
- A autenticação HTTP e a extração de atribuição pertencem ao addon do provider. O base
  recebe somente `AttributionDTO`, persiste evidência append-only com dedupe e
  enriquecimento monotônico e, por opt-in da conta, expõe uma projeção operacional
  limitada. IDs externos, URLs e extensões do provider não entram no UiDTO ou bus.
- Binários não entram em DTO JSON, inbox/outbox JSON, bus ou logs. Mídia inbound é
  baixada depois do commit; upload outbound é consumido atomicamente pelo envio.
- Reply referencia o binding externo. Reaction, edit e delete usam um ledger próprio;
  projeções outbound só são aplicadas depois do sucesso confirmado pelo provider.
  Edit/reaction são monotônicos por `occurred_at` + ID local, reaction possui uma lane
  por ator e delete é terminal. Eco `from_me` representa o lado próprio pelo autor
  técnico da conta, nunca pelo autor da mensagem-alvo.
- Mutações de grupo usam participantes protocolares estruturados. Eventos exigem
  `mutation.target_from_me` como evidência do provider; em grupos esse referencial pode
  ser relativo ao ator e nunca substitui a direção do binding correlacionado. O
  `mutation.target_protocol_participant` é evidência opcional: quando presente precisa
  resolver ao mesmo participante persistido no alvo. Comandos outbound de reaction, edit
  e delete carregam `own_protocol_participant`, `target_protocol_participant` e
  `options.target_from_me`.
- Reaction de grupo pode atingir mensagem remota ou própria. Edit inbound continua
  exclusivo do autor; delete inbound aceita o autor ou participante ativo comprovado
  como `admin`/`superadmin`, inclusive para projetar tombstone de mensagem alheia.
  Comandos outbound de edit/delete continuam restritos a mensagem própria. Ator,
  direção, conexão, profile e equivalência PN/LID são revalidados contra o roster.
- Receipts de grupo são persistidos por mensagem, participante e milestone num ledger
  próprio. Eles nunca promovem o estado agregado da mensagem para delivered/read; a UI
  recebe somente contagens distintas de entregues e leitores.
- Chamadas externas nunca acontecem dentro da transação da interface.
- O cron de health nunca faz I/O de provider: ele apenas agenda um job OCA único por
  conexão ativa. Refresh manual segue a mesma fila; não chama o provider na request.
- Webhook de conexão ou conta arquivada ainda precisa passar por autenticação e
  validação básica; se válido, recebe acknowledge `200/ignored=inactive` sem criar
  Inbox, ledger ou job. Assinatura inválida continua sendo rejeitada.
- `Retry-After` de health persiste o limite em `health_retry_not_before`; cron e refresh
  manual respeitam o mesmo cooldown e não enfileiram novo probe antes do prazo.
- O estado persistido é provider-neutral. A UI deriva `unknown` quando não há observação
  nos últimos 180 segundos e trata `checking` como indicador transversal, sem esconder o
  último estado. O dispatch usa exatamente o mesmo teste de frescor; `unknown` nunca
  cruza o boundary do provider. Um latch de identidade não mascara observações frescas
  `disconnected` ou `authentication_required` na frota; para estado conectado ou stale,
  permanece `degraded`, e sempre bloqueia outbound até prova positiva.
- Health e lifecycle observam a sessão, mas nunca chamam connect, login, geração de QR
  ou outra ação de autenticação automaticamente.
- Dispatch exige conta e conexão ativas, outbound ativo, estado `connected`, observação
  fresca e `identity_mismatch_latched = False`; arquivar a conta é um gate final antes
  do provider. Um mismatch ou identidade não comprovada observados por health armam esse
  latch durável; lifecycle, quedas intermediárias e health sem prova explícita de
  identidade não o limpam. Somente health `connected` com `identity_matches = True`
  libera o dispatch. Ao tornar a conexão novamente disponível, inclusive quando o estado
  persistido já era `connected` mas estava stale, apenas comandos `pending`/`retry`
  comprovadamente anteriores ao boundary podem ser retomados; `processing`, `uncertain`,
  `dead`, `done` e `cancelled` nunca são reenviados automaticamente.
- A ordenação da caixa usa chegada local (`mail.message.create_date` + ID local), nunca
  o relógio do provider; `occurred_at` permanece apenas como horário exibido.
- Cada envio da UI possui um UUID estável. Repetir o mesmo request na mesma conversa
  retorna a mensagem/outbox original; mudar o conteúdo com o mesmo UUID é rejeitado.
- A assinatura do operador é uma política opcional por conta/caixa. O core persiste no
  comando somente `{display_name}` como snapshot semântico, mantém `mail.message` e o
  ledger com o texto limpo, e cada adapter capaz renderiza o prefixo da plataforma. Na
  WuzAPI o wire text é `*Nome:*\nMensagem`; replay do mesmo UUID reutiliza o snapshot
  mesmo após toggle da política ou renome do operador.
- Receber mensagens de grupo e enviar para grupos são opt-ins independentes da conta.
  Novas contas nascem com ambos desligados; a migração mantém o comportamento inbound
  das contas já existentes. Metadata, receipts, mutações e ecos próprios continuam
  processáveis mesmo quando novas mensagens de participantes estão desabilitadas.
- A lista de eventos webhook desejados pertence à conexão do provider. O addon WuzAPI
  publica o catálogo versionado, e os botões de aplicar/verificar apenas agendam jobs
  OCA; PUT/GET, read-back, drift, retry e fencing de revisão acontecem fora da request.
- O nome manual do perfil convidado pode ser alterado pelo operador autorizado sem criar
  contato, trocar o `mail.guest` ou perder aliases PN/LID e autoria histórica.
- Gravação de áudio é capability distinta de anexar áudio. O provider declara formatos
  graváveis, formatos de voice note e duração máxima; a UI intersecta essa política com
  `MediaRecorder`, o core valida o contrato e o adapter aplica regras de container/PTT.
  A WuzAPI aceita OGG/Opus como voice note e MP4 audio-only como áudio comum.
- Capabilities no topo continuam descrevendo conversas diretas por compatibilidade.
  Qualquer outro tipo exige opt-in explícito em `conversation_types.<tipo>`; ausência ou
  payload malformado falha fechado. Envio para grupos também exige opt-in da conta,
  desligado por padrão, e usa exclusivamente a conexão que observa o profile do grupo.
- Um envio de grupo exige `client_message_id` pré-atribuído. O ledger, o campo superior
  do `CommandDTO` e o campo da mensagem devem coincidir novamente no boundary do job.
  Eco `from_me` de grupo só pode reconciliar dentro de uma conversa já resolvida. Se a
  conversa ainda não existir, a busca global é apenas uma guarda negativa: qualquer ID
  já pertencente a uma saída local de outra conversa bloqueia o bootstrap, enquanto uma
  mensagem humana não correlacionada do dispositivo pode criar o grupo.
- O processamento é `at-least-once`; provider externo pode não ser idempotente e o
  sistema não promete `exactly-once`.
- A interface própria não importa nem altera componentes, Store ou modelos JavaScript
  privados de Discuss ou `im_livechat`.
- Eventos nativos do Discuss que abrem chat ou atualizam Store não são emitidos para
  canais `contact_center`; a UI recebe apenas eventos de bus próprios.
- Refresh da timeline faz merge incremental e preserva histórico/cursor já carregados.
  Lacunas de reconexão acima da janela do backend usam páginas forward limitadas a 100
  registros e teto agregado de 20 páginas automáticas a partir de um high-water mark
  revisionado. Backlogs maiores são recentralizados em um head fresco; o histórico
  anterior continua disponível pela paginação backward.

## Addons

### `contact_center_base`

- Contrato DTO e registry de adapters.
- Conta lógica de plataforma, conexões de provider e capabilities efetivas.
- Ledgers de inbox/outbox e processamento assíncrono por OCA `queue_job`.
- Identidades, aliases de participantes e conversas, bindings e estados de entrega.
- Criação de guest, resolução de identidade e promoção opcional para contato.
- Extensões de `mail.channel` e `mail.message`, sem duplicar conversa ou mensagem.
- API de aplicação explícita para postar inbound e criar outbound.
- Timeline e replies sobre os modelos nativos de `mail`; mídia em bindings/uploads
  privados e reaction/edit/delete em ledger de mutações com projeção nativa.
- Equipes, responsável, tags e estados do atendimento.
- Pipelines, etapas e casos de atendimento independentes do estado operacional da
  conversa. Uma conversa possui um caso padrão e pode possuir outros casos para
  assuntos/propostas distintos.
- Saúde provider-neutral da conexão, scheduler que apenas enfileira jobs OCA, métricas,
  ordenação durável de observações e retomada segura da outbox na reconexão.
- Views administrativas. O menu `Técnico`, exclusivo de administradores do sistema,
  separa atalhos para os modelos nativos relacionados (`mail.message`, mensagens
  programadas, guests, reactions, ratings, previews, notificações, seguidores, subtipos,
  tracking e atividades) dos ledgers próprios da Central. Os atalhos reutilizam as
  actions nativas; não duplicam modelos, actions nem views do Odoo.
- Testes comuns que todo adapter deve cumprir.

### `contact_center_wuzapi`

- Configuração e credenciais específicas da WuzAPI.
- Controller e autenticação do webhook.
- Mapper payload WuzAPI → `EventDTO`.
- Mapper `CommandDTO` → API WuzAPI.
- Cliente HTTP, mídia inbound/outbound, reply, reaction, edit, delete e leitura de
  status; QR/login/connect permanecem ações humanas explícitas.
- Cobertura versionada de `Message` para texto, imagem, áudio, vídeo, documento,
  sticker, localização/live location, contato, template, poll e interactive; conteúdo
  humano desconhecido recebe uma projeção segura, sem expor o payload.
- `CallOffer`, `CallAccept` e `CallTerminate` viram cards imutáveis de chamada;
  `IdentityChange` vira card imutável de segurança. Os cards são eventos de controle,
  não mensagens outbound nem gatilhos de ação automática.
- `ReadReceipt` alimenta o ledger de delivery. O comando provider-neutral opcional
  `mark_read`, habilitado por política da conta e capability, agrupa até 100 IDs de
  mensagens diretas e usa `/chat/markread` fora da request da UI.
- Eventos selecionados sem projeção de produto continuam autenticados e sanitizados no
  Inbox como `unsupported`; o contador das últimas 24 horas aparece na frota e o ledger
  administrativo conserva o histórico. Presença, history sync, blocklist, recuperação
  criptográfica e newsletters permanecem deliberadamente sem projeção.
- Rotação de HMAC ocorre somente pela ação coordenada e pelo `queue_job`: o addon gera
  segredo pendente forte, configura `/session/hmac/config`, exige ACK estrito e promove
  a chave por revisão fenced. Current/pending/previous são aceitos durante a transição;
  a chave anterior possui janela não renovável de cinco minutos e é apagada por job.
  Segredos não entram em argumentos/descrições de job, DTO, bus ou logs, e edição direta
  da chave estabelecida é bloqueada.
- Normalização dos eventos exatos de lifecycle `Connected`, `KeepAliveRestored`,
  `Disconnected`, `ConnectFailure`, `StreamReplaced`, `KeepAliveTimeout`, `StreamError`,
  `LoggedOut`, `QRTimeout`, `ClientOutdated` e `TemporaryBan` para `connection.updated`.
  `Connected` e `KeepAliveRestored` são somente hints com
  `health_confirmation_required`: o core fica `degraded` e agenda `/session/status`;
  lifecycle isolado nunca reabre outbound.
- Validação do JID retornado por `/session/status` contra a identidade própria
  configurada na conta, após remover o sufixo de device; divergência vira o detalhe
  seguro `identity_mismatch`, e sessão conectada sem JID vira `identity_unverified`, sem
  publicar identificador bruto. WuzAPI sem identidade própria configurada também fica
  `identity_unverified`; somente identidade configurada e JID coincidente podem produzir
  health `connected`. Resultados negativos armam o latch provider-neutral.
- URL do serviço, token da API e identidade própria não podem mudar enquanto outbound
  estiver ativo nem houver comando `processing`. Depois de desativar outbound e drenar o
  in-flight, a mudança é aceita como reconfiguração segura: fica
  `degraded/identity_unverified`, arma o latch, incrementa
  `health_configuration_revision` e agenda nova verificação.
- Fixtures anonimizadas e testes do adapter.
- Contrato e fixtures fixados na versão implantada WuzAPI `v1.0.8`, commit `9487eca`;
  fatos observados em `main` não entram sem revalidação.

Configuração específica da WuzAPI não entra no core.

### `contact_center_meta` — arquitetura greenfield vigente

- Addon independente, dependente de `contact_center_base`, `meta_api_base` e
  `meta_webhook_base`, com `adapter_key = 'meta'`.
- Plataformas canônicas: `messenger` e `instagram`. Provider e plataforma permanecem
  dimensões separadas; `mail.channel.channel_type` continua `contact_center`.
- Uma `contact.center.account` representa cada caixa lógica. Messenger/Page e Instagram
  são contas distintas mesmo quando pertencem à mesma Page e ao mesmo App.
- `meta.api.app` é a única fonte do App ID, versão Graph e referência do App Secret.
  `meta.webhook.endpoint/page/asset/subscription` são as únicas fontes de callback,
  verify token, Page token, Page/Instagram assets, subscriptions e read-back.
- A `contact.center.provider.connection` armazena apenas `meta_webhook_asset_id`. Page,
  App, modo de transporte e ID de destino são projeções somente leitura desse asset. Um
  asset pode ter apenas uma conexão primária ativa e o vínculo é imutável.
- Não existem App, autorização, callback, delivery ledger, health ou segredo Meta
  próprios no `contact_center_meta`. Não existem caminhos alternativos de ingresso,
  fallback ou dupla escrita.
- `meta_webhook_base` permanece um componente técnico compartilhado e não publica
  aplicativo no launcher. O menu raiz legado fica inativo de forma compatível com
  upgrades; o `contact_center_meta` expõe apenas proxies administrativos para endpoints,
  páginas/ativos e entregas dentro de `Técnico -> Registros da Central`.
- O único callback autentica os bytes exatos e o único ledger técnico imutável usa
  `meta.webhook.delivery/item/dispatch`. O consumidor `contact_center.meta` reivindica
  apenas seus campos e projeta o item atômico em
  `Inbox Event -> EventDTO -> mail.channel/mail.message`.
- O runtime de App/Page é resolvido somente em memória por métodos privados do Odoo e
  capabilities process-local não serializáveis. RPC não recebe segredo ou Page token.
- URLs assinadas de mídia ficam somente em `contact.center.meta.media.locator`, sem ACL,
  acessado por `sudo()` mais capability interna. Ledger, DTO, UiDTO, bus, snapshot e log
  recebem somente referência opaca.
- PSID e IGSID são namespaces distintos e permanecem escopados à conta. Não há vínculo
  automático entre ativos ou plataformas.
- Texto direto inbound/outbound, reply, echo, referral/`AttributionDTO`, mídia inbound,
  receipts, reactions, edit/unsend, postback e perfil assíncrono passam pelos contratos
  provider-neutral já existentes. Outbound de texto respeita a janela de 24 horas e
  timeout ambíguo nunca é reenviado cegamente.
- `meta_api_base` e `meta_webhook_base` são infraestrutura compartilhada, sem depender
  de Contact Center ou Marketing Center. O ledger técnico não cria guest, conversa,
  mensagem ou touchpoint; cada produto consome seus itens pelo próprio addon.

### `contact_center_crm`

- Addon ponte opcional; `contact_center_base` não depende de `crm`.
- Vincula explicitamente, em relação 1:1, a equipe de atendimento à equipe comercial.
  Enquanto o vínculo estiver ativo, o roster do CRM é a autoridade: líder comercial é
  projetado como supervisor e membros ativos como agentes da equipe de atendimento. Essa
  projeção usa as permissões nativas já existentes no Contact Center; não cria uma
  segunda ACL paralela.
- Papéis necessários de Contact Center são concedidos de forma aditiva. Remover alguém
  do roster CRM revoga imediatamente seu acesso às caixas da equipe, mas não remove
  automaticamente grupos que também possam ser necessários em outra equipe.
- Desativar ou remover o vínculo congela o último roster e devolve a equipe de
  atendimento ao modo nativo/manual. Caixas com `owner_user_id` preservam o grant direto
  do dono independentemente do roster espelhado.
- Vincula um pipeline do Contact Center a uma `crm.team`. Enquanto o vínculo estiver
  ativo, o catálogo efetivo de `crm.stage` da equipe comercial é a fonte canônica das
  etapas desse pipeline.
- Vincula `contact.center.case` a `crm.lead`. Cada caso aponta para no máximo um lead,
  mas uma conversa possui vários casos e um lead pode receber casos de várias conversas.
  Portanto, conversa e lead formam uma relação N:N explícita através dos casos, sem uma
  M2M opaca no `mail.channel`.
- “Transformar em lead” cria um `crm.lead` e seu vínculo; “mesclar com lead” significa
  vincular um lead existente. Nunca executa o merge destrutivo nativo do CRM.
- Mudanças de etapa convergem nos dois sentidos com revisão/idempotência, sem duplicar
  atividades: atividades, probabilidade e demais dados comerciais permanecem no lead.
- Ao criar ou enriquecer um lead, esta ponte escreve apenas dados comerciais não-UTM,
  como telefone, e-mail, descrição e vendedor autorizado. Ela não escreve `campaign_id`,
  `source_id` ou `medium_id`.
- A proposta de atribuição da conversa converge pelo bridge opcional
  `marketing_center_contact_center_crm`; somente `marketing_center_crm` pode aplicar a
  tupla UTM nativa, por `first_trusted_assignment` atômico, fill-only e auditável. Essa
  escrita continua bloqueada até shadow e go/no-go do plano do Marketing Center.
- Uma futura ponte de Helpdesk seguirá o mesmo padrão tipado para equipe, pipeline,
  etapa e caso/ticket, sem tornar o core dependente de Helpdesk.

### Configuração extensível de providers

- Existe um único menu e formulário genérico: `Provider Connections`.
- `adapter_key` é um selector alimentado pelo registry dos addons instalados.
- O formulário base fornece um notebook estável; cada addon injeta sua própria aba,
  visível somente quando seu provider é selecionado.
- `contact_center_wuzapi` estende `contact.center.provider.connection` com campos
  prefixados `wuzapi_*` e injeta a aba `WuzAPI`. Não existe menu, action nem modelo
  `contact.center.wuzapi.config` separado.
- Um futuro `contact_center_waha` seguirá o mesmo contrato e injetará sua aba/campos sem
  alterar o `contact_center_base`.

### Topologia de conta e conexões

- `contact.center.account` é a caixa lógica durável: possui plataforma, dono individual
  opcional, equipe com acesso opcional, políticas, pipeline padrão e conversas
  canônicas. Não representa credencial, container ou sessão do provider.
- `contact.center.provider.connection` é um transporte substituível dessa caixa. Uma
  conta pode conservar várias conexões para operação, migração e auditoria sem duplicar
  suas conversas.
- `primary` é o único papel que pode possuir ingress ativo; é também o único que pode
  despachar quando `outbound_active` estiver habilitado. Existe no máximo um primary
  ativo por conta.
- `standby` permanece configurado e monitorado, sem tráfego; `migration` identifica um
  candidato preparado para troca controlada; `historical` arquiva a conexão, desliga
  inbound/outbound e preserva os vínculos externos existentes.
- O contrato greenfield é explícito e fail-closed: `create()` sem `role` sempre resulta
  em `standby` sem ingress/egress e flags antigos nunca promovem silenciosamente o
  primeiro provider. A criação programática de `primary` declara obrigatoriamente
  `active`, `role`, `inbound_active` e `outbound_active`; conexões existentes só são
  promovidas por `action_use_as_primary()`. O toggle Odoo de arquivar/desarquivar
  continua normalizando `historical ↔ standby`, sempre sem tráfego ao restaurar.
- Não há round-robin, balanceamento ou fallback automático entre conexões. A troca de
  primary é uma operação administrativa atômica, serializada por conta, e exige drenar
  ou resolver comandos outbound ativos antes de retirar o transporte anterior.
- A admissão de novo comando e a troca de primary compartilham locks canônicos e uma
  revisão monotônica, evitando que uma transação `REPEATABLE READ` grave comando no
  provider antigo depois do cutover.
- IDs externos são escopados pela conexão que os observou. Uma nova mensagem simples usa
  o primary atual; reply, edit, reaction e delete continuam presos ao provider do
  binding alvo e falham fechado depois do cutover. Um replay idempotente exato devolve
  seu ledger original, mas nunca cria um novo envio cruzando providers.

### `contact_center_ui`

- Client action Owl e menu próprios para o atendimento.
- Lista de conversas, timeline, composer, filtros, badges e ações operacionais.
- API/UiDTO local e versionada, fornecida pelo `contact_center_base`.
- Atualização em tempo real por eventos de bus próprios e estáveis.
- Nenhuma dependência de `im_livechat` nem patch/import de componentes privados do
  Discuss; o reaproveitamento ocorre nos modelos Python `mail.channel`, `mail.message`,
  `mail.channel.member`, `mail.guest` e `ir.attachment`.

Um futuro `contact_center_discuss_bridge` ou `contact_center_odoo_whatsapp` será
opcional e não fará parte do MVP.

## DTO v1

### `EventDTO`

- `schema_version`
- `provider_schema_version`, `event_id` e `event_type`
- `occurred_at` em UTC
- `account_ref`, `connection_ref` e `conversation_ref`
- `platform` e tipo da conversa
- `direction`, `is_from_me` e origem
- `actor.addresses[]` e `conversation.addresses[]`, sempre separados
- `message`, `reply_to` e referências de mídia
- para mutações de grupo, `mutation.target_from_me` como hint do provider e, quando
  observado, `mutation.target_protocol_participant`; o binding persistido do alvo é a
  fonte canônica de direção e participante
- `delivery`
- `extensions`

Cada endereço informa `namespace`, valor bruto e normalizado, `role`, `source_field` e
`confidence`. Um endereço de conversa, grupo, destinatário alternativo ou
`DeviceSentMeta` nunca é inferido como alias do remetente.

Eventos iniciais: `message.created`, `message.updated`, `message.deleted`,
`message.reaction`, `delivery.updated` e `connection.updated`.

`MediaDTO` é provider-neutral e carrega tipo, MIME, nome, tamanho, hash, dimensões,
duração, indicação de voice note e locator opaco. `capabilities.media` publica, por
tipo, se está habilitado, `max_bytes` e a allow-list opcional de `mimetypes`; o core
revalida essas regras no upload, no consumo e antes do dispatch.

### `CommandDTO`

- `schema_version`
- `command_id` — chave de idempotência estável
- `client_message_id` opcional — correlação pré-atribuída quando suportada
- `command_type`
- `account_ref`, `connection_ref` e `conversation_ref`
- `conversation.addresses[]` e `target_address`
- `message`, `reply_to`, snapshot protocolar e opções
- em mutações de grupo, `own_protocol_participant`, `target_protocol_participant` e
  `options.target_from_me`

Operações iniciais: `send_message`, `mark_read`, `react`, `edit_message` e
`delete_message`. A interface habilita cada operação conforme as capabilities do
adapter.

Capabilities são declaradas pela conexão/adapter e plataforma; a conta expõe o conjunto
efetivo da conexão ativa. `extensions` são opcionais, possuem namespace, como
`platform.telegram` ou `provider.wuzapi`, e nunca são necessárias para processar o fluxo
comum.

`client_message_id` significa correlação, não idempotência garantida pelo provider. No
WuzAPI, o mesmo ID deve ser preservado, mas um retry ainda chama `SendMessage`
novamente; timeout ambíguo continua em `uncertain` até haver reconciliação.

O contrato mínimo de adapter possui operações para autenticar webhook, normalizar o
envelope sanitizado persistido, executar comando, informar capabilities e consultar
health. Retornos e erros são classificados como sucesso, permanente, transitório ou
timeout ambíguo.

### `UiDTO/API v1`

A UI consome somente uma API local do Contact Center para listar conversas, carregar a
timeline, marcar `fetched/seen`, enviar mensagens e executar ações. Essa API não expõe
payload do provider, membership nativa nem o Store privado do Discuss e constitui a
fronteira de portabilidade entre Odoo 16 e versões futuras.

A API v1 expõe `bootstrap`, `list_conversations`, `get_conversation`, `get_timeline`,
`mark_fetched`, `mark_seen`, `update_conversation`, `claim_conversation`,
`check_connection_health`, busca/vínculo/criação/desvínculo de contato, `send_message`,
`react_message`, `edit_message` e `delete_message`. `send_message` aceita texto e/ou
`media_refs`. Uploads usam a rota autenticada `/contact_center/media/upload`; conteúdo
usa `/contact_center/media/<id>/content`, protegida por ACL, record rule e membership.
Lista e timeline são paginadas por cursores locais estáveis. Mudanças de mensagem,
entrega, conversa e identidade chegam à UI por um evento de bus próprio
`contact_center/event`; ponteiros de leitura não provocam loop de ressincronização.

Mensagens outbound de grupo podem expor `group_delivery` somente com `delivered_count` e
`read_count`; delivered inclui participantes que já leram. Nenhum participante, PN, LID,
JID, total do roster ou payload do provider entra no UiDTO.

`bootstrap.connection_health` foi desenhado para uma frota de aproximadamente 20
números: publica resumo e itens compactos com conta, conexão, plataforma, provider,
endereço próprio exibível, estado, `checking`, diagnóstico seguro e datas. Agent pode
ver apenas a saúde das caixas do seu roster; Supervisor/Administrator pode solicitar
refresh, que apenas agenda os jobs. O evento incremental `connection_health_updated`
permite atualizar o item e recalcular o resumo localmente. Uma revisão monotônica no
cliente impede que a resposta atrasada do RPC de refresh sobrescreva um resultado mais
novo recebido pelo bus. Se o evento incremental for perdido, o cliente faz polling de
fallback limitado a 11 tentativas/180,5 s; `connectionHealthRevision` garante a
reatividade dessa convergência no Owl do Odoo 16, sem exigir reload da página.

Arquivamento de conta/conexão, mudança do dono individual, mudança da equipe de acesso e
alteração de roster publicam uma invalidação de escopo para os usuários atuais e
anteriormente autorizados. A UI refaz o bootstrap filtrado; nunca adiciona diretamente
um `connection_id` do bus que não existia no snapshot atual.

## Modelos do core

- `contact.center.account` — conta lógica e estável da plataforma, com empresa,
  plataforma, identidade própria, dono individual/equipe de acesso opcionais e pipeline
  padrão. Trocar WuzAPI por WAHA/Evolution não troca esta conta nem seus aliases.
- `contact.center.provider.connection` — conexão substituível com um adapter, conta,
  configuração, estado, health, capabilities e revisão monotônica da configuração de
  health. Somente uma conexão fica ativa para outbound por conta; conexões antigas
  permanecem para rastreabilidade e também podem ser monitoradas sem reativar comandos.
- `contact.center.identity` — interlocutor canônico no escopo da empresa, com
  `mail.guest` obrigatório e persistente e vínculo opcional a `res.partner`.
- `contact.center.identity.alias` — todos os IDs externos observados, por conta e
  namespace, valor bruto/normalizado, papel, origem, confiança e histórico.
- `contact.center.identity.conflict` — conflito explícito quando aliases apontam para
  identidades diferentes e a reconciliação portátil não pode provar um merge seguro.
- `mail.channel` — conversa canônica com `channel_type = 'contact_center'`, empresa,
  projeção do dono/equipe da caixa, responsável e estado. Não existe um segundo marcador
  de conversa persistido.
- `contact.center.pipeline` e `contact.center.pipeline.stage` — catálogo de fluxos e
  etapas de atendimento por empresa, neutro em relação a CRM/Helpdesk.
- `contact.center.case` — assunto operacional dentro de uma conversa, com pipeline,
  etapa, responsável, prioridade e revisão monotônica. Uma conversa pode conter vários
  casos; exatamente um é o caso padrão criado automaticamente.
- `contact.center.case.transition` — histórico imutável e idempotente de mudanças de
  etapa do caso.
- `contact.center.channel.binding` — vínculo lógico entre canal, conta e tipo de
  conversa. Em conversa direta, aponta para a identity remota; pode redirecionar para um
  binding sobrevivente após consolidação. No MVP há um único binding por canal,
  independentemente da conexão de provider usada.
- `contact.center.channel.alias` — todos os endereços externos observados para a mesma
  conversa, como evidência e roteamento, mantendo PN/LID de conversa separados dos
  aliases de pessoa.
- `mail.message` — mensagem canônica, com corpo, autor, anexos, reply e reactions.
- `contact.center.message.binding` — direção, origem, tipo de conteúdo, IDs externos e
  snapshot protocolar necessário para reply, reaction, edit e delete. Pode receber um
  `client_message_id` antes da chamada externa.
- `contact.center.media.binding` — metadados, locator restrito, estado do download, hash
  e vínculo com o `ir.attachment` privado da mensagem.
- `contact.center.media.upload` — upload outbound temporário, pertencente ao usuário e à
  conversa, consumido uma única vez na transação que cria mensagem e outbox.
- `contact.center.message.mutation` — ledger idempotente de reaction, edit e delete,
  incluindo direção, ator, estado e evidência do dispatch.
- `contact.center.delivery.event` — histórico de `queued/sent/delivered/read/failed`.
- `contact.center.group.delivery.event` — milestone `delivered/read` por mensagem
  outbound e participante técnico de grupo, convergente pela chave natural mensagem +
  participante + estado, com primeira ocorrência e última observação.
- `contact.center.attribution.touchpoint` — evidência operacional append-only observada
  no provider, deduplicada por conexão e chave canônica da mensagem/evento; pode ser
  enriquecida monotonicamente e ligada depois ao binding de mensagem, conversa e
  identity. Não substitui o ledger de marketing nem escreve campos `utm.*`.
- `contact.center.attribution.identifier` — identificadores externos exatos e namespaced
  do touchpoint, restritos a administradores e nunca expostos na UI do operador.
- `contact.center.inbox.event` — conexão, envelope sanitizado obrigatório, versão do
  provider, DTO normalizado, dedupe, estado de domínio e UUID do `queue.job`. Cabeçalhos
  de autenticação, segredos e conteúdo binário/base64 inline nunca são persistidos;
  blobs são omitidos ou externalizados com hash, tamanho e referência.
- `contact.center.outbox.command` — comando, idempotência, tentativas de domínio, estado
  de dispatch, evidência de incerteza e UUID do `queue.job`, ligado à conexão escolhida
  para o envio.

Constraints mínimas:

```text
UNIQUE(account_id, namespace, value_normalized)  # identity.alias
UNIQUE(mail_guest_id)                            # identity; required/ondelete restrict
UNIQUE(channel_id)                               # channel.binding no MVP
UNIQUE(account_id, identity_id, conversation_type) # somente conversa direta
UNIQUE(account_id, namespace, value_normalized)  # channel.alias
UNIQUE(channel_binding_id, external_message_id)
UNIQUE(message_id)                               # message.binding
UNIQUE(provider_connection_id, inbox_dedupe_key)
UNIQUE(account_id, outbox_idempotency_key)
UNIQUE(channel_binding_id, ui_request_id)        # idempotência do composer
UNIQUE(message_binding_id, participant_id, state) # receipt de grupo
UNIQUE(provider_connection_id, canonical_key)     # attribution touchpoint
```

Constraints ORM garantem que identity, aliases e bindings pertençam à mesma empresa da
conta. A unicidade de conversa direta é parcial e não se aplica a grupos ou plataformas
que possuam múltiplas threads com a mesma identidade.

Telefone digitado, nome ou semelhança textual não são chaves de identidade. IDs externos
são aliases por namespace. O `whatsapp.pn` protocolar é a exceção portável: resolve a
identity no escopo da empresa mesmo quando outra conta observou primeiro. Cada conta
ainda persiste seu próprio alias como evidência e preserva seu canal direto.
`whatsapp.lid`, `whatsapp.jid`, `messenger.psid`, `instagram.igsid` e equivalentes são
resolvidos por conta. Vincular outros namespaces entre contas ou plataformas exige
evidência forte e uma operação explícita.

## Canal, postagem e portabilidade

`contact_center_base` adiciona somente o valor semântico `contact_center` à seleção de
`mail.channel.channel_type`. A remoção do addon deve converter esses canais para
`group`, preservando canais e mensagens; nunca usar `cascade`. `is_contact_center`, se
necessário apenas para compatibilidade de API, será computado do tipo e nunca uma
segunda fonte de verdade.

`mail.channel.name` recebe no create um fallback determinístico baseado na identity; a
UI exibe o nome da identity ou do contato vinculado. Atualizações automáticas de
`pushName` não sobrescrevem um nome alterado explicitamente pelo agente.

Plataforma (`whatsapp`, `telegram`, `instagram`) e provider (`wuzapi`, `waha`,
`evolution`) são exibidos por dados do binding. O valor `whatsapp` não será registrado
como `channel_type`, evitando colisão futura com o módulo Enterprise oficial.

No Odoo 16, `mail.channel.create()` sempre inclui o parceiro do usuário corrente. Todo
canal externo nasce por uma factory do core que reconcilia a lista exata de membros e
remove qualquer criador implícito que não esteja explicitamente autorizado. Somente
guest e agentes autorizados permanecem.

Para conversa direta, o core resolve primeiro a identity e então procura o canal por
`(account, identity, direct)`. `channel.alias` não é a chave canônica. Assim, uma
identity/guest portátil pode ser membro de vários canais, mas existe no máximo uma
conversa direta ativa por `(account, identity)`. Grupos e futuras plataformas com
múltiplas threads continuam usando seus identificadores de conversa. Canais de contas
diferentes nunca são fundidos nem têm mensagens movidas durante a consolidação da
identity.

Para evitar popup, pin e resposta local no Discuss, o Odoo 16 filtra de forma estreita
`_channel_message_notifications()` para não emitir `mail.channel/new_message` em canais
`contact_center`; `_notify_thread()` permanece nativo. Edit, reaction e delete usam o
mesmo isolamento dos hooks nativos e publicam somente eventos do Contact Center. A UI
consome um canal de bus próprio.

O core expõe dois caminhos distintos:

- `post_inbound(EventDTO)` resolve conta, conversa e guest, chama `message_post()` e
  cria os bindings sem outbox, usando explicitamente `message_type='comment'`,
  `subtype_xmlid='mail.mt_comment'`, `date=occurred_at` e `partner_ids=[]`;
- `send_message(UiDTO)` valida agente, binding, reply, mídia e capabilities e cria
  `mail.message`, message binding, anexos consumidos e outbox atomicamente;
- `react_message`, `edit_message` e `delete_message` criam mutação e comando outbound
  sem projetar sucesso antes da confirmação do provider.

Não haverá override genérico que transforme qualquer `message_post()` em envio externo.
O adapter nunca é chamado por esses métodos; somente o worker processa a outbox depois
do commit. Em canal `contact_center`, um `mt_comment` fora dos serviços explícitos do
core é rejeitado para não parecer uma resposta enviada. `mt_note` é nota interna sem
binding externo e nunca gera outbox. Mensagem externa não cria `mail.notification` nem
`mail.mail`; menções do provider não são convertidas em destinatários Odoo.

`channel_fetched()` nativo ignora o novo tipo no Odoo 16. A UiDTO fornece operações
próprias `mark_fetched` e `mark_seen`, validadas por membership, que atualizam os
ponteiros de `mail.channel.member` sem depender do Discuss.

Como `contact_center` é um tipo novo, a Fase 0 deve testar deliberadamente ACL,
membership partner/guest, `channel_info`, bus, unread/read/seen, avatar, anexos,
reactions e arquivamento. A UI própria usa uma API estável mesmo que a implementação
mude de `mail.channel` no Odoo 16 para `discuss.channel` em versões futuras.

## Identidade, guest e promoção

Fluxo de identidade inbound:

1. Resolver aliases account-scoped na conta lógica e aliases portáteis confiáveis no
   escopo da empresa.
2. Se não houver correspondência, criar `contact.center.identity`, `mail.guest` e
   aliases na mesma transação.
3. Garantir o guest como `mail.channel.member` da conversa, junto dos agentes como
   membros partner.
4. Postar a mensagem externa com `mail.message.author_guest_id` usando um único helper
   testado com contexto controlado de public user + guest e `message_post()` nativo.
5. Criar o binding da mensagem na mesma transação, com origem inbound que proíbe outbox.
6. Mesmo quando a mensagem já existir por dedupe, processar novos aliases confiáveis
   para enriquecer a identidade.

Dois primeiros webhooks concorrentes com o mesmo `whatsapp.pn` protocolar devem
convergir para uma única identity/guest por empresa por advisory lock, constraint e
retry, ainda que venham de caixas diferentes. LID, JID opaco, PSID e IGSID iguais em
caixas distintas não produzem essa convergência. Se aliases resolverem identidades
diferentes, o core tenta consolidá-las somente quando a evidência portátil é forte e
nenhum blocker conservador existe; caso contrário, o inbox fica bloqueado em
`identity_conflict` para revisão.

PN e LID só são ligados por endereço primário/alternativo do mesmo papel no evento,
mapeamento validado da biblioteca/protocolo ou confirmação manual. Apenas o PN validado
ganha `resolution_scope='company'`; LID/JID continuam como evidência local da caixa.
Nome, `pushName`, formato numérico ou telefone digitado coincidente isoladamente não
bastam.

`mail.guest` é a persona persistente do Discuss, não a chave externa. PN-JID, LID, JID
bruto, ID do Telegram e equivalentes pertencem a `contact.center.identity.alias` e são
armazenados como strings. Um LID nunca é convertido em telefone.

Na projeção de nome, porém, um `whatsapp.pn` protocolar observado para a mesma identity
tem precedência sobre o LID opaco: o fallback exibido é o telefone formatado. O adapter
WuzAPI pode enriquecer assincronamente esse fallback por consulta direcionada e limitada
ao `/user/check`, aceitando somente `VerifiedName` seguro e consistente. Não se consulta
`/user/contacts` por conversa. Se não houver nome, permanece o telefone; se não houver
PN confiável, permanece o identificador opaco. Nomes manuais, partner vinculado, LID e
PN persistidos nunca são descartados por esse enriquecimento.

Eventos `from_me` seguem outra política:

- eco de comando já conhecido apenas reconcilia binding e status, sem duplicar mensagem;
- mensagem enviada pelo celular ou outro dispositivo é importada como outbound, com
  origem `external_device` e autor técnico configurado na conta;
- nenhuma dessas situações cria guest ou gera nova outbox.

Promoção significa vincular, não converter:

- o fluxo padrão cria ou seleciona explicitamente uma pessoa (`res.partner` com
  `is_company=False`); a interface orienta a identificar a pessoa que conversa com a
  equipe antes de qualquer contexto empresarial;
- o core define `contact.center.identity.partner_id` e registra quem/quando vinculou;
- `partner_id` não é único: várias identidades externas podem apontar para o mesmo
  contato;
- o `mail.guest`, sua participação nos canais e as autorias históricas permanecem;
- mensagens externas posteriores continuam usando a persona guest;
- a UI passa a mostrar o contato vinculado, permite abrir o cadastro nativo ao clicar no
  nome e oferece logo depois o vínculo/criação da empresa principal da pessoa;
- telefone/celular só é preenchido por ação explícita e a partir de PN/E.164 validado.

Existe uma exceção deliberada para um número centralizado que represente a empresa e
seja atendido por várias pessoas sem interlocutor fixo. Nesse caso, um supervisor deve
escolher explicitamente o fluxo “número central” e pode vincular `identity.partner_id`
diretamente a um `res.partner` com `is_company=True`. A busca e a criação desse cadastro
são separadas do fluxo pessoal; o servidor não infere empresa a partir do nome ou do
telefone e nunca promove automaticamente um guest para empresa. O campo protegido
`identity.partner_link_kind` persiste a decisão `person` ou `central_company`;
permissões não dependem de `res.partner.is_company`, que pode ser alterado
posteriormente no aplicativo Contatos. Mudanças incompatíveis no cadastro são expostas
como drift para revisão, sem reclassificação automática.

No fluxo pessoal, a empresa principal usa a relação nativa `res.partner.parent_id`.
Assim, a identidade externa continua sendo da pessoa, enquanto o contexto comercial vem
de `partner_id.commercial_partner_id`. Esse é o ponto de integração futuro para
pré-filtrar CRM, vendas, pedidos, faturamento e financeiro; para um número central
vinculado diretamente à empresa, `commercial_partner_id` converge para a própria
empresa.

Referência futura, fora do MVP: após a promoção, avaliar o módulo OCA
`partner_contact_in_several_companies` para representar uma pessoa vinculada a várias
empresas parceiras. No fluxo person-first, `contact.center.identity.partner_id` deve
continuar apontando para o contato principal (`standalone`); cada vínculo profissional é
um `res.partner` `attached`, ligado ao principal por `contact_id` e à empresa por
`parent_id`. Esses registros representam posições da mesma pessoa e nunca novas
identidades externas. A exceção de número central permanece vinculada diretamente à
empresa e não cria uma pessoa artificial.

Não reescrever `author_guest_id` das mensagens antigas nem usar
`mail.guest.access_token` como credencial do provider. A reconciliação automática por PN
é conservadora: bloqueia empresa/estado incompatível, contatos vinculados distintos,
nomes manuais conflitantes ou mais de uma conversa direta ativa na mesma caixa. Quando
segura, escolhe um sobrevivente determinístico, redireciona as identities aposentadas,
move aliases e bindings e substitui apenas memberships ativas pelo guest sobrevivente.
Como `mail.channel.member.guest_id` é imutável no Odoo 16, o novo membro é criado antes
da remoção do antigo. Mensagens, reações e tombstones preservam seus guests/autores
históricos; nenhum `mail.message.author_guest_id` é reescrito.

## Inbox, outbox e concorrência

O webhook autentica, grava envelope sanitizado + metadados mínimos e responde 2xx;
normalização e domínio rodam depois em `queue_job`. A chave de dedupe usa o ID nativo
quando confiável; caso contrário, o adapter gera um fingerprint determinístico e
versionado. O composer persiste mensagem, binding e outbox atomicamente; criar o ledger
enfileira o job, sem chamar o provider.

Estados mínimos:

```text
inbox:  pending → processing → done
                    ├→ retry
                    ├→ blocked | unsupported
                    └→ dead

outbox: pending → processing → done
                    ├→ retry
                    ├→ uncertain
                    └→ dead | cancelled
```

Decisão implementada na versão `16.0.1.1.0`:

- `contact.center.inbox.event` e `contact.center.outbox.command` continuam sendo os
  ledgers de negócio, auditoria, dedupe e idempotência.
- OCA `queue_job` `16.0.3.0.2` é a única infraestrutura de execução, concorrência,
  agendamento e retry; não há cron, lease, `next_attempt_at`, `SKIP LOCKED` nem backoff
  próprios do Contact Center.
- Há canais `root.contact_center.inbox` e `root.contact_center.outbox` e funções
  registradas para os dois métodos `_job_process`, com `retry_pattern` declarativo.
- Cada ledger guarda o UUID do job; `identity_key` estável evita jobs ativos duplicados.
  `attempts` e os estados do ledger são evidência de domínio, não um segundo scheduler.
- Falha ao persistir o inbox retorna 5xx, nunca 2xx. Jobs terminais, bloqueados ou
  `uncertain` não são reenviados automaticamente; requeue é ação administrativa.

A função da outbox usa `allow_commit=True`. O job grava `processing`, UUID e instante de
dispatch e faz um commit durável antes de qualquer HTTP. Se o job reaparecer após esse
boundary ou ocorrer falha ambígua depois dele, o comando vira `uncertain` para
reconciliação manual e nunca é reenviado cegamente. Estados de dispatch e receipts
externos permanecem separados.

Quando a conexão suporta `client_message_id`, um hook puro do adapter deriva o ID do
`command_id` e o persiste com message binding e outbox antes de qualquer HTTP. Eco
`from_me` pode então reconciliar mesmo se chegar antes da resposta. Sem essa capability,
eco desconhecido aguarda enquanto houver comando `processing` ou `uncertain` na mesma
conversa.

`delivery.updated` recebido antes da mensagem referenciada fica em retry com TTL para
reconciliação, não vai diretamente a `dead`. Constraints e testes com dois cursores
cobrem convergência concorrente e `IntegrityError → reload`.

OCA `queue_job` aplica o retry pattern. Conexão desconectada ou não autenticada pausa o
job sem consumir tentativas de domínio; limite de taxa por conexão continua previsto
para a Fase 1. `StreamReplaced` exige pausa e diagnóstico, não tempestade de retry.

O M4 acrescenta um fluxo de saúde sem criar um scheduler paralelo:

- o cron roda a cada minuto e somente agenda jobs idempotentes no canal
  `root.contact_center.health`, com prioridade 30 e jitter determinístico de 0–29 s;
- os estados canônicos são `connected`, `degraded`, `disconnected` e
  `authentication_required`; `unknown` é derivado por ausência/frescor e `checking` é um
  booleano transversal, portanto os estados canônicos + `unknown` somam o total e
  `checking` pode sobrepor essa contagem. UI e dispatch compartilham o mesmo limite de
  180 s; observação stale vira `unknown` e bloqueia envio;
- polling de `/session/status` repara webhooks perdidos; eventos WuzAPI de lifecycle
  aceleram a mudança. A ordem usa o recebimento durável do inbox e, no mesmo segundo, o
  ID do inbox; empate entre polling e lifecycle mantém o estado menos permissivo;
- `Connected` e `KeepAliveRestored` chegam como hints `health_confirmation_required`: o
  core publica `degraded/identity_unverified` e agenda um health job único. Somente
  `/session/status` fresco pode voltar a abrir outbound;
- um mismatch ou JID ausente em sessão conectada, vindo de um `health_job`, arma o campo
  durável `identity_mismatch_latched`. Ele não esconde uma observação fresca
  `disconnected` ou `authentication_required` na UiDTO; para conexão `connected` ou
  observação stale, publica `degraded` com `identity_mismatch` ou `identity_unverified`.
  Em todos os casos `_prepare_dispatch` bloqueia o envio;
- no WuzAPI, ausência de `account.own_external_identity` também é `identity_unverified`
  e arma o latch; nenhuma sessão é considerada `connected` para outbound sem identidade
  esperada configurada e confirmada;
- eventos lifecycle, quedas intermediárias e resultados de health que não tragam
  simultaneamente `state = connected` e `identity_matches = True` nunca limpam o latch.
  Somente essa prova positiva, fresca e obtida por `health_job` o libera;
- `Retry-After` recebido no health grava `health_retry_not_before`. Tanto o cron quanto
  o refresh manual deixam de enfileirar probes até esse instante; a UI não pode furar o
  cooldown do provider;
- `_prepare_dispatch` bloqueia qualquer conexão que não esteja ativa, outbound ativa,
  ligada a uma conta ativa, `connected`, com observação de no máximo 180 s e sem o latch
  de identidade;
- URL/token WuzAPI e identidade própria não mudam com outbound ativo ou outbox
  `processing`. Uma alteração permitida arma o latch, publica
  `degraded/identity_unverified`, incrementa `health_configuration_revision` sob lock e
  limpa o `health_retry_not_before` da revisão anterior antes de agendar confirmação.
  Imediatamente antes do I/O de health, o adapter invalida e relê do ORM URL, token e
  identidade esperada. Resultado iniciado numa revisão anterior é descartado e nunca
  libera dispatch;
- webhooks corretamente assinados de conta/conexão arquivada recebem acknowledge sem
  ledger/job. Arquivamento, troca de equipe e roster invalidam a frota; ID de conexão
  desconhecido vindo do bus provoca bootstrap filtrado, nunca inserção otimista;
- recovery observa a transição do predicado completo indisponível → disponível, não
  apenas mudança do campo `state`. Assim, um health fresco pode recuperar outbox mesmo
  se o banco já guardava `connected` stale. Ainda retoma somente `pending`/`retry` com
  `dispatch_job_uuid` e `dispatch_started_at` vazios. Um job OCA já pendente tem ETA e
  contador de retry do executor limpos, sem zerar as tentativas do ledger nem criar um
  segundo job. `uncertain` permanece sempre para reconciliação humana.

Health não inicia connect/login/QR. Uma sessão em `authentication_required` exige ação
humana, e uma conexão histórica ou standby com `outbound_active = False` nunca reativa
outboxes apenas por ter voltado a responder `connected`.

## Limites do MVP

- O escopo implementado cobre conversas diretas e conversas de grupo habilitadas por
  opt-in da conta e capabilities específicas, com um anexo por mensagem outbound.
- Campanhas, templates, importação de histórico e gestão de participantes de grupo
  permanecem fora do piloto.
- Grupos sem opt-in, capability, conexão saudável, profile ou roster comprovado falham
  fechado no servidor e na UI.
- Um canal possui uma conta lógica e um destino outbound não ambíguo.
- A homologação técnica da Fase 5.3 não constitui, sozinha, o aceite do piloto.

## Uso de ideias do Discuss Hub

O Discuss Hub será referência, não dependência. Os projetos estudados são Odoo 18/19 e
não podem ser portados diretamente para o Odoo 16.

Aproveitar:

- connector por instância, lifecycle, status e QR;
- `mail.channel` como conversa e `mail.message` como mensagem;
- anexos, replies, reactions e unread nativos;
- equipes, roteamento, transferência, tags e encerramento;
- ações extensíveis no frontend e fixtures comuns de adapters.
- no fork `lcsztl/discuss-hub`, a UX para criar ou vincular contato a partir de um
  guest.

Não copiar:

- providers no mesmo addon ou seleção fixa de tipos;
- plugin criando contato, canal e mensagem;
- chamada HTTP síncrona em `message_post`;
- webhook processando todo o domínio antes de responder;
- criação automática de usuário ou `res.partner`;
- import dinâmico de plugins e campos específicos no modelo base.
- identificação de guest somente por nome ou telefone;
- promoção que substitui membros e reescreve a autoria histórica das mensagens.

O Discuss Hub oficial não é referência para guest-first: no 18 cria `res.partner`
diretamente; no 19 cria partner e guest em paralelo, mantém `author_id` no partner e não
possui promoção. O fork `lcsztl/discuss-hub` implementa guest-first e promoção, mas sua
resolução é centrada em telefone e a promoção reescreve mensagens antigas. Reaproveitar
apenas o fluxo de interface; identidade, aliases e histórico seguem as regras deste
plano.

No Odoo 16, `mail.channel` será a conversa canônica e `mail.message` será a mensagem
canônica. Bindings auxiliares guardarão somente referências externas e metadados de
transporte, sem duplicar corpo, timeline ou estado da conversa.

No MVP, as conversas usam `mail.channel.channel_type = 'contact_center'`, sem tipo por
plataforma ou provider. Mensagens humanas continuam com
`mail.message.message_type = 'comment'`. O composer próprio chama a API de aplicação,
que cria atomicamente `mail.message`, binding e outbox; um worker chama o provider após
o commit.

## Segurança e acesso

- Grupos próprios: agente, supervisor e administrador do Contact Center.
- Papéis concedem operações; a caixa concede o escopo. `owner_user_id` é o dono
  individual opcional e `default_team_id`, mantido por compatibilidade de schema, passa
  a significar equipe com acesso opcional — não “time dono”.
- O acesso efetivo é a união do dono com agentes e supervisores da equipe. Somente dono
  configura uma caixa exclusiva; somente equipe configura uma caixa compartilhada; ambos
  configuram uma caixa com dono primário e acesso compartilhado. Sem ambos, a caixa
  falha fechada e não pode ativar tráfego.
- Agentes e supervisores enxergam somente as contas de que são donos ou cuja equipe os
  contém, e conversas em que seu partner é membro. Supervisores recebem operações
  adicionais, nunca visibilidade global implícita. O administrador é uma exceção
  privilegiada aceita para configuração e registros técnicos por empresa, inclusive
  ledgers de mídia e mutação. O ledger de receipts de grupo também é técnico, somente
  leitura para o administrador e limitado por empresa.
- A caixa lógica, e não conexão de provider, conversa, equipe de CRM ou equipe de
  Helpdesk, é a fronteira de autorização. Todas as conexões da caixa herdam o mesmo
  escopo.
- Quando uma equipe de atendimento possui vínculo CRM ativo, o CRM é somente a fonte do
  roster dessa equipe; a autorização continua sendo materializada pela equipe da caixa e
  pela membership nativa do canal. Equipes Contact Center sem vínculo continuam
  disponíveis para exceções operacionais.
- Membership partner é a concessão nativa de acesso ao canal e às mensagens. Alterar o
  dono, a equipe ou seu roster reconcilia exatamente os membros autorizados; quem é
  removido perde também a leitura histórica e deixa de ser responsável quando essa
  atribuição se torna inválida. Record rules não substituem a remoção da membership.
- A API local valida empresa, escopo da caixa e membership; sudo é restrito aos helpers
  internos mínimos.
- A promoção para contato aceita somente nome/e-mail/telefone/celular, fixa a empresa
  pela conversa autorizada e usa elevação apenas nessa criação; não concede ao agente a
  permissão global de criar contatos.
- A UI é autenticada e nunca usa `mail.guest.access_token` para dar acesso ao agente.
- Credenciais e autenticação de webhook pertencem ao addon do provider, ficam visíveis
  apenas para administradores e nunca entram em DTO, bus, fixtures ou logs.
- O contexto public user + guest existe somente dentro do helper de postagem inbound e
  não cria uma rota pública genérica para postar mensagens.
- Há limite de tamanho no webhook e binários/base64 inline são removidos antes da
  persistência. WuzAPI permanece com `--skipmedia=true`; mídia inbound é baixada depois
  do commit pelos endpoints autenticados `/chat/download*`, validada e armazenada em
  `ir.attachment` privado. O S3 nativo da revisão fixada não é baseline; qualquer adoção
  futura depende da revalidação registrada em `research/wuzapi-media-transport.md`.

## Operação em homologação

- O alvo autorizado deste ciclo é somente o SERVIDOR05, `odoo16-teste.soloz.com.br`,
  banco neutralizado `odoo16`.
- O fluxo ágil é: deploy de source, testes em bancos temporários, dry-run, upgrade e
  validate. Cada etapa mantém guards de host, UUID, neutralização, lock e versões.
- Como o SERVIDOR05 é descartável, backup do banco em `install`/`upgrade` é opt-in com
  `--backup`; sem a flag, nenhuma cópia é criada. Em produção, a política de backup deve
  ser definida separadamente antes de qualquer mudança.
- Esta fase não autorizou nem executou qualquer alteração em produção.

## Fases

### Fase 0 — Fundação — ✅ concluída em homologação em 2026-08-21

- Criar os três addons, DTOs v1, registry, contas/conexões, aliases, bindings, extensões
  nativas, ACLs e ledgers persistentes.
- Registrar `channel_type = 'contact_center'`, API de aplicação e esqueleto da UI sem
  dependência dos componentes privados do Discuss.
- Implementar factory de canal com membership exata, isolamento do bus nativo,
  `mark_fetched/mark_seen` próprios e fronteira explícita de inbound/outbound.
- Criar adapter fake somente dentro dos testes do core.
- Aceite: base instala com `queue_job` como dependência declarada; UI depende apenas da
  API local; WuzAPI registra-se sem alterar o core; constraints passam; o helper real
  public user + guest e a matriz de comportamento do novo channel type estão cobertos
  por testes; nenhum parceiro público/técnico fica membro, nenhuma janela do Discuss
  abre, comentário nativo é rejeitado, notas internas não criam outbox e mensagens
  externas não geram e-mail nem inbox nativa.
- A implantação inicial `16.0.1.0.0` passou 24 testes do core e 36 integrados; foi
  posteriormente substituída pela Fase 0.1 abaixo.
- Nesta entrega histórica o transporte WuzAPI ainda não estava habilitado; esse limite
  foi removido pela implementação `16.0.1.2.0` da Fase 1. Em 2026-08-21, o websocket
  público da homologação foi corrigido para `192.168.2.13:8169` e validado por login,
  Discuss e Contact Center na mesma sessão, sem expiração.

### Fase 0.1 — OCA queue_job e UI extensível — ✅ implantada em 2026-08-21

- `contact_center_base`, `contact_center_wuzapi` e `contact_center_ui` atualizados para
  `16.0.1.1.0`; OCA `queue_job` `16.0.3.0.2` instalado como dependência obrigatória.
- Crons, leases e scheduler próprios removidos. Ledgers Inbox/Outbox preservados e
  ligados aos canais/funções do `queue_job`; crons legados ficaram inativos.
- Boundary durável da outbox implantado com `allow_commit=True` e transição segura para
  `uncertain` após início ambíguo de dispatch.
- `Provider Connections` tornou-se a única tela de configuração. O selector vem do
  registry e a WuzAPI injeta sua aba e campos no formulário; menu e modelo WuzAPI
  separados foram removidos, com migração dos dados legados.
- Testes isolados no SERVIDOR05: 29 do core e 44 integrados, zero falhas/erros; bancos
  temporários removidos. Ensaio de migração `16.0.1.0.0 → 16.0.1.1.0`, dry-run com
  rollback, upgrade real e validação final concluídos sem módulos pendentes.
- Deploy validado em `odoo16-teste.soloz.com.br`, tree hash
  `7def819bb6b4859cdd1f66b7a0b0810771373f813cf173d36ca6d93c9fe9a2dd`. Os quatro módulos
  estão instalados nas versões esperadas; produção não foi tocada.
- JobRunner ativo e smoke real do canal inbox concluído: job
  `cf85516a-ec1d-4249-9794-c9195f0fc74d` terminou em `done`.
- Smoke visual concluído na UI: somente o menu genérico `Provider Connections` é
  exibido; selecionar `WuzAPI` abre a aba específica, sem erros ou warnings no console e
  sem persistir registro de teste.
- Esse marco foi sucedido pela versão `16.0.1.2.0`, que habilita o webhook autenticado e
  o transporte de texto da Fase 1 sem alterar a arquitetura de filas da Fase 0.1.

### Fase 1 — Texto ponta a ponta — ✅ backend concluído em homologação em 2026-08-21

Estado comprovado e aceito em homologação em 2026-08-21:

- `contact_center_base`, `contact_center_wuzapi` e `contact_center_ui` estão instalados
  no SERVIDOR05 como `16.0.1.2.0`; OCA `queue_job` permanece em `16.0.3.0.2`.
- O webhook HMAC, os envelopes `Message`/`ReadReceipt`, inbound/outbound de texto,
  aliases PN/LID, guest, canal, mensagem, correlação e receipts estão implementados e
  cobertos por testes provider-neutral e WuzAPI.
- As suites isoladas passaram 35 testes do core e 63 integrados, sem falhas ou erros; os
  bancos temporários foram removidos.
- Deploy, dry-run com rollback, upgrade sem backup e validate foram concluídos no banco
  neutralizado `odoo16` por decisão explícita para o laboratório descartável. O tree
  hash implantado é `bee585f441fb3470392303867f3991de65701ff6c85d2a0bebcfe9d36ab51b25`.
- A configuração de homologação contém exatamente uma equipe, uma conta WhatsApp e uma
  conexão WuzAPI ativa. A instância `Lucas` possui HMAC individual e webhook inscrito
  somente em `Message` e `ReadReceipt`; o processo usa `--skipmedia=true`.
- O smoke autenticado criou e processou um inbound em `done` na primeira tentativa. O
  replay dos mesmos bytes foi reconhecido como duplicata, reutilizou inbox/job e manteve
  uma identidade guest não promovida, aliases PN/LID, um canal e uma mensagem, sem
  alterar a contagem de partners.
- O outbound pela `UiAPI` concluiu uma outbox em `done` na primeira tentativa, preservou
  a igualdade entre correlação externa e `client_message_id`, chegou a `sent` e manteve
  o provider conectado. Uma nova execução com o mesmo identificador retomou os
  registros, preservou `outbox_count=1` e não realizou um segundo envio.
- O tráfego real assinado do WuzAPI acrescentou três textos inbound originados pelo
  provider e três mensagens correlacionadas `external_device`/outbound, além do evento
  sintético. As duas identidades permaneceram guests com PN/LID, sem promoção, e um
  `ReadReceipt` com binding foi processado.
- O JSON-RPC público autenticado validou bootstrap schema v1, uma conta com
  `send_message=true` e duas conversas WhatsApp/WuzAPI. As timelines sanitizadas
  retornaram uma entrada e uma saída na conversa do smoke e três entradas e três
  mensagens `external_device`/outbound na conversa real, com autoria guest nos inbounds.
- O fluxo terminou com oito bindings de `mail.message` e nenhum `mail.notification` ou
  `mail.mail`, confirmando que mensagens externas não vazaram para notificações ou
  e-mail nativos.
- A reexecução sanitizada está em
  `scans/raw/20260821-odoo16-contact-center-phase1-smoke/20260821T212430140327Z/result.json`;
  a primeira execução aplicada está em
  `scans/raw/20260821-odoo16-contact-center-phase1-smoke/20260821T212259031124Z/result.json`.
- Limitações aceitas: self-send comprova aceitação pelo provider, não leitura humana; 11
  mensagens de grupo e quatro receipts fora do escopo ficaram `unsupported`; dois
  receipts órfãos anteriores à integração chegaram a `dead` após 12 retries pela
  ausência legítima de binding.
- Produção não foi acessada nem alterada. Esse backend aceito tornou-se a base da
  interface implantada na Fase 2 abaixo.

Contrato funcional preservado pelo backend aceito:

- Webhook, inbound de texto, identidade, aliases PN/LID, guest, `mail.channel` e
  `mail.message`.
- Outbound de texto, `client_message_id`, outbox, pause/throttle, retry e receipts.
- Métricas mínimas: contagem por estado, idade do pending mais antigo e último sucesso
  por conexão.
- Aceite: remetente desconhecido cria identity + guest sem criar contato; a mensagem usa
  `author_guest_id`; PN/LID do ator e da conversa não duplicam guest ou canal; webhooks
  concorrentes e duplicados criam uma mensagem, mas ainda enriquecem aliases; `from_me`
  não cria guest nem loop; retry mantém os mesmos `command_id` e `client_message_id` sem
  assumir idempotência externa; receipt fora de ordem reconcilia por retry; trocar a
  conexão do provider preserva conta, identidade e conversa; provider não é chamado na
  transação da interface. Testes com dois cursores comprovam a convergência concorrente.
- SLO provisório para acompanhamento operacional: p95 menor que 5 s do webhook
  confirmado até o `mail.message` e do commit da UI até o início do dispatch, sem
  duplicata visível.

### Fase 2 — Interface própria de atendimento — ✅ implantada em 2026-08-21

- Client action Owl próprio com lista paginada, busca, filtros de conta/estado,
  timeline, unread/seen, replies, composer de texto, origem/provider/plataforma e
  estados de entrega/dispatch.
- Ações operacionais para `open/pending/resolved`, assumir, equipe, responsável e tags;
  painel de identidade com aliases e criação/vínculo/desvínculo explícito de contato.
- Layouts desktop e mobile próprios, com drawer de detalhes fora da navegação quando
  fechado; bundles minificado e `debug=assets` validados pelo domínio público.
- Refinamento visual: rail decorativo removido, status real do bus explícito, escala
  desktop de 11–15 px, timeline curta ancorada ao composer, avatar inbound alinhado e
  estados `Aberta/Pendente/Resolvida` traduzidos e diferenciados por cor.
- A UiDTO v1 aplica ACL, membership e empresa, pagina por chegada local, publica apenas
  eventos de bus do Contact Center e torna o envio idempotente por UUID do composer.
- Nenhum patch/import privado de Discuss ou `im_livechat`; `mail.channel` e
  `mail.message` continuam canônicos e não há segundo modelo de conversa.
- Promoção preserva `mail.guest`, membership e autoria histórica. Dois submits
  concorrentes/duplicados convergem para um único contato vinculado.
- Versões homologadas: `contact_center_base` `16.0.1.3.1`, `contact_center_ui`
  `16.0.1.3.3`, `contact_center_wuzapi` `16.0.1.2.0` e `queue_job` `16.0.3.0.2`.
- Testes finais: 42 do core e 70 integrados, sem falhas/erros; QUnit 15/15 e 59/59
  asserções; browser desktop/mobile, busca, estado, reply, detalhes e tempo real sem
  erro de console. O smoke visual não enviou mensagem real ao WhatsApp.
- Deploy final no SERVIDOR05 com tree hash
  `a9fc81d5116a5e94ae518e3127606a72f24660f2723427814ab8e2d2d57da31d`; dry-run, upgrade
  sem backup e validação concluídos. Produção não foi acessada nem alterada.

### Fase 2.1 — Papéis e escopo operacional por caixa — ✅ marco histórico substituído pela Fase 2.2

- Grupos Agent, Supervisor e Administrator definem operações; o roster explícito de
  `contact.center.team` define os registros sobre os quais elas podem ser executadas.
- `account.default_team_id` tornou-se a fronteira de autorização e roteamento. Agente e
  supervisor veem apenas equipes/contas atribuídas; conversa, timeline, aliases,
  delivery, mídia e mutações exigem membership nativa do partner no canal.
- Administradores são a exceção privilegiada aceita para configuração e registros
  técnicos por empresa, inclusive ledgers de mídia e mutação. A interface operacional de
  conversas permanece baseada em membership.
- Caixas compartilhadas usam equipes com vários agentes; caixas exclusivas usam equipes
  com um agente. O mesmo mecanismo também suporta um supervisor em uma ou mais caixas.
- Mudanças no roster reconciliam todos os canais da equipe sob lock, removem
  imediatamente o acesso histórico e limpam o responsável quando ele deixa de estar
  autorizado.
- ACLs, record rules, API e reconciliação estavam cobertos pela suíte daquele marco de
  52 testes do core e 89 integrados, sem falhas ou erros.

### Fase 2.2 — Dono individual e equipe de acesso — ✅ implantada em 2026-09-01

- Evolui a fronteira da Fase 2.1 sem trocar a conta lógica: `owner_user_id` concede
  acesso individual e `default_team_id` passa a significar equipe de acesso
  compartilhado.
- O escopo efetivo é a união desses dois grants. Assim, caixa somente com dono é
  exclusiva; somente com equipe é compartilhada; com ambos atende os dois cenários.
- A conversa apenas projeta a configuração da caixa. Não é permitido escolher uma equipe
  diferente ao criar ou transferir uma conversa.
- Mudanças de dono, equipe ou roster reconciliam a membership nativa e removem
  responsáveis que deixaram o escopo. Caixa sem dono e sem equipe fica em configuração
  incompleta e não admite tráfego novo; administradores mantêm a exceção técnica por
  empresa.
- Uma equipe Contact Center pode permanecer nativa/manual ou espelhar o roster de uma
  `crm.team` por binding explícito do addon `contact_center_crm`. O grant continua sendo
  a equipe da caixa; o CRM apenas passa a ser a autoridade de seus membros enquanto o
  binding estiver ativo.
- Implantada no SERVIDOR05 com cobertura de concorrência e revogação imediata. A suíte
  final executou **387/387** testes do core e **719/719** integrados; produção não foi
  acessada.

#### Atribuição automática opcional por caixa — base `16.0.1.31.0` — ✅ implantada no SERVIDOR05

- A caixa pode definir `auto_assignment_user_id`; vazio mantém o fluxo manual pelo botão
  **Assumir**. O seletor oferece somente usuários do escopo efetivo
  `owner_user_id ∪ agentes/supervisores da equipe` e não concede acesso adicional.
- Uma conversa nova recebe o responsável antes da criação do caso padrão. Em conversa
  histórica sem responsável, somente uma nova mensagem inbound aceita dispara a regra;
  replay deduplicado não atribui e responsável existente nunca é sobrescrito.
- Ativar a opção não distribui retroativamente o histórico. Se dono, equipe ou roster
  forem alterados e o usuário perder acesso, a configuração é desativada e a
  reconciliação existente remove responsabilidades inválidas.
- A política vive no core, funciona para qualquer provider e está disponível tanto no
  formulário da caixa quanto em `Configuração → Adicionar caixa`.
- Implantada na árvore `a76d11a7c74e...`: **396/396** testes base, **203/203** WuzAPI e
  **729/729** integrados, todos sem falha. As oito caixas existentes permaneceram com a
  opção desativada; smoke autenticado da UI passou sem erro de console e produção não
  foi acessada.

### Fase 3 — Mídia e recursos de mensagem — ✅ implantada em 2026-08-21

- Imagem, áudio, vídeo e documento estão implementados inbound/outbound por capability
  estruturada; reply, reaction, edit e delete respeitam capabilities independentes.
- O browser envia um arquivo por rota autenticada e recebe uma referência opaca. O
  upload privado é consumido atomicamente com `mail.message`, binding e outbox; o
  primeiro recorte permite um anexo outbound com caption opcional.
- WuzAPI permanece deliberadamente com `--skipmedia=true`. O webhook sanitiza base64,
  thumbnails, waveform e cópias redundantes; após o commit, `queue_job` baixa a mídia
  por `/chat/downloadimage`, `/chat/downloadaudio`, `/chat/downloadvideo` ou
  `/chat/downloaddocument`, valida tipo/MIME/tamanho/hash e cria `ir.attachment`
  privado. O S3 nativo não é baseline desta revisão.
- Reply só é oferecido quando existe ID externo referenciável. Reaction, edit e delete
  usam ledger idempotente; projeções outbound ocorrem somente após sucesso do provider.
  Delete produz tombstone sem remover a `mail.message`; mutações inbound fora de ordem
  aguardam o binding por retry, sem duplicação.
- A UI própria envia e renderiza mídia, exibe edited/tombstone e oferece ações
  acessíveis de reply/reaction/edit/delete sem importar o Store privado do Discuss.
- A rota de conteúdo aplica autenticação, ACL e record rules; usuários operacionais são
  limitados pela membership nativa. Ela responde `Cache-Control: no-store` e suporta
  Range; o teste funcional confirmou `206` válido e `416` inválido.
- Versões homologadas: `contact_center_base` `16.0.1.5.0`, `contact_center_ui`
  `16.0.1.4.1`, `contact_center_wuzapi` `16.0.1.3.0` e `queue_job` `16.0.3.0.2`.
- Validação isolada final: 52 testes do core e 89 integrados, sem falhas/erros. QUnit
  passou 20 testes e 97 asserções tanto no bundle minificado quanto em `debug=assets`.
- O smoke real WuzAPI concluiu mídia outbound, reply, edit, reaction e delete com HTTP
  200; todas as outboxes criadas pelo ensaio ficaram em `done`, sem mídia ou mutação
  testada em estado de falha. Mídia inbound real não foi disparada externamente e ficou
  coberta por fixtures e testes integrados.
- Build funcional implantado no SERVIDOR05 com tree hash
  `729be81062d118d0e8295f0e8001d726846cd2bbdb9fae9db7a841afa593f88a`; dry-run, upgrade
  sem backup e validação concluídos. Produção não foi acessada nem alterada.

### Fase 4 — Piloto e hardening — código homologado; aceite operacional pendente

- QR/login/connect continuam ações humanas. Healthcheck, observação de lifecycle, painel
  da frota e recuperação segura da outbox estão implantados no SERVIDOR05; replay,
  dead-letter e os demais testes operacionais de falha continuam no piloto.
- Marco inicial de homologação em 2026-08-22: as cinco instâncias WuzAPI conectadas
  (`Lucas` e `Comercial01`–`Comercial04`) foram registradas como cinco vínculos
  exclusivos equipe → conta → Provider Connection. Cada conexão possui HMAC e webhook
  próprios para `Message` e `ReadReceipt`, health/capabilities válidos e outbound ativo.
  O dry-run posterior encontrou zero operações pendentes; nenhuma mensagem real foi
  enviada nesse cadastro.
- Marco de hardening de 2026-08-22, implantado e validado no SERVIDOR05:
  `contact_center_base` `16.0.1.6.0`, `contact_center_wuzapi` `16.0.1.4.0` e UI mantida
  em `16.0.1.4.1`. O release adiciona viewer JSON somente leitura, renomeia o payload
  inbound para **Sanitized Envelope** e persiste um snapshot sanitizado do request do
  provider antes do I/O; snapshots de mídia nunca carregam base64, binário ou segredo.
- Achados da auditoria incorporados neste marco: M1 (backoff de mídia pelo
  `retry_pattern`, com 12 tentativas), M2 (recuperação manual supervisionada e reset de
  tentativas), M3 (backoff de receipts/mutações órfãos pelo pattern), M5 (documentos
  potencialmente ativos como attachment, CSP e `nosniff`), M6 (JSON técnico do inbox
  restrito ao Administrator) e M9 (regras company-scoped de Inbox/Outbox para o
  Administrator). Requeue automático de mídia e estado terminal próprio para órfãos não
  entram neste recorte.
- Marco M4 de 2026-08-23, implantado somente no laboratório SERVIDOR05:
  `contact_center_base` `16.0.1.7.0`, `contact_center_wuzapi` `16.0.1.5.0`,
  `contact_center_ui` `16.0.1.5.2` e OCA `queue_job` `16.0.3.0.2`. O cron de um minuto
  só agenda health jobs OCA, o polling converge com os eventos exatos de lifecycle, a
  identidade própria é validada contra a sessão e mismatch/JID ausente mantêm dispatch
  bloqueado até um health posterior comprovar explicitamente o match. Frescor de 180 s,
  confirmação obrigatória dos hints `Connected`/`KeepAliveRestored` e cooldown durável
  de `Retry-After` também estão implantados. A UI resume aproximadamente 20 números sem
  disparar um bootstrap completo por evento.
- A capacidade atual do JobRunner do laboratório é
  `root:4,root.contact_center.health:2,root.contact_center.media:1`: até dois probes
  simultâneos, no máximo um download lento e ao menos um slot global restante no pior
  caso combinado. A topologia deverá ser medida novamente sob a carga do piloto e não
  autoriza configuração de produção.
- M7, M8, M10 e M11 foram mantidos separados neste marco e posteriormente concluídos no
  incremento de 2026-08-23 descrito abaixo. A disposição histórica deste release está em
  `reviews/2026-08-22-phase3-audit-disposition.md`.
- Validação final: 63 testes do core e 100 integrados, sem falhas/erros. QUnit passou a
  suíte do viewer base com 4 testes/15 asserções e a UI com 20 testes/97 asserções,
  tanto no bundle minificado quanto em `debug=assets`. Dry-run transacional, upgrade e
  validação do banco neutralizado foram concluídos; a árvore final implantada tem hash
  `bcb10d73b37fb6a0621f4ac54d7fb092900ea57dbc249a406253def44cbd3647`. Nenhuma mensagem
  real foi enviada e produção permaneceu fora do escopo.
- Aceite provisório: um número e uma equipe por pelo menos cinco dias úteis e 100
  mensagens de texto; zero duplicata visível; nenhum `dead` ou `uncertain` sem causa por
  mais de 24 h; indisponibilidade, timeout ambíguo, duplicação e reconexão documentados
  e testados. As metas finais são recalibradas pelo benchmark da Fase 1 antes do piloto.
- O build M4 implantado tem tree hash
  `40c486d6de2c753cd6efa940763266b4591e4c4bcc3b9c27a390ee4c2d856c8c`. As suítes do
  backend passaram 92/92 no core e 141/141 integradas, com evidência em
  `scans/raw/20260823-odoo16-contact-center-m4-health-recovery/test/20260823T215648678610Z`;
  QUnit passou 28/28 testes e 168 asserções. As cinco conexões WuzAPI ficaram conectadas
  e com identidade própria válida. No navegador, **Verificar todas** convergiu sem
  reload e sem erros no console após a correção da corrida RPC/bus com revision fence,
  polling limitado a 11 tentativas/180,5 s e reatividade `connectionHealthRevision`.
  Nenhuma mensagem foi enviada, nenhum backup foi feito por instrução para o banco
  descartável, e produção não foi acessada nem alterada.
- Incremento de qualidade de 2026-08-23 implantado no SERVIDOR05: base `16.0.1.7.1`,
  WuzAPI `16.0.1.5.1` e UI `16.0.1.5.3`, tree hash
  `340b759bb389b6063b173a474a81f8d3cc86c4de20c9613c7576cc01a3af36c1`. Os cinco hotspots
  Python e os dois hotspots JavaScript foram decompostos, os gates estáticos ficaram
  limpos e foram adicionados testes de concorrência/paginação, download WuzAPI e teto
  comum de retry. Validação: 94/94 testes do core, 145/145 integrados e QUnit 30/30 com
  192/192 asserções; o painel convergiu em 5/5 conexões sem erro de console. Refatoração
  física dos arquivos grandes, mixin genérico de fila e helpers genéricos de `sudo()`
  foram recusados neste release por churn/risco; detalhes em
  `reviews/2026-08-23-code-quality-disposition.md`.
- Incremento M7/M8/M10/M11 de 2026-08-23 implantado no SERVIDOR05: base `16.0.1.8.0`,
  WuzAPI `16.0.1.6.0`, UI `16.0.1.6.0` e tree hash
  `68812fa2b4fdc866c672de3f8b6e71f0e7bfa8be897f10a8b6f5f43bce742546`. Edits e reactions
  são monotônicos, delete é terminal, eco `from_me` usa o autor técnico da conta e alvos
  aceitam ID externo ou do cliente. Reações nativas do Discuss em mensagens Contact
  Center são bloqueadas. Outbound de mídia lê `attachment.raw` e codifica uma vez; o
  ensaio de 50 MiB reduziu o pico observado de aproximadamente 400 para 281 MiB. Refresh
  em tempo real faz merge sem truncar o histórico carregado, preserva cursor/scroll e
  anuncia novas mensagens quando o operador está lendo acima. Validação: 104/104 testes
  do core, 159/159 integrados, QUnit UI 32/32 com 214/214 asserções e viewer 4/4 com
  15/15 nos bundles minificado e `debug=assets`; navegador com 25 conversas, 5/5
  conexões e zero erro/warning. Detalhes em
  `reviews/2026-08-23-phase3-audit-followup-disposition.md`.
- Follow-up `1.8.1/1.6.1` de 2026-08-24 implantado no SERVIDOR05: a lane de reactions
  agora participa da mesma revisão monotônica persistida; troca concorrente de conversa
  não mistura timelines; mutation, receipt e eco `from_me` correlacionam IDs dentro do
  canal resolvido por aliases ou `conversation_ref`; edit/delete próprios independem de
  autor técnico; e o replay administrativo de inbox terminal possui lock, confirmação,
  guarda de job ativo e de conflito aberto. Os dois deletes que haviam morrido foram
  reenfileirados de forma dirigida e terminaram em `done`, sem tocar nos 28 receipts
  antigos. A migração limpou campos de reaction em edit/delete. Tree hash
  `1241fbd58337f571756f05b85d17f9d134ef92171d7b31ccede271ab86a51b95`; validação
  **110/110** core, **165/165** integrada, QUnit UI **33/33** e 222/222 asserções,
  viewer **4/4** e 15/15 nos dois bundles, navegador com 26 conversas, 5/5 conexões e
  zero erro/warning. Disposição em
  `reviews/2026-08-24-followup-verification-disposition.md`.
- Processo: o diretório passou a ser um repositório Git independente, branch `16.0`, com
  commits auditáveis e tag local do marco anterior `16.0.1.8.0-lab`. O remoto GitHub
  ainda não existe; nenhum push foi feito.
- Backlog preservado após esse incremento: reconciliação exclusivamente local de outbox
  `uncertain` por eco; streaming verdadeiro de mídia; cursor forward para lacuna de
  reconexão superior a 100 mensagens; i18n JavaScript amplo; expurgo de PII após
  política de retenção; queda/reconexão real no ensaio do piloto. Mídia inbound real já
  possui 16 anexos `ready` e íntegros no laboratório; concorrência e memória ainda
  precisam de benchmark.
- Permanecem para o piloto os smokes operacionais destrutivos/de falha sob carga,
  inclusive queda/reconexão e canário `uncertain`; o aceite geral do piloto não deve ser
  inferido desta homologação de laboratório. A capacidade observada cobre cinco, não
  vinte conexões. O cache descartável do builder Docker foi removido quando o filesystem
  chegou a 98%, recuperando 1,362 GiB e deixando aproximadamente 3 GiB livres;
  capacidade e disco ainda devem ser monitorados antes de ampliar a carga.
- Incremento de hardening de webhooks da Fase 4 implantado em 2026-08-24 somente no
  SERVIDOR05: base `16.0.1.17.0`, WuzAPI `16.0.1.14.0`, UI mantida em `16.0.1.14.0` e
  OCA `queue_job` `16.0.3.0.2`. Nomes observados pelo provider agora convergem de forma
  monotônica para identity, guest e canal direto gerenciados; nomes manuais e contatos
  promovidos são preservados. A migração faz backfill apenas dos DTOs canônicos já
  persistidos, sem chamada ao provider e sem replay bruto.
- A auditoria completa dos 114 webhooks do caso LID sanitizado `lab-subject-a@lid`
  confirmou a corrida de criação: o evento de recuperação chegou sem nome e o evento
  canônico seguinte já continha o nome esperado e o alias PN. Após migração e replay
  dirigido, há uma única conversa com o nome correto, 45 mensagens correlacionadas e 45
  IDs externos únicos. Três mensagens diretas antigas de outro dispositivo e seis
  receipts dependentes terminaram em `done`; três receipts próprios de leitura
  terminaram corretamente em `unsupported`, sem retries órfãos.
- Mensagem direta `from_me` desconhecida passa a criar a conversa pelo endereço remoto,
  sem usar o `PushName` da própria conta como nome do guest. Receipt direto próprio não
  é mais confundido com delivery outbound. Lote WuzAPI com `MessageIDs` explicitamente
  vazio é terminal `unsupported`, mantendo falha transitória apenas para payload ausente
  ou estruturalmente inválido.
- Documento com MIME vendor, inclusive `image/vnd.dwg`, é aceito como attachment privado
  sem ser renderizado inline. Dois downloads DWG históricos convergiram para `ready`;
  104 receipts vazios foram reclassificados de `dead` para `unsupported` por replay
  exato, sem reprocessamento amplo da dead-letter.
- Homologação: **193/193** testes base e **290/290** integrados, sem falha/erro.
  Chromium autenticado mostrou o nome, guest, aliases LID/PN e timeline multimídia
  corretos, frota **5/5 conectada** e zero erro de console. Runtime hash
  `ea16430a070ca609cc8f1bbf6c56e8e8ddd752e11a7f699fd4b28dbe2b6235fc`; evidências em
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/`, release atômico em
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/` e
  relatório em `reviews/2026-08-24-phase4-pilot-hardening-validation.md`.
- O código previsto para a Fase 4 está homologado no laboratório. O aceite operacional
  do piloto continua condicionado ao ensaio coordenado de queda/reconexão, canário
  `uncertain` e medição de capacidade; isso não bloqueia o desenvolvimento das features
  posteriores. Os dois vídeos ainda em `failed` excedem o teto configurado de 50 MiB e
  representam política de tamanho, não falha do pipeline.
- Fechamento técnico de 2026-08-28: as cinco conexões ficaram `connected/healthy`, sem
  identity latch ou falha nova após o release. O read-back assíncrono confirmou URL e os
  16 eventos desejados em 5/5 caixas. Um canário real na instância Lucas criou e
  deduplicou o inbound sintético, preservou o guest compartilhável por conta e concluiu
  a outbox em uma tentativa, com correlação externa e sessão conectada. O script de
  smoke foi corrigido para validar aliases da conta atual, pois um mesmo guest pode ter
  aliases em várias caixas. Isso conclui o gate funcional WuzAPI; queda/reconexão real,
  timeout ambíguo e capacidade para aproximadamente 20 caixas continuam ensaios do
  rollout, não defeitos de código conhecidos.

### Fase 5 — Conversas de grupo — Fase 5.1 implantada no laboratório em 2026-08-24

- A primeira entrega é inbound e somente leitura: apenas `Message/message.created`
  humano, inbound e não `from_me` de chats `@g.us`.
- Um único `mail.channel` Contact Center por conta + JID do grupo; o binding terá
  `conversation_type='group'` e não terá identidade de pessoa.
- Cada participante será resolvido como `contact.center.identity` + `mail.guest`; PN e
  LID ficam nos aliases do participante, enquanto o JID do grupo fica somente nos
  aliases do canal. `mail.message.author_guest_id` preserva o autor individual.
- Composer, upload, reply, reaction, edit, delete, receipts e ecos de grupo ficam
  bloqueados também no servidor nesta primeira entrega. A UI exibirá badge de grupo e
  estado somente leitura.
- Broadcasts `@broadcast` não serão classificados como grupo. Mensagens sem conteúdo
  humano, contendo somente distribuição de chaves/contexto, terminam `unsupported` sem
  criar bolha vazia.
- Metadata (`GroupInfo`, `JoinedGroup`, `Picture`), reconciliação por `/group/info`,
  participantes/administradores, outbound e receipts por participante serão fases
  posteriores, sempre com fixtures reais e contrato provider-neutral.
- Fase 5.1 homologada nas versões base `16.0.1.9.0`, WuzAPI `16.0.1.7.0` e UI
  `16.0.1.7.0`, com tree hash
  `52d533492f4198c035f51ee3f4620097f42ce4cfb204461fc1d9640c3100f032`. Passaram
  **121/121** testes do core e **181/181** integrados; QUnit passou UI **35/35** com
  237/237 asserções e viewer **4/4** com 15/15, em bundle minificado e `debug=assets`.
- Um evento real de grupo recebido após o upgrade criou exatamente um binding sem
  `identity_id`, um alias de canal com papel `group`, uma mensagem com
  `author_guest_id`, nenhuma outbox e nenhuma duplicata. A UI real mostrou badge,
  participante e rodapé somente leitura, sem composer; as cinco conexões permaneceram
  conectadas e não houve erro ou warning no console.
- A revisão adversarial ficou sem achado alto ou médio após endurecer o core contra
  atores com papel de roteamento e fazer a UI habilitar controles somente para
  `conversation_type='direct'`. Validação estrutural mais estrita de JID, teste
  integrado de download de mídia de grupo, ledger de conflito de routing e regressão
  multi-plataforma com namespace compartilhado permanecem backlog baixo.

### Fase 5.2 — Metadata técnica de grupos — ✅ implantada e validada em 2026-08-24

- O core acrescenta um profile 1:1 ao binding de grupo e um roster técnico com aliases
  PN/LID. Sincronizar esse roster não cria `mail.guest`, identity, `res.partner` nem
  membership do canal; somente autores realmente observados seguem o fluxo da Fase 5.1.
- `GroupInfo`, `JoinedGroup` e `Picture` são apenas hints assinados para agendar um pull
  assíncrono. O snapshot autoritativo vem de `GET /group/info?groupJID=<jid-do-grupo>`
  no WuzAPI `v1.0.8`, commit `9487eca`.
- Snapshots completos podem reconciliar remoções e publicar contagens. Snapshots
  parciais apenas fazem upsert, permanecem `stale` e ocultam contagens não
  autoritativas. O TTL completo é seis horas e o retry parcial ocorre em 15 minutos.
- A foto é localizada por `POST /user/avatar`, baixada e validada assincronamente com
  limite de 2 MiB, persistida como attachment privado e exposta somente por rota local
  autenticada; URL remota e credenciais não entram no UiDTO.
- A UI continua inbound/read-only e recebe somente nome, avatar local, contagens
  agregadas, papel próprio, estado e instante da sincronização. Roster, aliases PN/LID,
  revisões e JID bruto ficam fora da API operacional e do bus.
- O parser aceita tanto o JID numérico atual quanto o formato legado
  `numeric-hyphen-numeric@g.us`, sem aceitar broadcasts como grupos.
- As cinco instâncias foram inscritas em `GroupInfo`, `JoinedGroup` e `Picture`. O
  backfill terminou com **51/51** profiles `ready`, 49 avatares, 19.365 participantes
  técnicos e 38.697 aliases. Os totais de `mail.guest`, `res.partner` e
  `mail.channel.member` ficaram invariáveis durante a convergência, sem criação em massa
  pelo roster.
- Um snapshot read-only posterior ao `16.0.1.8.3`, já com novos dados chegando, mostrou
  **53/53** profiles `ready`, 51 avatares, 19.860 participantes técnicos, 39.686 aliases
  e 5/5 conexões ativas `connected`. Esse retrato vivo é separado da prova controlada de
  invariância do backfill acima.
- A ação administrativa pública `action_retry_metadata_sync`, protegida por ACL, record
  rule e lock, recuperou as 15 sincronizações inicialmente falhas; todas convergiram.
- Versões homologadas no SERVIDOR05: OCA `queue_job` `16.0.3.0.2`, base `16.0.1.11.0`,
  WuzAPI `16.0.1.8.0` e UI `16.0.1.9.0`. Passaram **147/147** testes do core,
  **222/222** integrados, QUnit UI **47/47** com 416/416 asserções e viewer **4/4** com
  15/15, nos bundles minificado e `debug=assets`. O handoff acrescenta agrupamento de
  mensagens e conversas por caixa, viewer interno de imagem/vídeo, player de áudio,
  ações touch/acessíveis e painel compacto de saúde.
- No navegador real havia 133 conversas abertas e 5/5 conexões; grupo somente leitura,
  nome, avatar e agregados foram exibidos sem erro ou warning no console. A rota
  autenticada de avatar respondeu `200 image/jpeg`, `nosniff` e cache privado; acesso
  anônimo frio foi redirecionado para login.
- O smoke do handoff agrupou oito mensagens reais em um run (um início, sete
  continuações e a última também como fim), sem composer. Dezoito players de áudio
  chegaram a `readyState=4`, sem erro, e alternaram de 1x para 1,5x. O viewer unitário
  ainda mantinha grid de três colunas e comprimia uma imagem natural 1200x1600 para
  aproximadamente 13 px. Após o patch `16.0.1.8.3`, o desktop mediu stage de 1138 px e
  imagem 464x618, sem navegação; em mobile 390x844, stage 378 px e imagem 366x488, sem
  overflow. `Esc` restaurou o foco e o console terminou em zero erro/warning.
- Os recortes de responsabilidade agora são globais e aplicados no servidor antes do
  cursor, paginação e total. O smoke comprovou duas conversas em `Minhas` e 131 em
  `Não atribuídas`, com seleção reconciliada. A fila de imagens por aba inicia somente
  mídia próxima da viewport, prioriza a timeline, reserva dimensões e limitou o pico
  real a duas requisições concorrentes antes e depois do scroll incremental.
- Tree hash do código runtime implantado:
  `39721ec9cdbb368999f0587b5a20da53f2a7f988f2f70040fb0b7b0e3cd23481`. O upgrade alterou
  somente base/UI (IDs 3817/3818), manteve os dados de negócio e foi executado sem
  backup de banco do laboratório; o deploy reteve somente a cópia automática e
  recuperável da árvore de código anterior. Produção não foi acessada.
- Evidências finais de deploy, upgrade e validate:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/`.
- Evidência consolidada em `reviews/2026-08-24-phase5-2-group-metadata-validation.md`.

### Fase 5.3 — Operações de grupo — ✅ concluída e validada em 2026-08-24

#### Fase 5.3a — texto novo e correlação — ✅ implantada e validada em 2026-08-24

- Capabilities passaram a ser efetivas por tipo de conversa. Grupo só abre envio quando
  o adapter declara `conversation_types.group.send_message`, a conta possui opt-in e a
  conexão exata do profile está ativa para outbound.
- O opt-in `group_outbound_enabled` nasce desligado. No laboratório, somente
  `WhatsApp Lucas - Laboratorio` foi liberada; as quatro caixas comerciais continuam
  somente leitura.
- O composer de grupo aceita somente texto novo. Upload, reply, reaction, edit, delete e
  receipts continuam bloqueados no modelo, API, outbox, dispatch e UI.
- A WuzAPI envia texto pelo contrato existente `/chat/send/text`, mantendo o JID
  canônico completo `@g.us` como destino e o ID de correlação de 32 caracteres.
- Eco `from_me` correlacionado conclui a mensagem/outbox sem duplicar. Mensagem enviada
  pelo aparelho cria uma única projeção com autor técnico; se for a primeira mensagem
  humana de um grupo desconhecido, pode criar canal/profile sob opt-in e agenda
  `GroupInfo`. A correlação global funciona somente como bloqueio contra IDs locais de
  outra conversa, que nunca podem contaminar aliases ou iniciar o novo grupo.
- O boundary revalida conta, opt-in, capability, profile/conexão, endereço e papel do
  grupo, forma textual, ausência de mídia/reply/mutação e igualdade dos três
  `client_message_id` persistidos.
- Versões homologadas no SERVIDOR05: OCA `queue_job` `16.0.3.0.2`, base `16.0.1.12.1`,
  WuzAPI `16.0.1.9.0` e UI `16.0.1.10.0`. Passaram **153/153** testes base e **230/230**
  integrados; QUnit UI **47/47**, 419/419 asserções, e viewer **4/4**, 15/15, em
  minificado e `debug=assets`.
- A UI real mostrou composer textual apenas em grupo da caixa Lucas, com anexo
  desabilitado, e rodapé `Envio não habilitado` em grupo de caixa comercial. A frota
  ficou **5/5 conectada**, o console limpo teve zero erro/warning e nenhum envio real
  foi executado: permaneceram zero outboxes e zero bindings de agente para grupos.
- Tree hash final do runtime:
  `36d68c66e1a42981a79e0856031234489abe109838affa3a7909cbea67c71a65`. Evidências em
  `scans/raw/20260824-odoo16-contact-center-phase5-3a-group-text/` e disposição em
  `reviews/2026-08-24-phase5-3a-group-text-validation.md`.

#### Fase 5.3b — mídia outbound e reply — ✅ implantada e validada em 2026-08-24

- Reply de grupo exige o participante protocolar canônico da mensagem-alvo. O core
  persiste PN/LID, revisão de saúde e snapshot de correlação; ausência, ambiguidade ou
  mudança do roster falha fechado antes da chamada externa.
- A escolha PN/LID segue o `AddressingMode` observado no snapshot autoritativo do grupo.
  O dispatch revalida conexão, profile, revisão e participante antes e depois do HTTP;
  somente sucesso confirmado persiste participante e ID externo.
- O composer aceita texto, imagem, áudio, vídeo e documento em grupos quando capability,
  opt-in e saúde permitem. Áudio não aceita legenda; cada mensagem continua limitada a
  um anexo. Reply e mídia usam os DTOs provider-neutral do core.
- Eco `from_me` reutiliza a projeção existente. A equivalência entre PN e LID só é
  aceita quando o roster resolve ambos para o mesmo participante; conflito de alvo ou
  reply falha fechado, inclusive em resposta ambígua/`uncertain`.
- A migração WuzAPI `16.0.1.11.0` atualiza capabilities, agenda metadata, remove
  evidência obsoleta e retropreenche o participante das mensagens históricas a partir de
  snapshots protocolares. O lock order permanece
  `connection -> channel/profile -> binding`.
- Versões homologadas no SERVIDOR05: OCA `queue_job` `16.0.3.0.2`, base `16.0.1.14.0`,
  WuzAPI `16.0.1.11.0` e UI `16.0.1.12.0`. Passaram **165/165** testes base, **252/252**
  integrados e QUnit UI **48/48**, 438/438 asserções, em minificado e `debug=assets`.
- O estado final contém **62/62** profiles `ready`, todos com participante canônico na
  revisão de saúde vigente. As quatro mensagens históricas outbound de grupo que
  possuíam evidência de remetente foram retropreenchidas.
- A UI autenticada carregou 159 conversas, exibiu grupo, metadata e bloqueio seguro para
  uma conta sem opt-in. A frota ficou **5/5 conectada** e o console terminou com zero
  erro/warning. Nenhuma mensagem real de grupo foi enviada nesta validação.
- Tree hash final do runtime:
  `b8c82a93e419a63c373a7012cda7c442c3e766d95f69b96ab0f58502b7828372`. Evidências em
  `scans/raw/20260824-odoo16-contact-center-phase5-3b-group-media-reply/` e relatório em
  `reviews/2026-08-24-phase5-3b-group-media-reply-validation.md`.

#### Fase 5.3c — reaction, edit e delete — ✅ implantada e validada em 2026-08-24

- Reaction suporta alvo remoto ou próprio. No inbound, o binding correlacionado define
  direção/participante; edit exige o autor e delete aceita também `admin`/`superadmin`
  ativo do roster. No outbound, edit/delete permanecem restritos a mensagens próprias,
  outbound e correlacionadas.
- Inbound, eco e dispatch revalidam ator, autoria, direção, conexão, profile, roster e
  equivalência PN/LID. Resultado ambíguo ou evidência incompleta falha fechado.
- A WuzAPI usa `/chat/react`, `/chat/send/edit` e `/chat/delete`; reaction própria usa
  `me:<id>` e reaction remota envia `Participant`.
- O ledger de mutações preserva monotonicidade por ocorrência, lanes por ator e delete
  terminal. Eco `from_me` resolve o ator pelo lado próprio da conta.

#### Fase 5.3d — receipts por participante — ✅ implantada e validada em 2026-08-24

- `ReadReceipt` delivered/read resolve o ator pelo roster técnico e correlaciona apenas
  mensagens outbound da mesma conversa e conexão.
- Replays PN/LID e eventos fora de ordem convergem sem criar guest, contato, identity ou
  membership. Lotes mistos preservam IDs conhecidos e registram apenas a quantidade de
  correlações ausentes como metadata técnica do Inbox.
- O agregado da mensagem permanece `sent`; receipts fornecem somente contagens por
  participante. O ledger técnico é leitura administrativa por empresa, enquanto a UI
  recebe apenas `delivered_count` e `read_count`.
- A migração WuzAPI `16.0.1.12.0` atualiza capabilities e agenda metadata, sem replay
  automático do histórico bruto.
- Versões homologadas no SERVIDOR05: OCA `queue_job` `16.0.3.0.2`, base `16.0.1.15.0`,
  WuzAPI `16.0.1.12.0` e UI `16.0.1.13.0`. Passaram **176/176** testes base e
  **267/267** integrados. QUnit passou **51/51**, 478/478 asserções da UI, e **4/4**,
  15/15 asserções do visualizador JSON, em minificado e `debug=assets`.
- A UI autenticada abriu conversas diretas e de grupo, mostrou metadata de
  participantes, ações por mensagem e frota **5/5 conectada**, sem erro de console.
  Nenhuma mutação nem mensagem real de grupo foi disparada pela validação.
- Tree hash final do runtime:
  `6cb0020a07c9189880076d4a2144cdf3d900dd601e113bf28137dd856c562f08`.
- Evidências em `scans/raw/20260824-odoo16-contact-center-phase5-3-group-events/`,
  release atômico em `scans/raw/20260824-odoo16-contact-center-phase5-3-atomic-release/`
  e relatório em `reviews/2026-08-24-phase5-3-group-events-validation.md`.

#### Follow-up — avatar individual e alinhamento de runs — ✅ implantado e validado em 2026-08-24

- O avatar individual é uma projeção provider-neutral da conversa direta, vinculada ao
  `contact.center.channel.binding`. Assim, a visibilidade continua específica por
  conta/instância e não altera `mail.guest` nem o `res.partner` promovido.
- O evento `identity.avatar.changed` apenas invalida uma conversa já existente; evento
  de foto desconhecido não cria guest, identity, canal ou mensagem.
- A busca ocorre em `queue_job`, com backoff, TTL de 24 horas e backfill inicial
  escalonado. O adapter devolve somente `AvatarResult`; URL remota nunca entra no DTO,
  banco operacional, bus ou frontend.
- O core limita a 2 MiB, valida JPEG/PNG/WebP por conteúdo, persiste anexo privado e
  entrega somente pela rota autenticada
  `/contact_center/conversation/<channel_id>/avatar` após validar membership.
- No SERVIDOR05, o backfill processou 113 conversas diretas: 91 `ready`, 3 `absent` e 19
  `unavailable`, sem erro técnico e sem job pendente.
- Avatar e spacer de mensagens consecutivas passaram a usar a mesma variável CSS: 30 px
  no desktop. O smoke real mediu todas as bolhas inbound no mesmo `x=411`, em conversa
  direta e grupo, inclusive 12 e 9 continuações respectivamente.
- Versões homologadas: base `16.0.1.16.0`, WuzAPI `16.0.1.13.0` e UI `16.0.1.14.0`.
  Passaram **181/181** testes base, **277/277** integrados e QUnit UI **53/53**,
  **495/495** asserções, em minificado e `debug=assets`.
- A sessão Chromium limpa mostrou avatar individual real na lista e painel por URL
  local, sem erro no console. Runtime hash:
  `6570fffb30128a3a3a3a2594f75aa1cff025767eed67d14140371be93d405438`.
- Evidência consolidada em `reviews/2026-08-24-direct-avatar-alignment-validation.md`.

#### Follow-up — controles por caixa, webhooks e áudio — ✅ implantado e validado em 2026-08-25

- Cada conta/caixa possui opções independentes para assinar mensagens do agente, receber
  mensagens de grupo e enviar para grupos. A assinatura é persistida como snapshot
  semântico do operador e renderizada somente pelo adapter WuzAPI como
  `*Nome:*\nMensagem`; o corpo canônico permanece limpo.
- A conexão WuzAPI publica um catálogo versionado com 48 eventos selecionáveis. Aplicar
  e verificar apenas enfileiram jobs OCA; escrita remota, read-back, drift, retry e
  fencing por revisão ocorrem fora da request. O read-back real da conexão Comercial01
  terminou `In Sync`, inclusive para a URL configurada.
- O perfil convidado ganhou edição inline do nome, restrita ao escopo operacional. A
  operação preserva o mesmo `mail.guest`, aliases PN/LID, vínculo opcional e autoria
  histórica.
- O composer grava áudio com `MediaRecorder` somente quando navegador e capability do
  provider concordam. OGG/Opus vira voice note WuzAPI; MP4 audio-only vira áudio comum.
  Upload e dispatch validam container, hash, tamanho e duração extraída no servidor,
  incluindo MP4 fragmentado, sem confiar na duração informada pelo cliente.
- O estudo read-only das quatro contas comerciais registrou os sinais `externalAdReply`,
  `ctwaClid`, `conversionSource` e entry points em
  `research/meta-click-to-whatsapp-attribution.md`. A decisão de modelar atribuição como
  touchpoint imutável da conversa foi implementada no incremento seguinte; somente o
  bridge CRM permanece posterior.
- Versões homologadas no SERVIDOR05: OCA `queue_job` `16.0.3.0.2`, base `16.0.1.18.0`,
  WuzAPI `16.0.1.15.0` e UI `16.0.1.15.0`. Passaram **208/208** testes base e
  **320/320** integrados. QUnit UI passou **61/61**, 545/545 asserções, nos bundles
  minificado e `debug=assets`.
- Chromium autenticado confirmou as flags, edição do guest, assinatura indicada no
  composer, seletor/read-back de webhook e gravação/upload de um MP4 fragmentado real
  com prévia de 21 segundos. O arquivo foi removido sem disparar mensagem; a aplicação e
  ambos os QUnit terminaram sem erro ou warning de console.
- Evidências de deploy/teste/validação estão em
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/` e consolidadas em
  `reviews/2026-08-25-per-inbox-webhooks-voice-validation.md`. Produção não foi acessada
  nem alterada.

#### Follow-up — identidade entre caixas e atribuição — implantado no SERVIDOR05 em 2026-08-25

- `whatsapp.pn` protocolar agora reutiliza uma identity e um `mail.guest` por empresa. A
  pessoa pode conversar simultaneamente com várias caixas; cada conta conserva um
  `mail.channel`, equipe, responsável e estado próprios. LID/JID/PSID/IGSID continuam
  account-scoped e empresas permanecem isoladas.
- A migração reconcilia duplicatas legadas somente quando todos os blockers passam.
  Memberships ativas convergem para o guest sobrevivente; autores, reações e demais
  referências históricas mantêm o guest original.
- O WuzAPI normaliza sinais Meta/Click-to-WhatsApp em `AttributionDTO`. O base captura o
  touchpoint antes da projeção da mensagem, deduplica, enriquece sem sobrescrever
  conflitos e liga mensagem/canal/identity quando disponíveis. Conteúdo não suportado
  ainda pode produzir evidência de atribuição.
- O ledger técnico é append-only e administrativo. Cada conta pode habilitar uma
  projeção segura e paginada no painel do operador; URLs, IDs externos e extensões do
  provider ficam fora dessa API e do bus.
- Naquele marco, o futuro `contact_center_crm` consumiria e vincularia os touchpoints a
  `crm.lead`/oportunidades sem adicionar dependência de CRM ao `contact_center_base`.
- Versões implantadas no laboratório: base `16.0.1.19.1`, WuzAPI `16.0.1.16.0` e UI
  `16.0.1.16.0`.
- Validação isolada: **225/225** testes base e **345/345** integrados; QUnit UI
  autenticado: **68/68**, 586/586 asserções, em minificado e `debug=assets`, sem erro ou
  warning de console.
- A migração deixou 55 PNs observados em várias caixas convergidos para uma identidade;
  19 identities já possuem conversas em mais de uma caixa. Não restou binding ativo ou
  membership apontando para identity/guest aposentado, e a autoria histórica permaneceu
  intacta.
- Replay controlado de três webhooks comerciais gerou dois touchpoints canônicos, com
  três vínculos de evidência (`1 + 2`). A recaptura foi idempotente, ambos os
  touchpoints ligaram mensagem/canal/identity e a projeção opt-in foi validada em dois
  canais sem expor URLs, IDs externos ou extensões do provider.
- Evidência consolidada em `reviews/2026-08-25-cross-inbox-attribution-validation.md`.

#### Limites preservados

- O opt-in de grupo permanece desligado por padrão.
- Histórico antigo de receipts e mutações não é reproduzido automaticamente.
- As contagens representam receipts observados, não o total de membros do grupo.
- Gestão de participantes continua fora do escopo.
- Produção permanece fora do escopo até aceite explícito.

### Fase 6 — Meta Messenger e Instagram — iniciada em 2026-08-25

> Decisão vigente em 2026-08-31: a especificação normativa é a seção
> **`contact_center_meta` — arquitetura greenfield vigente**. Os marcos abaixo registram
> a evolução do laboratório e suas evidências; não autorizam manter modelos, rotas,
> tabelas, jobs ou dupla escrita das implementações substituídas.

#### Fase 6.0 — acesso dedicado e fixtures — infraestrutura provisionada, aceite parcial

- Criar um Meta Business App exclusivo do Contact Center, sem alterar o app legado de
  Lead Ads.
- Vincular uma Page de laboratório e um Instagram profissional, com identidade técnica,
  permissões mínimas e tokens separados por ambiente.
- Validar por leitura app mode, Graph version, token, scopes, Page tasks, ativos e
  subscriptions; depois configurar callback/subscriptions em mudança explícita.
- Capturar fixtures anonimizadas de remetente externo sem app role.

Estado em 2026-08-25: criado o Meta Business App dedicado `Soloz Contact Center`, fixado
em Graph `v26.0`, sem alterar o app legado de Lead Ads. Foram vinculados como caixas
distintas a Page `Soloz Industrial - Estrutura Solar` e o Instagram profissional
`@solozindustrial`, compartilhando uma autorização restrita no Odoo. Os callbacks de
Messenger e Instagram foram validados. Em 2026-08-26 o read-back confirmou `messages`,
`message_echoes`, `message_deliveries`, `message_reads`, `message_edits`,
`message_reactions`, `messaging_postbacks` e `messaging_referrals` na Page/Messenger; no
Instagram, `messages`, `message_reactions`, `messaging_postbacks`, `messaging_referral`
e `messaging_seen`. O token emitido cobre `pages_show_list`, `pages_manage_metadata`,
`pages_messaging`, `instagram_basic` e `instagram_manage_messages`, com escopo granular
aos dois ativos. `pages_read_engagement` é capacidade opcional de metadados, não
pré-requisito de mensageria: as conexões ficam `connected + metadata_limited`. Ainda
permanecem externos ao piloto a credencial operacional gerenciada por System User,
Live/App Review/Advanced Access/Business Verification e a prova com remetente real sem
App role. Segredos e tokens não entram no plano nem no repositório.

Aceite: Facebook e Instagram aparecem como ativos distintos, o token técnico é válido e
nenhum webhook ou workflow existente foi substituído.

#### Fase 6.1 — fundação e ingresso durável — concluída no laboratório

- Criar `contact_center_meta`, registro do adapter, modelos App/Authorization, extensão
  da conexão, ACLs/regras por empresa e aba Meta.
- Implementar verificação `GET`, HMAC `X-Hub-Signature-256` sobre bytes exatos, limites,
  sanitizer por allow-list, ledger da entrega e fan-out assíncrono com OCA `queue_job`.
- Roteamento estrito por `object + entry.id/recipient.id`; ativo desconhecido fica
  `unrouted`, nunca cai numa caixa padrão.

Aceite: testes cobrem challenge, assinatura, replay, batch misto, ativo desconhecido,
multiempresa e acknowledge rápido, ainda sem criar mensagem.

Estado em 2026-08-26: a fundação está implantada no SERVIDOR05; callback público
desconhecido retorna 404, os modelos e o menu Meta Apps estão carregados e o job
`root.contact_center.meta_webhook` está registrado. A infraestrutura externa foi
provisionada posteriormente conforme o estado da Fase 6.0. As subscriptions de
mensagem/referral estão aplicadas e confirmadas por read-back; reconciliação automática
e health continuam na Fase 6.5.

#### Fase 6.2 — texto inbound direto — concluída no laboratório

- Normalizar mensagens e echoes de Messenger e Instagram para `EventDTO`, com `mid`,
  reply e endereços PSID/IGSID escopados.
- Reutilizar integralmente guest, identity, `mail.channel`, `mail.message`, bindings,
  equipes, filtros e UI provider-neutral do core.
- Declarar envio, grupo, mídia e mutações como capabilities desligadas até cada fluxo
  ser testado.

Aceite: texto externo cria uma única conversa/mensagem na caixa correta; retry, echo e
eventos fora de ordem não duplicam guest, canal nem mensagem.

Estado em 2026-08-26: `contact_center_meta 16.0.1.1.2`,
`contact_center_base 16.0.1.20.3` e `contact_center_wuzapi 16.0.1.17.0` instalados no
SERVIDOR05. O normalizador cobre Messenger e Instagram, inbound, echo, reply e referral;
reply antes do alvo usa retry sem perder `parent_id`, e jobs de avatar exigem opt-in do
adapter. A infraestrutura externa foi provisionada conforme a Fase 6.0. Um smoke
assinado pelo callback público criou, na primeira tentativa, uma conversa direta e uma
mensagem inbound em cada caixa; o replay do Messenger foi deduplicado e a referral
sintética criou o touchpoint imutável esperado. O follow-up de 2026-08-26 passou a
aceitar também referrals autônomas de Messenger e Instagram: o callback assinado criou
um touchpoint por plataforma, o replay reutilizou a mesma entrega e nenhuma identidade,
conversa ou mensagem artificial foi projetada. Passaram **259/259** testes base e
**433/433** integrados. A validação com usuário real sem role continua pendente.
Evidências consolidadas em `reviews/2026-08-25-meta-external-sandbox-connection.md` e
`reviews/2026-08-26-meta-standalone-referral-validation.md`.

#### Fase 6.3 — mídia e eventos de estado inbound — implementação concluída, aceite externo parcial

- Imagem, áudio, vídeo e PDF com locator privado, download imediato em job, validação de
  MIME/tamanho/hash e anexo privado.
- Seen/read, delivery do Messenger, replies, reaction, edit/unsend e postback conforme
  capability real de cada plataforma.
- Enriquecimento de nome/avatar apenas de forma assíncrona; nenhuma Graph call ocorre em
  `normalize_event()`.

Aceite: fixtures e eventos reais cobrem mídia, URL expirada, retry, exclusão, reaction,
read e as diferenças Messenger/Instagram sem vazamento de URL/token.

Estado em 2026-08-26: `contact_center_base 16.0.1.22.0` e
`contact_center_meta 16.0.1.3.0` estão implantados no SERVIDOR05. O ingresso cobre
imagem, áudio, vídeo e documento/PDF via locator privado, quick/story reply, postback,
reaction, edit de Messenger e Instagram, unsend do Instagram e receipts/seen/read.
Sticker e a transição `image + sticker` convergem; `share`/`ig_post`, story mention e
reel entram como conteúdo interativo neutro sem URL privada no DTO.

Receipts cumulativos possuem cursor durável por conexão + conversa. `mids` e `watermark`
do mesmo evento são aplicados juntos, e um echo criado depois herda o estado
entregue/lido sem replay do webhook. Conversas diretas iniciadas no dispositivo agendam
o enriquecimento assíncrono de nome/avatar, sem esperar resposta do cliente. Download
por CDN assinada independe da validade do Page token; somente a consulta de perfil Graph
exige token válido.

Validação canônica: **278/278** testes base e **485/485** integrados, zero falha/erro;
base ativa e versões validadas. Permanecem para o aceite externo da fase: receber mídia
e cada evento de estado reais pelo sandbox, testar URL expirada real e repetir com um
remetente sem app role. Evidência em
`reviews/2026-08-26-meta-phase6-3-hardening-validation.md`.

Correção de visibilidade em 2026-08-26: o agrupamento da caixa de entrada passou a ser
semeado pelas contas autorizadas do bootstrap, e não apenas pelas primeiras 50 conversas
carregadas. Assim, caixas de baixo volume como Instagram e Messenger continuam visíveis
com contador de conversas carregadas igual a zero. UI `16.0.1.17.2` implantada no
SERVIDOR05; QUnit passou **76/76**, **630/630** asserções nos bundles minificado e
`debug=assets`. Evidência em `reviews/2026-08-26-meta-inbox-visibility.md`.

#### Fase 6.4 — outbound controlado

- Texto e reply de texto passam pela outbox canônica. Mídia e reaction outbound ficam
  para incrementos próprios de capability e validação externa.
- Bloquear fora da janela de 24 horas. Human Agent será capability e ação explícitas
  somente após aprovação, nunca fallback automático.
- Throttling por ativo, `appsecret_proof`, retry de 429/613 e timeout ambíguo sem
  reenvio cego.

Aceite: envio e echo convergem para a mesma mensagem, políticas fecham por padrão e uma
falha de uma Page não bloqueia outra.

Estado em 2026-08-30: implementação concluída no laboratório. Texto direto e reply de
texto de Messenger/Instagram passam exclusivamente pela outbox canônica, usam namespace
de destino estrito, Page ID autorizado na rota Page-linked, referência de reply ancorada
ao binding persistido, `appsecret_proof`, janela de resposta de 24 horas baseada em
evidência inbound persistida e retry específico para 429/613. Timeout ou 5xx depois da
fronteira mutante termina `uncertain` e nunca provoca reenvio cego. Mídia, reaction e
Human Agent continuam capabilities desligadas. Testes cobrem correlação com echo,
rejeição fora da janela, concorrência e isolamento entre autorizações/Pages.

#### Fase 6.5 — health, subscriptions e piloto

- Health em cadência própria para token, expiração, scopes, tasks, ativo, app mode,
  versão e drift das subscriptions, sem uma chamada Graph por conexão a cada minuto.
- Aplicar/verificar subscriptions somente em jobs OCA, com read-back e fencing de
  revisão; nunca durante a request.
- App Review/Advanced Access, Business Verification e teste completo com remetente sem
  role antes do piloto.

Estado em 2026-08-28: health e reconciliação automática estão implementados. Jobs OCA
revisionados/fenced inspecionam token/expiração, App ID/type, scopes granulares, Page
tasks, Page/Instagram vinculados e subscriptions; `apply` executa somente em job e exige
read-back autoritativo. O gate de outbound diferencia Development/Live e exige
credencial `system_user` em Live. O diagnóstico persiste somente classe e estágio
allow-listed, nunca URL, token, resposta ou payload.

O probe real confirmou token PAGE válido, App/type/profile, alvos granulares e
subscriptions do App/Page. Messenger e Instagram permanecem corretamente
`connected + metadata_limited`: a ausência de `pages_read_engagement` limita somente
metadados e não bloqueia inbound/outbound. Antes do piloto Meta ainda são externos: usar
credencial gerenciada por System User, publicar/obter App Review/Advanced
Access/Business Verification e testar remetente sem App role. Evidência atualizada em
`reviews/2026-08-31-meta-health-capability-validation.md`.

Release técnico homologado no SERVIDOR05: OCA `queue_job 16.0.3.0.2`, base
`16.0.1.22.5`, WuzAPI `16.0.1.17.1`, Meta `16.0.1.5.3` e UI `16.0.1.17.3`; **288/288**
testes base e **540/540** integrados, zero falha/erro. QUnit autenticado passou
**76/76** com **630/630** asserções e o viewer **4/4**, **15/15**, ambos em minificado e
`debug=assets`. A interface mostrou as sete caixas, realtime ativo, WuzAPI 5/5 saudável
e Meta 2/2 fail-closed naquele marco histórico, sem erro ou warning no console. O estado
atual de capacidade substitui esse diagnóstico antigo sem reescrever a evidência da
release de 2026-08-28.

Aceite: perda de autorização bloqueia outbound, ingressos continuam auditáveis e o
runbook comprova recuperação sem interferir no Lead Ads.

Fechamento de capacidade outbound em 2026-08-28: o core mantém um deadline durável por
`provider.connection`, reservado atomicamente antes da fronteira externa. Cada adapter
informa apenas o espaçamento mínimo; WuzAPI usa um segundo e o default é zero.
`Retry-After` positivo é limitado a uma hora e compartilhado com comandos irmãos, que
permanecem `pending`, sem snapshot nem consumo de tentativa. WuzAPI e Meta usam fallback
de 60 segundos em rate limit sem header válido. A ordem de locks permanece account →
connection → outbox e nenhum lock atravessa I/O. Teste real com dois cursores provou um
único cruzamento do adapter. Quotas Meta por App, acima do escopo de uma única conexão,
permanecem responsabilidade futura de coordenação no `meta_core`.

#### Fase 6.6 — Instagram Login sem Page — opcional

- Adicionar `graph.instagram.com`, OAuth próprio, token longo de 60 dias e refresh.
- Revalidar capabilities e payloads; não reutilizar silenciosamente a implementação de
  Page-linked Instagram.

### Fase 7 — Pipelines, casos e ponte CRM — backend implantado; UX operacional pendente

- O core fornece vários pipelines por empresa, várias etapas por pipeline e um pipeline
  padrão por caixa. A equipe pode publicar um catálogo permitido/padrão, mas caixas
  exclusivas sem equipe continuam plenamente suportadas.
- `open/resolved` é o estado curto vigente da conversa; `pending` foi removido e os
  registros legados foram migrados para `open`. O Kanban trabalha com
  `contact.center.case`, pois duas propostas na mesma conversa podem estar em etapas
  distintas.
- Toda conversa existente ou nova recebe exatamente um caso padrão; o operador pode
  abrir casos adicionais. Transições possuem revisão monotônica, UUID idempotente e
  histórico append-only.
- O addon `contact_center_crm` cria vínculos explícitos equipe↔equipe, pipeline↔equipe
  CRM, etapa↔`crm.stage` e caso↔`crm.lead`. O vínculo de equipe ativo espelha o roster
  CRM no roster nativo do Contact Center; o vínculo de pipeline, separado, mantém a
  integração de catálogo, casos e leads mesmo se o espelho de roster for desativado.
- Ao vincular o pipeline, o CRM é a autoridade do catálogo de etapas. Mudanças de etapa
  do caso e do lead convergem nos dois sentidos, preservando os modelos e atividades
  nativos do CRM.
- Uma conversa pode originar ou vincular vários leads por seus diferentes casos. O mesmo
  lead também pode receber casos de outras conversas; nenhuma conversa ou lead é
  fundido/destruído.
- A ponte futura de Helpdesk reutilizará os mesmos casos e criará bindings próprios, sem
  adicionar campos Helpdesk ao core nem reutilizar permissões de CRM.
- O rollout final instalou `contact_center_base 16.0.1.30.1` e
  `contact_center_crm 16.0.2.0.1` no SERVIDOR05. O catálogo e a sincronização foram
  validados pelas suítes **387/387** do core e **719/719** integradas.
- Validação do rollout e recuperação segura das duas tentativas anteriores:
  `reviews/2026-09-01-owner-team-pipeline-crm-release-validation.md`.
- O addon e os modelos estão ativos; a associação administrativa das equipes reais do
  CRM permanece explícita. O laboratório não recebeu binding automático porque os cinco
  times Contact Center existentes são caixas técnicas individuais, todos com o mesmo
  supervisor, e não correspondem de forma inequívoca às quatro equipes comerciais.
- Portanto, o backend e seus contratos estão implantados, mas UiDTO de casos/leads,
  deep-links, kanban operacional enriquecido e visão 360° continuam fora desta entrega.
  O inventário de candidatos pode ser automático; ativar bindings reais exige validação
  humana do mapeamento, nunca inferência por nome ou supervisor.

### Marco de estabilização arquitetural — 2026-08-25

- Revisão independente confrontada com o código atual; disposição completa em
  `reviews/2026-08-25-independent-audit-disposition.md`.
- Implementados: ordem canônica de locks, sync paginado, reancoragem de timeline,
  hot-writes de aliases, ordem de receipts, streaming HTTP, limites WuzAPI, guards ORM,
  fallback de realtime, mensagens técnicas, cobertura HTTP/webhook/mídia e recuperação
  limitada de ledgers sem job ativo.
- Parciais ou com solução revisada: classificação de timeout outbound, isolamento de
  mídia por capacidade runtime e limite do corpo do webhook quando middleware anterior
  já o materializou. Foram rejeitados o reenvio por consulta negativa não autoritativa,
  commit intermediário no GET de mídia, expurgo genérico de ledgers e retry crescente
  incompatível com as lanes atuais.
- Backlog deliberado: compactação auditável/opt-in, TTL outbound configurável, tour OWL
  executável por `test-tags` e smokes operacionais externos.
- JobRunner revalidado no laboratório em
  `root:4,root.contact_center.health:2,root.contact_center.media:1`, com os subcanais
  `health(C:2)` e `media(C:1)` observados em execução.
- Retenção do ledger e TTL outbound não recebem defaults arbitrários: serão fases
  próprias, com regra configurável e estado visível antes de qualquer expiração.
- Versões implantadas e homologadas: base `16.0.1.20.0`, WuzAPI `16.0.1.17.0`, UI
  `16.0.1.17.0`, Meta `16.0.1.1.0` e OCA `queue_job 16.0.3.0.2`.
- Passaram **250/250** testes base, **413/413** integrados, QUnit UI **73/73** com
  **616/616** asserções nos dois bundles e JSON viewer **4/4**, **15/15**. O browser
  autenticado exibiu 223 conversas, realtime ativo, **5/5** conexões e zero erro ou
  warning.
- Árvore funcional homologada antes do fechamento documental:
  `b5f997e81af86e304791099e9804fe29b9c7ec1c9ce42a3abf70efb932fd47f7`. Evidências
  canônicas em `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/`:
  `deploy/20260825T153106694730Z`, `test/20260825T151422134428Z`,
  `upgrade/20260825T151908851207Z` e `validate/20260825T154011918820Z`.
- Este marco homologa o alicerce técnico; o aceite operacional do piloto permanece com
  os gates já definidos na Fase 4.

#### Cross-check independente do marco

- O cross-check posterior está disposto em
  `reviews/2026-08-25-independent-audit-cross-check-disposition.md`.
- Corrigidos antes do piloto: corrida de alias sob `REPEATABLE READ`, isolamento de
  falhas opcionais de atribuição, FKs do touchpoint somente depois dos locks canônicos,
  ordem canal/perfil nos recibos de grupo, fallback para grupo sem perfil e teto estável
  do refresh em tempo real.
- O Meta agora descarta somente o `referral` opcional inválido, nunca a mensagem;
  `source_type=ad`, tempo da nova evidência e elevação controlada de `_link_projection`
  foram padronizados.
- Homologação posterior concluída no SERVIDOR05 com base `16.0.1.20.2`, WuzAPI
  `16.0.1.17.0`, UI `16.0.1.17.1` e Meta `16.0.1.1.1`: **255/255** testes base,
  **421/421** integrados e QUnit **75/75**, **625/625** asserções.
- Follow-up de referral autônoma homologado em 2026-08-26 com base `16.0.1.20.3`, WuzAPI
  `16.0.1.17.0`, UI `16.0.1.17.1` e Meta `16.0.1.1.2`: **259/259** testes base e
  **433/433** integrados. A árvore implantada e revalidada tem hash
  `7fd2d1894017b4dfb4e661826b892c557362a70b0ee2a4413a988cfa2960271e`.
- A mídia pausada agora faz preflight fail-closed sem I/O no provider; permanecem para
  hardening/política a resolução administrativa de `uncertain` sem redispatch e métricas
  de crescimento dos ledgers. Retenção/LGPD continuam fora do escopo desta etapa por
  decisão de produto.

#### Enriquecimento de identidade WuzAPI — 2026-08-28

- Base `16.0.1.23.2`: fallback WhatsApp prefere PN confiável formatado a LID opaco e a
  migração converge identity, guest e canais diretos gerenciados sem replay.
- WuzAPI `16.0.1.18.1`: o job de perfil combina avatar com `VerifiedName` obtido por
  `/user/check`; respostas são limitadas, validadas e seguem o retry do `queue_job`.
  Nome opcional inseguro é ignorado sem bloquear avatar ou fallback de telefone.
- Dois casos reais reportados foram validados: um recebeu o nome comercial do provider;
  o outro, sem nome confiável, passou a exibir o PN formatado em vez do LID.
- Homologação no SERVIDOR05: **292/292** testes base e **550/550** integrados, sem
  falhas ou erros. Produção não foi acessada.
- Árvore implantada: `1ba9ecc9bdcc6c8342bf4da13e9e4f78b51af6a68eae024e9cfd2fd12a6ebb34`;
  evidências em `release/20260829T000744740121Z` e `test/20260829T000835183776Z` nos
  diretórios canônicos de hardening.

#### Estabilização da auditoria R3 — 2026-08-28

- A lane canônica de uma mutation de grupo passou a ser o message binding já
  correlacionado. `target_from_me` e participante vindos do provider permanecem
  evidências opcionais e nunca substituem direção/autoria persistidas.
- Edit inbound permanece author-only. Delete inbound aceita também participante ativo
  comprovado como `admin`/`superadmin`; 132 reactions/deletes WuzAPI antes rejeitados
  foram seletivamente reprocessados e terminaram em `done`.
- Texto Meta autenticado sobrevive a anexos não suportados ou locators inválidos. A
  sanitização coloca somente o item/campo inválido em quarentena e mantém evidência
  limitada; flags semânticos inválidos nunca viram mensagem inbound.
- Health/subscriptions Meta são avaliados por autorização/Page. 403 com código de quota
  e todo 429 entram na lane de rate limit; o outbox permanece `pending`, devolve a
  tentativa de sondagem e respeita o deadline compartilhado sem teto artificial.
- Delivery Meta ganhou recovery pelo `queue_job`; naquela versão, evento de conta sem
  equipe ficava em inbox `blocked` e podia ser reprocessado depois da configuração. A
  regra vigente é bloquear somente a caixa sem dono e sem equipe. Conflitos de
  identidade são visíveis ao Administrator dentro da empresa.
- Verify token não ASCII, preservação de nome quando avatar falha, jitter de health,
  ordem canônica dos locks de atribuição, rótulos sociais por plataforma, datetime da UI
  e foco do composer após o gravador foram corrigidos.
- Release homologado somente no SERVIDOR05: base `16.0.1.24.0`, WuzAPI `16.0.1.19.0`,
  Meta `16.0.1.6.0`, UI `16.0.1.17.4`; **295/295** testes base, **565/565** integrados e
  QUnit **76/76**, **634/634** nos bundles minificado e `debug=assets`.
- Árvore implantada: `01eb8395c222e6bf53959c7b53cee365ae89d81794e10d51096d9beb48c5a1e2`.
  Evidências em `scans/raw/20260828-odoo16-contact-center-audit-r3/`; produção não foi
  acessada ou alterada.

#### Separação dos serviços de aplicação — implantada `16.0.1.24.1`

- `contact.center.application` permanece a fachada canônica e conserva nomes,
  assinaturas, DTOs, ACLs, locks e eventos do bus.
- `contact.center.ui.api` foi movido integralmente para `models/ui_api.py`, sem alterar
  seus 19 RPCs públicos ou o formato do UiDTO.
- O fluxo outbound foi isolado em `models/application_outbound.py` por extensão Odoo
  `_inherit = "contact.center.application"`. Helpers compartilhados de grupo continuam
  no core para não criar dependência inbound → outbound.
- A equivalência estrutural confirmou 57/57 métodos da UI e 62/62 métodos da aplicação
  com AST idêntica ao checkpoint homologado; não existe método ausente ou duplicado.
- Um teste de contrato novo exige que o registry componha a fachada outbound e exponha
  todos os RPCs da UI. O laboratório passou **296/296** testes base e **566/566**
  integrados, sem falhas ou erros.
- O runner de laboratório agora recusa testar fonte remota diferente do `tree_hash`
  local e inclui `contact.center.application` no gate de modelos obrigatórios.
- O código do commit `3b323ab`, árvore
  `27f632b9a91f93c0761b2faacffb17f5596c50210439bf105c2d6f04c8383a57`, foi implantado
  atomicamente no SERVIDOR05. Registry instalado, versões e ausência de módulos
  pendentes foram revalidados.
- QUnit passou **76/76**, **634/634** nos bundles minificado e `debug=assets`; o smoke
  autenticado carregou 485 conversas, realtime ativo, saúde `5/7`, composer e o caminho
  de erro do gravador, sem erro ou warning no console.
- Evidências em `scans/raw/20260829-odoo16-contact-center-application-refactor/`; tag
  homologada `16.0.1.24.1-lab`. Produção não foi acessada ou alterada.

#### Remediação posterior ao service split — `16.0.1.24.4`

- A pureza da separação foi confirmada; as correções desta rodada ficaram nos fluxos de
  provider, normalização, identidade, grupos e filas, sem recombinar a fachada.
- Rate limit do health Meta preserva autorização válida; anexo removido pelo sanitizer
  converge para `unsupported`; falha conhecida de participante próprio em mutation de
  grupo recebe retry localizado.
- Delete administrativo de grupo exige snapshot completo e mais novo que o inbox. A
  solicitação do pull também deve ter começado depois do recebimento, fechando a corrida
  em que um request antigo terminava depois. A espera pelo roster é durável, coalesce
  refresh concorrente e libera/reagenda o evento somente depois de uma revisão aplicada.
- Mutations agora preservam nome observado, validam o tipo do binding alvo antes de
  enriquecer aliases, ordenam nomes pelo `occurred_at` do provider e tratam
  `target_from_me` inbound como evidência opcional.
- Leituras WuzAPI de mídia, grupo e identidade classificam 429 na lane sem teto e
  respeitam `Retry-After`, mantendo o contrato canônico do outbox.
- O conflito legado #1 foi resolvido sem merge; o inbox #1263 foi reprocessado e
  convergiu corretamente para `unsupported`.
- Homologação exclusiva no SERVIDOR05: base `16.0.1.24.4`, WuzAPI `16.0.1.19.1`, Meta
  `16.0.1.6.1`, UI `16.0.1.17.4`; **304/304** testes base e **577/577** integrados, sem
  falhas ou erros. Árvore implantada
  `f72f13f1c6a46a6ed1420d6bf60d200c6c20c110f02450a91aeb882ac35e4771`.
- Disposição detalhada em
  `reviews/2026-08-29-service-split-verification-disposition.md`; evidências em
  `scans/raw/20260829-odoo16-contact-center-service-split-remediation/`. Produção não
  foi acessada ou alterada.

#### Fechamento da verificação da disposição — `16.0.1.24.5`

- Rate limit no health Meta e na configuração de webhook WuzAPI usa cooldown com jitter
  e `ignore_retry=True`, sem consumir o teto destinado a falhas reais.
- Rejeições Meta de conteúdo são escopadas ao slot: somente `attachments` e
  `attachment:*` justificam quarentena de uma lista vazia.
- Falta temporária do participante próprio da sessão de grupo continua recuperável;
  evidência persistida inválida ou ambígua falha permanentemente.
- Waiters de autorização por roster ganharam contagem e primeira data visíveis ao
  administrador, teto de 12 tentativas e término `unsupported` quando o escopo deixa de
  existir. Caixas ativas apenas pausadas/desconectadas preservam a espera.
- Aplicar um snapshot válido não depende da liberação imediata dos waiters: a liberação
  é best-effort em savepoint, e o cron faz fallback bounded com
  `FOR UPDATE SKIP LOCKED`. O profile é descarregado antes do predicado SQL e relido
  antes do enqueue, evitando divergência entre cache ORM e recovery.
- Não foi adotado TTL destrutivo para indisponibilidade externa nem removido o reset de
  tentativas por nova revisão do profile; o orçamento canônico do evento fica no inbox.
- Homologação exclusiva no SERVIDOR05: base `16.0.1.24.5`, WuzAPI `16.0.1.19.2`, Meta
  `16.0.1.6.2`, UI `16.0.1.17.4`; **312/312** testes base e **588/588** integrados, sem
  falhas ou erros. Árvore implantada
  `23bb787a9d6fbd8b408633dd04ad7a67819e2a0865f87319fb2b44d0e37749c7`.
- Resposta técnica e refutações em
  `reviews/2026-08-29-service-split-disposition-verification-response.md`; evidências em
  `scans/raw/20260829-odoo16-contact-center-disposition-followup/`. Produção não foi
  acessada ou alterada.

#### Follow-up de convergência dos waiters — `16.0.1.24.6`

- Falha permanente do pull de roster deixa de manter mutations indefinidamente
  pendentes: o profile permanece retryable, enquanto o inbox converge para `dead` e pode
  ser requeued pelo administrador depois da correção. Escopo inexistente continua
  `unsupported`; pausa, desconexão e rate limit continuam recuperáveis.
- O ledger morto preserva evidência sanitizada da causa do profile (classe, revisão,
  tentativa e instante), mesmo depois de uma nova sincronização limpar o erro corrente.
- O fallback global de profiles falhos respeita a ordem `channel -> profile -> inbox`;
  recuperação concorrente nunca produz dead-letter a partir de um snapshot obsoleto.
- Savepoints best-effort capturam apenas o corpo opcional. Falhas de entrada/saída da
  fronteira transacional propagam, e falha SQL parcial no release é revertida com o
  cursor e o cache ORM saudáveis.
- A semântica do teto permanece no orçamento total do inbox. Testes cobrem o ciclo
  `defer -> release -> defer`, preservação dos contadores, limpeza de erro antigo e os
  estados distintos `paused`/`disconnected`.
- Homologação exclusiva no SERVIDOR05: base `16.0.1.24.6`, WuzAPI `16.0.1.19.2`, Meta
  `16.0.1.6.2`, UI `16.0.1.17.4`; **314/314** testes base e **590/590** integrados, sem
  falhas ou erros. Árvore implantada
  `8048beb8044ec74c973c46bf6840e1143718bbeb60b000068c60b970bc170e8f`.
- Disposição detalhada em
  `reviews/2026-08-30-disposition-followup-verification-response.md`; evidências em
  `scans/raw/20260830-odoo16-contact-center-disposition-followup-2/`. Produção não foi
  acessada ou alterada.

#### Topologia de provider e cobertura WuzAPI — `16.0.1.25.0`

- A conta foi consolidada como caixa lógica e as conexões ganharam os papéis `primary`,
  `standby`, `migration` e `historical`. Existe um único ingress primary por conta e,
  quando aprovado, um único egress; não existe balanceamento de carga nem fallback
  automático.
- O cutover administrativo é serializado por conta, bloqueia enquanto houver envio ou
  mutation outbound ativa no transporte removido e usa uma revisão de admissão para
  fechar a corrida de snapshot do PostgreSQL. Binding e comando preservam afinidade
  estrita com o provider de origem.
- A WuzAPI passou a normalizar o catálogo conversacional definido em
  `research/wuzapi-event-coverage.md`, incluindo conteúdo rico, lifecycle, chamadas e
  mudança de identidade. Eventos selecionados ainda sem contrato de produto convergem ao
  ledger `unsupported`, sem inventar conversa ou mensagem.
- Cards de chamada/segurança são imutáveis e provider-neutral. `mark_read` é uma ação
  assíncrona opcional da conta/capability e não interfere no ponteiro local da UI.
- A rotação de HMAC é coordenada com a WuzAPI por job revisionado, aceita as chaves
  current/pending/previous somente durante o corte e elimina a chave anterior após a
  janela de drenagem de cinco minutos.
- Versões implantadas e validadas somente no SERVIDOR05: base `16.0.1.25.0`, WuzAPI
  `16.0.1.21.0`, Meta `16.0.1.7.0`, UI `16.0.1.18.1` e OCA `queue_job 16.0.3.0.2`.
  Passaram **351/351** testes base e **659/659** integrados, sem falha ou erro; QUnit
  passou **77/77** testes e **651/651** asserções em minificado e `debug=assets`.
- A homologação do bundle minificado revelou que o minificador legado do Odoo 16 removia
  um espaço de um template literal aninhado no rótulo acessível dos cards de controle. O
  rótulo agora usa concatenação literal e produz resultado idêntico nos dois modos de
  assets.
- Árvore de módulos implantada:
  `d38536d3d23dc15afa029aa2c95dd4fb4129b350822975feed3c6cca73e26537`. Evidências
  canônicas:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260830T050136876116Z`,
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260830T050455592654Z`
  e
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260830T050341479076Z`.
  Produção não foi acessada ou alterada; essa homologação técnica não substitui o aceite
  operacional do piloto nem os gates externos da Meta.

#### Fechamento de cobertura, reply e timeline — `16.0.1.25.1`

- A WuzAPI normaliza stickers WebP, wrappers efêmeros/view-once e
  `associatedChildMessage` com profundidade limitada. O webhook persiste somente o
  `mediaKey` do conteúdo que o adapter realmente seleciona e remove subárvores
  criptográficas/protocol-only antes do ledger.
- Reply direto Meta usa a rota Page ID autorizada no modo Page-linked e o formato
  superior `reply_to.mid`. O boundary genérico da outbox compara as duas projeções do
  DTO com o `reply_to_binding_id` persistido, impedindo referência forjada mesmo quando
  as cópias do payload são internamente consistentes.
- A timeline possui paginação forward exclusiva e cronológica para recuperar lacunas de
  reconexão. O browser conserva um high-water mark contíguo, não usa mensagens locais
  isoladas como prova de overlap, limita o catch-up automático a 20 páginas e
  recentraliza backlogs maiores sem perder a paginação histórica para trás. Falha
  silenciosa retoma do mesmo cursor com orçamento limitado.
- Versões implantadas somente no SERVIDOR05: base `16.0.1.25.1`, WuzAPI `16.0.1.21.1`,
  Meta `16.0.1.7.1`, UI `16.0.1.18.2` e OCA `queue_job 16.0.3.0.2`. Passaram **353/353**
  testes base e **674/674** integrados, sem falha/erro. QUnit UI passou **80/80**,
  **700/700**, e o viewer **4/4**, **15/15**, em minificado e `debug=assets`, sem erro
  ou warning de console.
- O smoke autenticado carregou 487 conversas, realtime ativo e cinco WuzAPI conectadas.
  As duas Meta permanecem em atenção pelos gates externos já documentados. Árvore de
  módulos implantada:
  `3236382e2d5b333b7f95f0c086b0c02aeaab4aea0be43ecf4b88565881a9bb98`.
- Limite conhecido não bloqueante: `mail.message.id` é cursor de alocação, não de ordem
  de commit. Uma transação excepcionalmente longa pode confirmar um ID menor depois do
  avanço do cursor; o head final repara a janela recente, mas uma garantia absoluta
  exigiria um cursor próprio de commit/projeção e permanece hardening pós-piloto.
- Evidência detalhada em
  `reviews/2026-08-30-provider-coverage-and-timeline-validation.md`. Produção não foi
  acessada nem alterada.

#### Onboarding guiado de caixas — `16.0.1.28.5`

- `Configuração → Adicionar caixa` conduz o administrador por cinco etapas: Atendimento,
  Provedor, Conectar, Revisão e Pronta. A caixa, equipe e preferências ficam no core;
  cada addon injeta apenas seus campos e ações de conexão.
- A implementação base é provider-neutral. O primeiro adapter guiado é a WuzAPI, com
  criação automática ou vínculo de instância existente, token individual, HMAC,
  callback, QR protegido e verificação de sessão/identidade antes da ativação.
- A configuração permanece em papel `migration`, sem ingress ou egress, até a ação
  explícita de ativação. Webhooks recebidos durante o pareamento são preservados como
  bloqueados e liberados por lotes via `queue_job` após o cutover seguro.
- Status de provider é datado no início da leitura remota; lifecycle posterior vence.
  Uma revisão monotônica de ingress força retry MVCC caso ativação e primeiro webhook
  concorram, impedindo promoção a partir de snapshot antigo.
- A interface foi validada em desktop e mobile, traduzida para pt_BR e não expõe o nome
  técnico do transient model. Os campos WuzAPI são validados no servidor ao preparar a
  conexão, permitindo voltar da etapa do provedor ainda vazia; o bundle SCSS compila no
  LibSass do Odoo 16.
- Configurações WuzAPI com falha podem voltar ao passo do provedor e corrigir nome ou
  token sem criar outra caixa; servidor, modo, conexão e referência idempotente
  permanecem fixos. Os botões genéricos do core continuam livres para outros adapters.
- A prova de health do onboarding não conclui um job canônico concorrente. Apenas o
  proprietário do UUID limpa a fila, e observações posteriores restritivas prevalecem. A
  URL gerenciada aceita somente a origem do serviço, sem aliases de path, e tokens de
  instâncias existentes exigem pelo menos 32 caracteres.
- Homologação exclusiva no SERVIDOR05: base `16.0.1.28.5`, WuzAPI `16.0.1.26.4`, Meta
  `16.0.1.9.0`, UI `16.0.1.18.3` e OCA `queue_job 16.0.3.0.2`. Passaram **355/355**
  testes base e **699/699** integrados, sem falhas ou erros; console do navegador sem
  erro ou warning. Árvore implantada
  `eacd92b4df2d07e87a05c6cf7c4bb7d94dd007fd358d3dd80590d1d7b0b7aea4`.
- Evidência detalhada em `reviews/2026-08-30-guided-inbox-onboarding-validation.md`.
  Produção não foi acessada ou alterada.

#### Extração do runtime Meta compartilhado — Meta `16.0.1.10.0`

- O transporte Graph, `appsecret_proof`, limites de resposta e taxonomia técnica de
  falhas passaram ao addon independente `meta_api_base` `16.0.1.0.0`, hospedado no
  repositório técnico `integration-core` e sem dependência de Contact Center ou
  Marketing Center.
- `contact_center_meta.services.graph` permanece como fachada compatível: conserva as
  quatro assinaturas públicas e traduz erros neutros para `AdapterError`,
  `TransientAdapterError`, `AmbiguousTimeoutError`, `ProviderPausedError` e
  `ProviderRateLimitError`. Health, outbox e consumidores existentes não conhecem o
  runtime compartilhado.
- Naquele corte, a extração ainda não incluía o webhook Meta. Assinatura, roteamento e
  delivery ledger permaneceram no caminho legado até a harmonização consciente do
  contrato de versões Graph; o cutover posterior está registrado na seção abaixo.
- Homologação exclusiva no SERVIDOR05: `meta_api_base` `16.0.1.0.0` e
  `contact_center_meta` `16.0.1.10.0`; **355/355** testes base e **701/701** integrados,
  sem falhas ou erros. Árvore Contact Center implantada
  `0f6b8338b760ba8b0022fc395c1b69fdd91a2e596ee20ee86fe4af7d7011e8c9`.
- Evidências canônicas:
  `scans/raw/20260830-odoo16-integration-core-meta-api-base/release/20260831T031224140828Z`,
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260831T032255374569Z`
  e
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260831T032554278909Z`.
  Produção não foi acessada ou alterada.

#### Rastreabilidade da mensagem até o webhook de origem — `16.0.1.29.1`

- `contact.center.message.binding` persiste `source_inbox_event_id` como referência
  imutável ao evento que originou mensagens inbound ou criadas em outro dispositivo. O
  vínculo exige o mesmo provider, conta, empresa e conversa; mensagens compostas no Odoo
  não recebem origem artificial quando o echo do provider chega.
- O backfill histórico associa somente correspondências únicas e comprováveis por ID
  externo ou ID do cliente. No laboratório foram vinculadas **10.148** mensagens reais:
  **8.033** de provider e **2.115** de dispositivo externo, sem atribuir origem a
  mensagens de agente ou automação.
- O DTO e a capability `view_source_webhook` são entregues somente a usuários do grupo
  nativo `base.group_system`. A autorização não depende de `?debug=1`, que qualquer
  usuário interno pode habilitar. Para o administrador do sistema, o menu da mensagem
  abre o formulário exato do Inbox Event em modal; revogação da capability ou ID
  inválido fecha o menu e falha de forma segura.
- Homologação exclusiva no SERVIDOR05: base `16.0.1.29.1`, WuzAPI `16.0.1.26.4`, Meta
  `16.0.1.10.0` e UI `16.0.1.18.4`. Passaram **365/365** testes base, **714/714**
  integrados e **83/83** QUnit minificados com **724/724** asserções. O smoke
  autenticado comprovou o item “Ver webhook de origem” e a abertura do evento correto,
  sem erro ou warning no console. O modo `debug=assets` não chegou a iniciar o QUnit:
  centenas de requisições paralelas de arquivos elevaram a memória até o encerramento
  `137` do container nessa topologia single-process. O navegador de prova foi encerrado,
  os containers e a rota exata do laboratório foram restaurados e voltaram a responder
  HTTP 200; esse modo não é registrado como aprovado.
- Evidência detalhada em `reviews/2026-08-31-source-webhook-link-validation.md`.
  Produção não foi acessada ou alterada.

#### Ingresso Meta compartilhado — ✅ cutover validado no SERVIDOR05 em 2026-08-31

- O `integration-core` implantado no SERVIDOR05 contém `meta_api_base 16.0.1.1.0` e
  `meta_webhook_base 16.0.1.0.1`. O primeiro concentra o transporte e as credenciais
  técnicas do App; o segundo persiste o ledger técnico de entrega e distribui itens aos
  consumidores registrados.
- `contact_center_meta 16.0.1.12.1` é somente o consumidor de domínio: converte cada
  item reivindicado para o ingresso canônico já existente
  `Inbox Event -> EventDTO -> mail.channel/mail.message`. O ledger técnico compartilhado
  não duplica guest, conversa, mensagem ou touchpoint.
- Os segredos ficam montados somente no container Odoo, em leitura; o dbmanager não os
  recebe. O callback compartilhado e suas 13 subscriptions ficaram `in_sync`, as
  conexões Messenger 6 e Instagram 7 foram vinculadas atomicamente aos assets 1 e 2, a
  autoridade ficou `shared` e o ingresso legado foi pausado por último.
- Release canônica final do `integration-core`:
  `scans/raw/20260831-odoo16-integration-core-shared-meta/release/20260831T230007445621Z`,
  árvore `19333942d3430f43c66000e7957feb4a286d263dd9549a66240be44d87db7242`, com
  **36/36** testes do API base e **27/27** do webhook base. O patch
  `meta_webhook_base 16.0.1.0.1` torna o retorno da ação de reconcile serializável por
  XML-RPC.
- O `switch` e uma segunda verificação read-only passaram com health fresco, rotas e
  bindings exatos, jobs fenced e nenhuma entrega na janela de transição. Evidências:
  `scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T230400287480Z`
  e `.../20260831T230933867551Z`. O procedimento e o rollback estão em
  `odoo16/reference/contact-center-meta-shared-cutover.md`.
- Estado consolidado e evidências do corte final em
  `reviews/2026-08-31-phase1-4-completion-validation.md`. Produção não foi acessada ou
  alterada.

#### Visualização inline de PDF — UI `16.0.1.18.8` — ✅ implantada no SERVIDOR05

- O cartão de um documento pronto abre o visualizador interno somente quando o tipo é
  `document`, o MIME normalizado é exatamente `application/pdf` e a URL autenticada
  local contém o mesmo ID da mídia. Outros documentos continuam como download direto.
- O modal existente carrega o PDF.js empacotado no Odoo 16 com a URL de conteúdo
  codificada, preserva o download explícito separado e nunca expõe `ir.attachment`, URL
  do provider, `data:` ou conteúdo inline.
- A rota de mídia e sua política de segurança não foram alteradas: documentos continuam
  com `Content-Disposition: attachment`, ACL/regra de registro, Range, ETag, CSP e
  `nosniff`. Não há schema, migração ou alteração de dados.
- Foram adicionadas regressões QUnit para MIME/estado/ID, URLs externas e divergentes,
  saneamento do download e continuidade de imagem/vídeo. Prettier, ESLint, XML, SCSS,
  compilação dos scripts e `git diff --check` passaram localmente.
- Implantada no release atômico da árvore
  `e3c150627485992500325646a241839a4ea359e7a84b27b113e364afcffac8c1`. Passaram
  **368/368** testes base e **754/754** integrados. QUnit passou UI **92/92**,
  **817/817** asserções, e viewer **4/4**, **15/15**, em minificado e `debug=assets`,
  sem erro de console. O smoke autenticado abriu um PDF real no PDF.js interno por URL
  local autenticada e comprovou uma página renderizada. Produção não foi acessada.
- Revisão detalhada em `reviews/2026-08-31-pdf-inline-viewer-implementation.md`.

#### Health Meta por capacidade — base `16.0.1.29.4`, Meta `16.0.1.12.1`, UI `16.0.1.18.8` — ✅ implantado

- A conexão operacional não depende mais de `pages_read_engagement`. Essa permissão é
  tratada como capacidade opcional para nomes, tarefas, vínculo Page/Instagram e outros
  metadados; sua ausência gera `connected + metadata_limited`, sem bloquear inbound ou
  outbound.
- A identidade é comprovada sem ler conteúdo da Página: `debug_token` deve confirmar o
  App, token `PAGE`, `profile_id` da Página e os `target_ids` granulares de Messenger e
  Instagram. O readback de `subscribed_apps` continua provando a instalação e os campos
  de webhook no ativo configurado.
- Falhas técnicas antes de uma comparação conclusiva passam a ser `identity_unverified`;
  `identity_mismatch` fica reservado para uma divergência real. A UI usa “ativo”, não
  “número”, para manter o texto válido em WhatsApp, Messenger e Instagram.
- A release `16.0.1.18.8` manteve conexões com metadados limitados no total conectado,
  mas ainda as colocou como aviso operacional no filtro Atenção. O painel passou a se
  chamar “Canais do atendimento”.
- Após o feedback operacional, a UI `16.0.1.18.9` retirou `metadata_limited` do filtro
  Atenção e deixou de degradar o indicador da frota. O diagnóstico opcional permanece no
  payload de health; a correção foi promovida ao laboratório no release final de
  2026-09-01.
- Conferência autenticada no Meta App confirmou a Página configurada, os oito eventos
  Messenger e os cinco eventos Instagram assinados. Nenhum token foi regenerado e
  nenhuma permissão adicional foi solicitada, pois a configuração essencial já estava
  correta.
- Implantado e homologado no SERVIDOR05 no release final da árvore `e3c150627485...`; as
  sete conexões foram observadas conectadas e o painel permaneceu sem erro de fundação.
  As suítes finais são as mesmas **368/368** base e **754/754** integradas, com QUnit e
  smoke autenticado sem erro de console.

#### Equipe fixa pela caixa — base `16.0.1.29.4`, UI `16.0.1.18.8` — ✅ implantada no SERVIDOR05

- A equipe deixa de ser apresentada como destino transferível no card do parceiro. O
  painel exibe “Equipe da caixa” como informação somente leitura e mantém editável
  apenas o responsável, quando a capability de supervisão estiver ativa.
- `update_conversation` rejeita qualquer payload com `team_id`, inclusive quando ele
  repete a equipe atual. A atribuição de responsável continua limitada aos agentes da
  equipe definida em `account.default_team_id` naquele marco.
- Naquele marco, a lista de responsáveis falhava fechada quando a equipe estava ausente
  ou desconhecida. A Fase 2.2 substitui essa regra pela união do `owner_user_id` com o
  roster da equipe, sem nunca expor o cadastro global de agentes. O store também
  revalida `manage_assignment` antes do RPC.
- A troca administrativa da equipe da própria caixa e a reconciliação histórica de
  canais são uma operação distinta e não fazem parte deste corte.
- Implantada no mesmo release final e coberta pelas suítes **368/368** base e
  **754/754** integradas. A prova visual confirmou “Equipe da caixa” somente leitura,
  hint de origem, ausência do seletor de equipe e somente o responsável editável; a
  busca de empresa vinculável também foi exercitada sem criar ou alterar cadastro. QUnit
  e console passaram conforme o marco acima. Produção não foi acessada.

Esse marco permanece como evidência histórica. A Fase 2.2 amplia a mesma regra fixa da
caixa para aceitar dono individual, equipe de acesso ou a união de ambos.

#### Reinstalação greenfield e provisionamento Meta — ✅ validado no SERVIDOR05 em 2026-09-01

- A primeira reconciliação revelou uma regressão no contrato Graph: os cinco campos do
  objeto `instagram` eram misturados ao POST `/{page-id}/subscribed_apps`, cuja borda
  aceita somente campos do objeto `page`; a Meta rejeitou a instalação inteira com HTTP
  400/code 100. `meta_webhook_base 16.0.1.1.1` separa os planos: a configuração global
  do App continua contendo `page` e `instagram`, enquanto cada Facebook Page recebe
  somente seus oito campos `page`. Há regressão automatizada inclusive para uma
  configuração Instagram-only.
- Releases canônicas:
  `scans/raw/20260831-odoo16-integration-core-shared-meta/release/20260901T040611534230Z`
  (**39/39** `meta_api_base` e **38/38** `meta_webhook_base`);
  `scans/raw/20260831-odoo16-contact-center-meta-greenfield/release/20260901T040924773312Z`
  (**172/172**, instalação do zero e replay); e
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T051354186268Z`
  (**387/387** base e **719/719** integrados), que sucede o corte intermediário
  `20260901T041305899881Z`.
- Versões finais: base `16.0.1.30.1`, WuzAPI `16.0.1.26.5`, Meta `16.0.2.0.0`, CRM
  `16.0.2.0.1`, UI `16.0.1.18.9`, `meta_api_base 16.0.1.1.0` e
  `meta_webhook_base 16.0.1.1.1`.
- O provisionamento fresco criou as contas e conexões Messenger/Instagram **11/12**,
  deixou as **13 subscriptions** ativas com Page e endpoint `in_sync`, health
  `connected + healthy` e outbound habilitado. Evidência:
  `scans/raw/20260831-odoo16-contact-center-meta-fresh-provision/20260901T041207125457Z`.
- Dois replays preservaram os mesmos IDs e retornaram `created=false`, sem caixas ou
  subscriptions duplicadas. O último confirmou ambas `healthy` e sem job pendente em
  `scans/raw/20260831-odoo16-contact-center-meta-fresh-provision/20260901T042054598156Z`.
- A prova autenticada da UI exibiu as caixas “Meta API - Instagram @solozindustrial” e
  “Meta API - Messenger Soloz Industrial” como conectadas, com todos os RPCs 200 e zero
  erro de console. Em todos os cortes as conexões WuzAPI e as tabelas/links opcionais do
  Marketing Center foram preservados. Produção não foi acessada.

#### Atualização realtime resiliente — UI `16.0.1.19.1` — ✅ implantada no SERVIDOR05

- A resolução de `busService.start()` não é mais tratada como prova de WebSocket aberto.
  O store segue os eventos nativos `connect`, `reconnect`, `reconnecting`, `disconnect`
  e `notification`, exibindo um estado de transporte verdadeiro.
- O bus continua sendo a via imediata. Uma reconciliação RPC limitada a cada 30 segundos
  permanece ativa como rede de segurança em qualquer estado, recuperando invalidações
  perdidas por suspensão do navegador, troca de SharedWorker ou reconexão. Enquanto não
  estiver online, o comando idempotente de início do Worker também é repetido.
- Duas regressões QUnit cobrem o ciclo completo e duas janelas no mesmo bus. O release
  passou **396/396** core, **203/203** WuzAPI, **729/729** integrados e QUnit **95/95**,
  **856/856** asserções. Duas sessões autenticadas reais atualizaram lista, conversa e
  timeline em até dois segundos após um evento controlado, sem erros de console.
- Evidência detalhada em `reviews/2026-09-01-realtime-inbox-refresh-validation.md` e
  incidente em
  `odoo16/incidents/2026-09-01-odoo16-contact-center-stale-realtime-inbox.md`. Produção
  não foi acessada ou alterada.

#### Identidade person-first e contexto empresarial — corte de fonte `16.0.1.32.0` / `16.0.1.20.1`

- O vínculo rotineiro aceita somente pessoa. A interface explica essa preferência e,
  depois do vínculo, oferece a empresa principal como segundo nível da identidade.
- Um vínculo direto com empresa existe somente no fluxo explícito de “número central”,
  restrito a supervisor e destinado a um WhatsApp compartilhado que não represente uma
  pessoa fixa.
- Os dois caminhos preservam `mail.guest`, aliases, memberships e autoria histórica. A
  empresa de contexto é resolvida por `commercial_partner_id`, preparando integrações
  futuras com CRM, vendas, faturamento e financeiro sem acoplá-las ao core.
- A classificação fica persistida em `partner_link_kind`; alteração posterior de
  `is_company` não promove nem rebaixa silenciosamente o vínculo e gera aviso de drift.
- O nome vinculado passa a abrir o cadastro nativo da pessoa ou empresa. Busca, criação,
  vínculo e desvínculo continuam passando pela API local e por suas verificações de
  empresa, caixa, papel e concorrência.
- O release `20260901T072204332691Z` passou **398/398** testes no base, **203/203** no
  WuzAPI e **731/731** integrados. A UI publicada passou **100/100** casos QUnit e
  **883/883** asserções. O aceite em navegador confirmou o fluxo guest pessoa primeiro,
  o segundo nível empresarial, a exceção de número central, o modal correto ao clicar no
  nome e o viewport móvel, sem erros de console ou mutações em contatos reais.
- Disposição e escopo de validação em
  `reviews/2026-09-01-person-company-identity-ux-validation.md`.

#### Identificação canônica de mensagens encaminhadas — base `16.0.1.33.0`, WuzAPI `16.0.1.27.0`, UI `16.0.1.21.0` — ✅ implantada no SERVIDOR05

- `MessageDTO` transporta `is_forwarded` e o `forwarding_score` opcional sem expor o
  payload WuzAPI ao domínio. A detecção considera exclusivamente o `contextInfo` da
  mensagem ativa e exige `isForwarded === true`; não procura recursivamente na mensagem
  citada e não deduz encaminhamento apenas pelo score.
- O binding canônico em `mail.message` persiste a evidência. Replays e ecos enriquecem o
  mesmo registro por merge monotônico: o marcador não volta de verdadeiro para falso e o
  maior score observado é preservado, sem duplicar a mensagem.
- A UiDTO publica somente o booleano necessário. A timeline mostra uma linha discreta
  com ícone e “Encaminhada” antes de reply, mídia ou texto; mensagens apagadas não
  exibem o marcador e o score técnico permanece fora do navegador.
- Um evento real já persistido no laboratório foi reproduzido pelo adapter. A mensagem e
  o binding existentes foram enriquecidos no lugar e a interface autenticada exibiu
  corretamente o marcador em desktop e em `390 × 844`.
- O release `20260901T074721225118Z` passou **399/399** testes no base, **204/204** no
  WuzAPI e **733/733** integrados. A UI publicada passou **100/100** casos QUnit e
  **887/887** asserções nos bundles minificado e `debug=assets`, sem erros ou avisos de
  console.
- Evidência detalhada em
  `reviews/2026-09-01-forwarded-message-identification-validation.md`. Produção não foi
  acessada nem alterada.

#### Política por caixa para mensagem apagada — base `16.0.1.34.0`, UI `16.0.1.22.0` — ✅ implantada no SERVIDOR05

- Cada `contact.center.account` possui o flag opcional **Manter conteúdo riscado ao
  apagar**, desligado por padrão. A decisão é copiada para a mutação de delete no
  momento em que ela nasce; alterar a caixa depois não reescreve exclusões anteriores.
- Com o flag desligado, a projeção operacional conserva somente o tombstone “Mensagem
  apagada”: limpa corpo/original, reactions e anexos, encerra mídia pendente como
  `discarded` e impede acesso pelas rotas autenticada e `/web/content`. Ledgers técnicos
  imutáveis continuam preservados para idempotência, correlação e diagnóstico.
- Com o flag ligado, o texto vigente é guardado em snapshot próprio e a timeline mostra
  conteúdo/mídia/reactions atenuados e riscados sob o aviso de exclusão. O corpo
  canônico de `mail.message` continua sendo o tombstone e ações de mensagem permanecem
  desabilitadas.
- Exclusões antigas recebem o estado explícito `legacy`: não são apresentadas como se
  tivessem sofrido um expurgo retroativo que não pode ser comprovado. Um recheck após o
  download do provider impede que uma mídia seja materializada depois de um delete.
- A confirmação da UI descreve dinamicamente a política da caixa. O release atômico
  `20260901T084441053850Z` passou **405/405** testes base, **204/204** WuzAPI e
  **739/739** integrados. QUnit passou **101/101** testes e **931/931** asserções nos
  bundles minificado e `debug=assets`, sem erro de console.
- Evidência detalhada em
  `reviews/2026-09-01-deleted-message-display-policy-validation.md`. Produção não foi
  acessada nem alterada.

#### Caixa prioritária, densidade compacta, fluxo binário e reabertura inbound — base `16.0.1.35.0`, UI `16.0.1.23.0` — ✅ implantada no SERVIDOR05

- A lista passa a apresentar a caixa lógica como metadado primário de roteamento em cada
  conversa. Plataforma e provider permanecem no cabeçalho da conversa selecionada, agora
  acompanhados pelo nome exato da caixa, sem repetir `WHATSAPP` como informação
  dominante em todos os cartões.
- O operador pode alternar entre densidade confortável e compacta. A preferência fica
  persistida localmente no navegador; no modo compacto a coluna é mais estreita, os
  cartões usam menos espaço e os filtros ficam recolhidos sob um botão próprio. O layout
  móvel continua responsivo e em largura integral.
- O estado operacional canônico passa a ser exclusivamente `open` ou `resolved`.
  Registros legados `pending` migram para `open`; DTO, filtros e API deixam de aceitar
  `pending`. O cabeçalho mostra uma única ação contextual: **Resolver** em conversa
  aberta e **Reabrir** em conversa resolvida. Ao mudar de estado, a lista ativa é
  reconciliada imediatamente para não deixar um item incompatível no filtro atual.
- Cada caixa possui o flag opcional **Reabrir conversa ao receber mensagem**, desligado
  por padrão. Quando habilitado, somente uma mensagem inbound genuinamente nova e já
  deduplicada move a conversa de `resolved` para `open`. Replay de webhook, receipt,
  mutation e mensagem `from_me` não reabrem; o responsável existente é preservado e a
  atualização é publicada no bus.
- O release atômico `20260901T091706970057Z`, árvore
  `d248475e0ac582d05d65d877e9edb4c10bc2926ddade1e5d6b0b5ccc7d227870`, passou **408/408**
  testes base, **204/204** WuzAPI e **742/742** integrados. QUnit passou **104/104**
  testes e **949/949** asserções nos bundles minificado e `debug=assets`. A rota pública
  de teste retornou HTTP 200 após o upgrade e foi restaurada com o hash anterior.
- Evidência detalhada em
  `reviews/2026-09-01-inbox-density-two-state-reopen-validation.md`. Produção não foi
  acessada nem alterada.

#### Correções UX críticas — base `16.0.1.35.1`, UI `16.0.1.23.1` — ✅ implantadas no SERVIDOR05 em 2026-09-02

- Notificação fora de foco agora é explícita e opt-in: contador no título, som e
  Notification API somente para inbound, com deduplicação e sem nome ou conteúdo do
  cliente na tela bloqueada. O evento `message_created` passou a transportar apenas a
  direção provider-neutral necessária.
- O composer mantém rascunho, resposta e anexo pronto por conversa. Troca de conversa
  durante upload ou gravação é bloqueada até concluir ou cancelar, evitando perda ou
  associação ao canal errado.
- O estado de envio passou a ser por `channel_id`, permitindo enviar em conversas
  diferentes sem liberar clique duplicado na mesma conversa. Clipboard e drag/drop
  reutilizam o upload canônico e preservam o contrato atual de um arquivo.
- Imagem com falha encerra o skeleton, mostra erro e oferece retry real sem criar botão
  aninhado nem abrir o viewer. A timeline mostra “Ir ao fim” sempre que o operador se
  afasta da mensagem mais recente, mesmo sem contador de novas mensagens.
- Itens deliberadamente não improvisados neste corte: reenvio auditável de outbox
  terminal, presença/typing multiatendente, projeção operacional de casos/CRM e visão
  360 por bridges. Eles exigem contratos próprios e permanecem no backlog priorizado.
- Release `20260902T053039152348Z`, árvore
  `9597aeecf8ee5e0b25f4e67d54a1b4f0d617d978dc6af43d8c7bfd2af17d154a`: **408/408** base,
  **204/204** WuzAPI e **742/742** integrados, comprovados pelos logs persistidos. A
  sessão de entrega reportou QUnit **111/111** com **1010/1010** asserções e smoke
  autenticado de atenção, rascunho A→B→A, desktop e `390 × 844`; porém, o diretório
  canônico do release não preservou log/JSON/screenshot pós-release dessas duas
  execuções. Elas devem ser repetidas com artefato persistente antes do gate de release.
- Disposição completa, inclusive refutações e critérios dos itens adiados, em
  `reviews/2026-09-02-ux-critical-review-response.md`. Produção não foi acessada nem
  alterada.

### Consolidação native-first e UX — decisão de 2026-09-02

Esta seção prevalece sobre roadmaps históricos quando houver divergência. A
contrachecagem independente está registrada em
`reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`.

- Entregue e confirmado em código: atenção fora de foco sem PII, rascunho por conversa,
  envio concorrente isolado por canal, clipboard/drag-and-drop de um arquivo, erro/retry
  de imagem, salto ao fim da timeline, reenvio seguro/auditável de outbox terminal,
  respostas rápidas, nota interna, follow-up, filtros/atalhos e agendamento externo
  durável.
- Ainda pendente: presença de atendentes, filas/SLA avançados, UiDTO/deep-links de casos
  e CRM, visão 360° e relatórios.
- Inbox e outbox possuem máquinas de estado distintas. `blocked`/`unsupported` pertencem
  ao inbox; reenvio trata somente outbox terminal inequivocamente seguro e cria nova
  tentativa ligada à anterior. Estado `uncertain` nunca recebe reenvio cego.
- Nota interna usa `mail.mt_note` sem destinatários, com UI provider-neutral e
  publicação em tempo real sem alterar o cursor de última mensagem da caixa. Sua
  integração opcional com outros domínios permanece responsabilidade dos bridges.
- `contact_center_crm` enriquece apenas dados não-UTM. A atribuição nativa pertence ao
  contrato do Marketing Center; nenhuma escrita UTM direta está autorizada neste core.
- Bindings CRM reais não serão criados por heurística. Primeiro se publica um inventário
  de candidatos; depois uma pessoa valida o mapeamento caixa/equipe/pipeline.
- Git/baseline recuperável é gate de release, i18n é gate antes de ampliar a UI e a
  política de retenção é decisão futura de pré-cutover/outbox. Por decisão de produto,
  LGPD não bloqueia o ciclo atual de desenvolvimento read-only/shadow.

Ordem executiva vigente:

1. fechar ADR/disposição, ownership, baseline reproduzível e scaffold de i18n;
2. concluir confiança de envio: estados honestos e reenvio seguro/auditável;
3. produtividade: respostas rápidas, nota interna, follow-up, filtros/atalhos, rascunho
   e agendamento externo — implantada e validada no SERVIDOR05;
4. multiatendimento: presença/colisão, distribuição, SLA e health acionável;
5. casos/CRM: candidatos de binding, validação humana, UiDTO, deep-links, kanban e
   enriquecimento não-UTM;
6. omnichannel/360°, conteúdo rico, analytics e integrações documentais opcionais.

#### Primeiro corte da consolidação native-first — ✅ implantado no SERVIDOR05 em 2026-09-02

Este corte atualiza o estado de implementação da lista acima. A evidência consolidada
está em `reviews/2026-09-02-native-first-first-implementation-release.md`.

- **CC-SEND:** `contact_center_base 16.0.1.36.0` e `contact_center_ui 16.0.1.24.0`
  implementam reenvio somente para `send_message` inequivocamente morto e não aceito
  externamente. A ação cria nova mensagem, binding e outbox com `retry_of`, preserva a
  origem terminal, revalida ACL, escopo, capability, health, conteúdo, reply e mídia e
  converge cliques concorrentes/idempotentes. Estados `uncertain` ou resolvidos nunca
  recebem reenvio cego.
- **CRM-MAP:** `contact_center_crm 16.0.2.1.0` publica inventário transitório e
  explicável de candidatos equipe/pipeline↔CRM. Coincidência de nome, roster, líder,
  pipeline ou etapas é apenas evidência; criar ou reativar binding exige ação explícita
  de administrador e revalidação sob lock. Nenhum binding é criado automaticamente.
- O release atômico passou **413/413** testes base, **204/204** WuzAPI e **762/762**
  integrados. O inventário CRM possui **50** testes dirigidos. Upgrade offline e replay
  idempotente concluíram sem módulo pendente.
- **Deploy no SERVIDOR05:** aplicado e validado com árvore
  `59e653bcde155f838228424e79e4dcd85343d712f1edbdcc746a7bab4943cd86`; evidência canônica
  em `scans/raw/20260902-cc-send-crm-map-release-r4`.
- **Aceite em navegador:** QUnit autenticado passou **114/114** testes e **1062/1062**
  asserções em minificado e `debug=assets`, sem erro ou warning de console. Smokes
  desktop e de densidade compacta comprovaram o app, o rótulo da caixa, a timeline,
  mídia e a alternância de densidade. O formulário administrativo do inventário CRM
  também abriu com seu aviso consultivo e sem gerar/aceitar candidatos; screenshots em
  `output/playwright/20260902-cc-native-first-release`. O reenvio externo real e a
  aceitação de um binding CRM não foram acionados para não enviar mensagem nem alterar o
  mapeamento operacional: seus contratos foram validados pelas suítes dirigidas.
- Três execuções anteriores do mesmo release falharam fechadas em testes antes do
  upgrade da base principal (duas no base e uma na integração). Em todas, fonte e rota
  foram recuperadas e o HTTP público voltou a `200`; os testes expuseram expectations
  antigas e dois fixtures CRM incompatíveis com a associação mono-time padrão do
  Odoo 16. Os artefatos estão em `20260902-cc-send-crm-map-release`, `-r2` e `-r3`.
- Os gates continuam fechados: nenhuma escrita UTM automática, nenhum cutover de
  tracking e nenhum binding CRM automático. Produção não foi acessada nem alterada.

#### Reconciliação de envio por evidência positiva — ✅ SERVIDOR05 em 2026-09-02

- Receipt exato, eco `from_me` e receipt de participante de grupo encerram uma outbox
  local ambígua quando o provider já comprovou aceite/entrega; essa reconciliação nunca
  chama o provider nem autoriza reenvio cego.
- O ingresso WuzAPI usa fence de leitura em vez de alterar a revisão de topologia a cada
  webhook. A ordem de locks direta e de grupos foi harmonizada com a finalização do
  worker.
- A UI trata `sent`, `delivered` e `read` como evidência mais forte que metadata antiga
  de dispatch e remove o amarelo/aviso no carregamento e no bus.
- A migração conservadora corrigiu três linhas legadas comprovadas e deixou zero
  `uncertain` com delivery positivo e ID externo. Evidência e análise em
  `reviews/2026-09-02-positive-delivery-reconciliation.md`.

#### Produtividade operacional — base `16.0.1.39.0`, UI `16.0.1.26.0` — ✅ implantada no SERVIDOR05

- Respostas rápidas reutilizam o conteúdo nativo `mail.shortcode`; a exposição ao
  atendimento é feita por `contact.center.quick.reply.binding`, com escopo explícito de
  empresa, equipe, caixa ou conversa. Shortcodes históricos permanecem invisíveis até
  receberem binding; não existe backfill implícito.
- Nota interna é uma `mail.message` com subtipo `mail.mt_note`, sem provider, binding
  externo ou outbox. Follow-up reutiliza `mail.activity` no `contact.center.case`,
  preservando o caso como unidade operacional.
- A interface acrescenta filtros e atalhos de atendimento. O rascunho é isolado por
  conversa em `sessionStorage` e persiste somente texto e modo; blobs, tokens de upload,
  reply e anexos não são serializados.
- Mensagem agendada usa intent provider-neutral durável, cancelável e idempotente,
  executado pelo OCA `queue_job`. Ao vencer, revalida autorização e chama o fluxo
  canônico de envio, sem invocar provider diretamente.
- `mail.message.schedule` não atende ao caso externo: ele posterga notificações de uma
  mensagem já criada, não cria a outbox do Contact Center e não atravessa o adapter do
  provider.
- **Evidência final do SERVIDOR05:** release atômico
  `scans/raw/20260902-cc-productivity-release-r3`, árvore
  `3767a84028f4e61bcc278ebd40cd3127117420b5d972fab057e574efc362e7bf`. Passaram
  **432/432** testes base, **204/204** WuzAPI e **783/783** integrados. QUnit passou
  **128/128** testes e **1144/1144** asserções nos bundles minificado e `debug=assets`,
  sem erro ou warning de console. O smoke autenticado desktop/mobile confirmou realtime,
  as quatro ferramentas e a restauração A -> B -> A do rascunho sem enviar mensagem nem
  criar registro; evidência local em
  `output/playwright/20260902-cc-productivity/summary.json`.
- A primeira tentativa do release final (`...release-r2`) falhou fechada em fixtures dos
  testes novos, antes do upgrade. O rollback automático restaurou a fonte anterior e
  HTTP `200`; os fixtures foram corrigidos e toda a suíte passou no release `r3`.
- O gate original de concorrência do catálogo de pipelines foi absorvido pelo hardening
  greenfield de 2026-09-03 abaixo; o marco histórico desta fase permanece como registro
  do risco que motivou a revisão.
- Decisão detalhada em `reviews/2026-09-02-productivity-operational-design.md`. Produção
  não foi alterada.

#### Fundação greenfield e contrato de release — ✅ R5 validada integralmente em 2026-09-03

- Versões publicadas na R2: `contact_center_base 16.0.1.43.0`,
  `contact_center_wuzapi 16.0.1.29.1`, `contact_center_meta 16.0.2.1.1`,
  `contact_center_crm 16.0.2.4.0` e `contact_center_ui 16.0.1.26.3`.
- Pipeline, estágio, caso, equipe e caixa usam um único protocolo de concorrência. A
  ordem canônica do core é conta → equipe → usuário → pipeline → canal → caso; revisions
  e revalidação fecham write skew. O bridge CRM acrescenta seus bindings e autoridades
  sem inverter essa ordem, inclusive na criação do primeiro binding.
- O pipeline padrão e o caso canônico de cada conversa não podem ser arquivados.
  Arquivamentos permitidos passam por ações explícitas, validam referências, links de
  bridges e follow-ups abertos, e `unlink` de caso continua proibido.
- Follow-up usa `mail.activity` com assignee e escopo revalidados sob a mesma topologia.
  Nota interna, agendamento e resposta rápida têm receipts/bindings próprios, sem
  transformar esses registros técnicos em uma segunda conversa ou mensagem canônica.
- O agendamento conserva o `outbound_request_id` imutável que identifica a requisição
  admitida; o tombstone CRM conserva `lead_record_id_snapshot` depois que a FK do lead é
  removida.
- Grants CRM registram proveniência por binding/usuário/grupo. Apenas memberships que o
  bridge comprovadamente criou são `managed` e elegíveis para remoção; permissões
  preexistentes ou explicitamente mantidas continuam sob propriedade humana, e bindings
  sobrepostos são reconciliados antes de revogar um papel.
- O bridge mantém `contact.center.crm.catalog.authority` como fence técnico do primeiro
  binding, com `authority_key`, `crm_team_id` e `authority_revision` no contrato de
  schema. O modelo não recebe menu nem ACL de usuário por desenho: somente serviços do
  bridge o acessam dentro do lock graph canônico.
- O release geral aceita Base apenas a partir da linhagem suportada `16.0.1.25.0` e
  restringe os demais addons ao predecessor/target explicitamente testado. O cutover
  Meta 1.x permanece greenfield e seu script dedicado é mantido funcional contra Meta
  `2.1.1`, Base `1.43.0`, `meta_api_base 1.1.1` e `meta_webhook_base 1.4.0`.
- O preflight inventaria módulos de teste Python importados e fontes QUnit declaradas;
  qualquer arquivo órfão ou queda abaixo do piso QUnit declarado bloqueia o release
  antes de alterar o SERVIDOR05. O piso da R2 foi **141**; a correção R3 eleva-o a
  **144**.
- O refresh em tempo real preserva a cauda já carregada pelo operador quando a lista
  ultrapassa o lote de 200 conversas; atualização incremental não pode reduzir a lista
  nem reiniciar seu cursor silenciosamente.
- A R2 foi implantada no SERVIDOR05 com árvore
  `7a48cea98e7c07c0f3ef6e8cef7b2eac8b267b6fc038654e9a45add2eadc8660`. Passaram
  **493/493** testes base, **210/210** WuzAPI e **878/878** integrados; upgrade offline,
  integridade de 298 arquivos, catálogo instalado, HTTP `200` e restauração byte a byte
  da rota foram comprovados. QUnit completou **141** casos.
- O smoke autenticado R2 confirmou desktop e `390 × 844`, timeline com texto e mídia,
  composer, ferramentas de produtividade e atualização por bus, sem erro ou warning de
  console. O teste realtime revelou que uma tag criada após o bootstrap atualizava a
  conversa, mas ainda não entrava no catálogo local da UI.
- A R3 corrige esse caso por reconciliação incremental e deduplicada do catálogo de
  tags, sem bootstrap completo, e acrescenta três regressões QUnit. Alvo:
  `contact_center_ui 16.0.1.26.4`; demais versões permanecem iguais. O release atômico
  foi aplicado com árvore
  `ec47e213ab78830b1369d0bf8877b6662cb9c0a165ddd8b627b00fa9b22338b5` e 298 arquivos.
  Repetiu **493/493**, **210/210** e **878/878** testes, upgrade `0`, HTTP `200` e rota
  restaurada byte a byte.
- O smoke R3 abriu 651 conversas sem erro ou warning no console. Uma tag criada após o
  bootstrap surgiu automaticamente na conversa pelo bus, refletiu attach/detach sem `F5`
  e foi removida integralmente ao fim. O QUnit do viewer base passou **4/4**; a primeira
  execução do QUnit da UI encontrou sete casos e 17 asserções em fixtures obsoletos:
  capabilities de mídia no formato legado, eventos sem `channel_id` e uma expectativa de
  refresh RPC redundante. O runtime foi preservado e apenas os testes foram atualizados,
  com checks estáticos limpos. Esse foi um gate intermediário da R3, depois fechado pela
  R4.
- A R4 foi implantada com árvore
  `28cc595f52bf9daa4091ded013302f41e200c60eb9c89645299c4ae0dfeb5d79`, preservando as
  versões alvo e os 298 arquivos. Passaram novamente **493/493** testes base,
  **210/210** WuzAPI e **878/878** integrados; upgrade offline `0`, cinco addons
  instalados sem pendência, HTTP interno/público `200` e rota Traefik restaurada byte a
  byte.
- O aceite final QUnit passou em minificado e `debug=assets`: UI **144/144** casos e
  **1248/1248** asserções; viewer base **4/4** casos e **15/15** asserções. Todas as
  execuções tiveram zero falhas, skips e TODOs. O smoke autenticado confirmou
  **Conversas**, **Tempo real ativo** e console com zero erro e zero warning.
- A R5 publica `contact_center_crm 16.0.2.4.1` e o hook de extensão
  `_contact_center_before_tombstone(reason)`. O core o executa dentro da transação,
  depois do lock/fence e da segunda validação de autorização, mas antes de limpar a FK
  viva do lead. Assim, bridges opcionais — em particular o Marketing Center — podem
  revogar projeções derivadas atomicamente com o tombstone, sem criar dependência do
  Contact Center em seus consumidores.
- A R5 foi implantada com status `applied_and_validated`, árvore
  `8396abc8f9bc497db315dfb63dbc0a0330fb8ad2b0995f40ad81a2d935296a8a` e os mesmos 298
  arquivos verificados. Repetiu **493/493** testes base, **210/210** WuzAPI e
  **878/878** integrados, upgrade offline `0`, UI **144/144** com **1248/1248**
  asserções e viewer **4/4** com **15/15** asserções em minificado e `debug=assets`. A
  rota foi restaurada, o endpoint público respondeu `200` e produção não foi acessada
  nem alterada. Evidência:
  `scans/raw/20260903-odoo16-contact-center-greenfield-foundation-atomic-release-r5/release/20260903T231220190392Z/summary.json`.
- Evidência consolidada em
  `reviews/2026-09-03-greenfield-foundation-release-validation.md`. Produção não foi
  acessada nem alterada.

#### Persistência outbound e mídia JPEG-documento — ✅ R6 implantada em 2026-09-03

- `contact_center_base 16.0.1.43.1` remove a reescrita da revisão de topologia das
  operações rotineiras de envio/seen/read e readquire a ordem canônica
  `account -> connection -> outbox` depois do limite externo. A revisão adversarial
  encontrou e o release removeu a inversão específica da finalização de grupo.
- Sucesso comprovado do provider seguido de `SerializationFailure` ou `DeadlockDetected`
  pode ser reconciliado somente no Odoo, em até três transações novas. O caminho exige
  evidência/DTO/binding/escopo/UUIDs coerentes e nunca chama o adapter nem autoriza
  reenvio.
- `contact_center_wuzapi 16.0.1.29.2` trata o caso real de JPEG/PNG transportado como
  documento: após o erro exato `invalid media hmac` em `/chat/downloaddocument`, faz uma
  única tentativa em `/chat/downloadimage`. MIME, teto de imagem, tamanho e SHA-256
  continuam obrigatórios.
- O release atômico R6 implantou a árvore
  `0bde1bec9975bbd80ebbca5affceba4def1c45a5960cb3e7d45d3b6a3e8a3fde`, com **497/497**
  testes base, **212/212** WuzAPI, **884/884** integrados e UI **144/144**,
  **1248/1248** nos bundles minificado e `debug=assets`.
- Onze envios históricos foram reconciliados como `done/sent`, com ID externo e
  participante de grupo, sem redispatch. As duas imagens reportadas foram recuperadas
  como `ready` e servidas com HTTP `200`. A repetição em dry-run provou idempotência.
- Evidência e limites: `reviews/2026-09-03-outbound-persistence-media-recovery.md`.
  Produção não foi acessada nem alterada.

### Depois do piloto

- Novos adapters, como `contact_center_waha`, `contact_center_evolution` e
  `contact_center_telegram`; `contact_center_meta` passa a seguir a Fase 6 deste plano.
- Avaliar integração opcional com o OCA `partner_contact_in_several_companies` para
  contatos promovidos que possuam vínculos profissionais em várias empresas parceiras.
- Bridges opcionais para Discuss e WhatsApp oficial do Odoo.
- Helpdesk, campanhas, importação de histórico e frontend avançado. A ponte CRM saiu
  deste backlog e é tratada na Fase 7.

## Convenções

- Estilo OCA, AGPL-3 e branch `16.0`.
- Código e documentação técnica dos addons em inglês.
- Versionamento `16.0.x.y.z` e testes obrigatórios para cada provider.

## Fechamento greenfield do Base e CRM — ✅ validado em 2026-09-04

O escopo que faltava no pente-fino passou por revisão linha a linha. O baseline remove a
dependência reversa Base → UI, rejeita números JSON não finitos em todos os DTOs, fecha
a proteção de grupos também para ancestrais implícitos e consolida no CRM uma ordem
única de locks, autoridade de permissões, tombstones e snapshots de origem. O racional e
as refutações estão em `reviews/2026-09-03-contact-base-crm-greenfield-review.md`.

O corte pré-produtivo foi executado no código:

1. os helpers de identidade usados apenas por migrations descartadas foram removidos;
2. todas as migrations pré-produtivas do Base, CRM e WuzAPI foram removidas;
3. permanecem somente os helpers de merge e rebind usados pelo fluxo runtime;
4. o contrato passa a aceitar o schema corrente como primeiro baseline produtivo e a
   falhar fechado para origens antigas não suportadas;
5. registros redundantes de upgrade dos menus técnicos foram removidos, preservando
   `base.group_system` diretamente nos `menuitem`.

O CRM também passou a carregar em lote o inventário de mappings e os vínculos de
lead/caso, além de limitar snapshots aos bindings relevantes. Isso remove consultas
quadráticas/repetidas sem deslocar autoridade, locks ou regras de acesso.

O release final instalou Base `16.0.1.43.5`, WuzAPI `16.0.1.29.2`, Meta `16.0.2.1.1`,
CRM `16.0.2.4.6` e UI `16.0.1.26.5`, com árvore
`6554c71d87d0111c5338ef4bb2a2fb2ed4db5afe21376a3b81ca66c77543e59b`. Passaram **505/505**
testes Base, **199/199** WuzAPI e **895/895** integrados; o inventário próprio do CRM
contém **91** testes. QUnit passou em minificado e `debug=assets`: viewer Base **4/4**,
**15/15** asserções; UI **144/144**, **1248/1248**. O upgrade offline terminou com
código `0`, a rota Traefik foi restaurada e o endpoint interno e público respondeu HTTP
`200`. Produção não foi acessada nem alterada (`production_touched: false`). Evidência:
`scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904-final-r4/summary.json`.

Retenção de PII/ledgers permanece gate deliberado para produção: definir finalidade,
prazo, `retain_until`, legal hold e anonimização/criptodestruição antes de ativar purge.
Dedupe e prova operacional não podem ser destruídos por um cron genérico. Arquivos
grandes serão decompostos quando uma mudança revelar uma fronteira estável; tamanho
isolado é risco de manutenção, não defeito atual nem motivo para um split mecânico.

O índice conjunto deste baseline, incluindo os limites com Marketing Center e as
fundações técnicas compartilhadas, está em
`https://github.com/soloztech/marketing-center/blob/16.0/reviews/2026-09-04-greenfield-baseline-cross-repo.md`.
Desde 2026-09-04, `meta_api_base`, `meta_webhook_base` e `google_api_base` permanecem
addons técnicos independentes, mas são versionados fisicamente em
`soloztech/marketing-center`; os nomes técnicos e o schema Odoo não mudaram.

O cutover consolidado foi repetido depois da mudança física. O Contact Center fechou com
`applied_and_validated`, árvore
`83b88b22773067c34e370d3784ec05f1bd0b2b09f08a478230475bbe16bb1f9e`, **505/505** testes
Base, **199/199** WuzAPI, **895/895** integrados e os quatro QUnit previstos. Cada base
Meta compartilhada resolve exatamente uma vez em `/mnt/outros/marketing-center`; a rota
de teste retornou ao SHA-256 original e ao HTTP 200. Evidência:
`scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T124751394613Z/summary.json`.
