# Análise crítica de UX — Central de Atendimento (ui 1.23.0 · base 1.35.0 · crm 2.0.1)

> **Status:** fotografia do baseline anterior ao release UX `20260902T053039152348Z`.
> C1, C4, M1, M2, M4 e a parte de salto de M7 receberam implementação posterior. A
> disposição vigente, as correções factuais e os limites de evidência estão em
> `reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`. Este documento não
> deve ser usado sozinho como backlog atual.

- Data: 2026-09-02
- Revisor: Claude (coordenação; 3 auditores de código por frente + 1 de contexto
  documental + walkthrough autenticado na UI real do laboratório)
- Escopo: experiência do atendente na UI, contra o objetivo declarado — "excelente canal
  de multiatendimento omnichannel, integrado com o Odoo, depois evoluir com os leads e
  kanban de conversa".
- Método: leitura integral do front do `contact_center_ui` (~16,5k linhas JS/XML/SCSS),
  do `contact_center_crm` e da `ui_api.py`; navegação autenticada **somente leitura** em
  `odoo16-teste.soloz.com.br` (SERVIDOR05) com capturas desktop 1600×900 e mobile
  390×844 (evidência em `output/playwright/2026-09-02-ux-review/`, gitignored);
  cruzamento com as validações de 2026-08-28 → 2026-09-01. **Produção (SERVIDOR02) não
  foi acessada nem alterada.**

## Veredito

**A fundação técnica é de nível de produto comercial; a experiência de multiatendimento
ainda não é.** Hoje o app é um excelente _cliente de WhatsApp multicanal_ — tempo real
resiliente, estados de entrega sofisticados, mídia robusta, mobile de verdade,
acessibilidade acima da média — mas quatro lacunas o separam de uma _central de
multiatendimento_: (1) nenhuma notificação fora de foco, (2) falha de envio que pode
ficar invisível (`blocked`/`unsupported` renderizam como "Na fila" para sempre, e não
existe reenvio), (3) nenhum sinal de colisão entre atendentes, e (4) rascunho destruído
ao trocar de conversa. E uma lacuna o separa de _omnichannel_: o atendente não vê o
cliente — nem as outras conversas dele em outras caixas, nem pedidos, nem lead. A ponte
CRM (`contact_center_crm`, ~3,5k linhas, tecnicamente sólida) é **invisível** na UI do
atendente: o payload da conversa não carrega caso, estágio nem lead, e o "kanban"
existente é uma view administrativa de backoffice com 600 casos em uma única coluna
"New", sem nenhum dado de conversa no card.

A boa notícia: o modelo de dados do kanban de conversa **já está pronto e bem
projetado** (1 caso default por conversa, ledger de transições, sync bidirecional de
estágio com CRM). O que falta é superfície de UI e enriquecimento — não arquitetura.

## Pontos fortes (validados em código e ao vivo)

1. **Tempo real maduro e honesto.** Bus tratado como invalidação, não fonte de verdade
   (`contact_center_store.esm.js:34-38`); reconciliação RPC a cada 30 s armada em
   qualquer estado; reemissão idempotente de `busService.start()`; forçar `online` ao
   receber notificação real. Cobre sleep de aba, SharedWorker hand-off e o `connect` não
   reemitido do Odoo 16. Confirmado ao vivo ("Tempo real ativo").
2. **Modelo de entrega raro de ver.** `delivery_state` (evidência do canal) ×
   `dispatch_state` (fila interna) com precedência anti-regressão
   (`contact_center_model.esm.js:1401-1416`) e o estado honesto "Envio incerto — não
   reenviar".
3. **Mídia de primeira linha.** `DeferredImage` com fila global (concorrência 2),
   IntersectionObserver, timeout e cancelamento; player de áudio com velocidade e
   autopause; galeria; PDF inline seguro (pdf.js, só `application/pdf` local);
   sanitização de URL em profundidade.
4. **Disciplina de corrida em toda parte** — contadores de request, revisões de bus,
   cursor repetido detectado, limpeza de projeção ao trocar de canal.
5. **Mobile é layout de verdade** (panes deslizantes, alvos ≥2.75rem, botão voltar,
   fleet-status no header) — confirmado visualmente em 390×844.
6. **Acessibilidade acima da média** para app interno: `:focus-visible` global,
   `aria-pressed`/`aria-current`/`aria-live` consistentes, restauração de foco,
   `prefers-reduced-motion`, `sr-only` real.
7. **Onboarding guiado de caixa genuinamente bom** (5 passos em linguagem de negócio,
   confirmado ao vivo) e **UX person-first real** no painel de contato, com empresa
   central rebaixada a exceção supervisionada.
8. **Estados de erro específicos e recuperáveis** no shell, lista e timeline; gravador
   de voz robusto com erros acionáveis em português (confirmado ao vivo: "Nenhum
   microfone foi encontrado neste dispositivo").
9. **Cobertura de testes desproporcional**: 6,6k linhas de QUnit só no model da UI.

## Achados

### Críticos (P0 de experiência)

**C1. Zero notificação fora de foco.** Não há som, título de aba, favicon badge,
Notification API nem systray (grep exaustivo em `static/src/`; único `audio.play()` é o
player de mídia, `message_content.esm.js:329`). O Discuss nativo é deliberadamente
silenciado para canais contact_center (`channel.py:734-742` retorna `[]`). O atendente
que troca de aba — gesto constante no Odoo — não descobre mensagem nova. Em operação com
SLA, esta é a lacuna mais cara do produto.

**C2. Falha de envio pode ser invisível, e não existe reenvio.** O outbox tem 7 estados
(`queue.py:241-250`), o front mapeia 5 (`contact_center_model.esm.js:128-164`);
`blocked` e `unsupported` não propagam `delivery_state=failed` (`queue.py:2989-2999`) e
renderizam como "○ Na fila" para sempre — indistinguível de envio iminente. O atendente
acha que respondeu; não respondeu. E para `failed`/`dead` reais não há ação de reenvio
em lugar nenhum (`ui_api.py` não tem `resend_message`; menu da mensagem só oferece
responder/reagir/editar/apagar/webhook) — texto precisa ser recopiado da bolha; áudio
gravado é irrecuperável.

**C3. Nenhum sinal de colisão entre atendentes.** Zero presença/typing (grep vazio em
todo o front); o único sinal é o badge estático de responsável — ausente justamente nas
conversas não atribuídas, a fila quente. Dois atendentes podem redigir respostas
duplicadas sem se ver. O `FOR UPDATE` do backend protege o dado, não o trabalho
digitado.

**C4. Rascunho destruído ao trocar de conversa.** `message_composer.esm.js:81-88` zera
corpo, anexo já upado (com `media_ref` válido!) e gravação em andamento ao mudar
`selectedChannelId`, sem aviso nem undo. Trocar de conversa e voltar é O gesto do
multiatendimento; em fila de 20 conversas isso é perda de trabalho contínua.

**C5. A ponte CRM não alcança o atendente.** `grep -rn "case|lead|crm"` no
`contact_center_ui` → zero; o manifest não depende de `contact_center_crm`
(`contact_center_ui/__manifest__.py:9`); o payload da conversa (`ui_api.py:1181-1312`)
não tem caso/estágio/lead. Criar lead exige sair do app, achar o caso em Service Cases
(sem deep-link — `contact_center_app.esm.js:20-40` ignora `props.action.context`) e ter
grupo de vendas. Ao vivo: kanban com **600 casos em uma coluna "New"**, cards com nome
do canal repetido (um deles é o JID cru `120363...@g.us`), sem avatar, sem última
mensagem, sem aging. O pipeline existe; ninguém o move, porque quem atende não o vê.

**C6. Sem visão 360° do contato — multicanal com silos, não omnichannel.** O painel
mostra só nome/telefone/e-mail/VAT + empresa (`ui_api.py:728-741`). Não há outras
conversas do mesmo contato (mesma pessoa no WhatsApp Comercial e no Suporte = duas
ilhas), nem pedidos/faturas/atividades/leads. Não existe view de `res.partner` em todo o
repo (nenhum smart button "Conversas" no Contatos). O contato aberto pelo painel via
modal é o form nativo — bom — mas a via de volta não existe.

### Altos

**A1. Sem respostas rápidas/templates/canned responses** (grep vazio). O maior
multiplicador de produtividade ausente; toda saudação e instrução é redigitada.

**A2. Fila e SLA inexistentes como eixo.** Ordenação única `last_message_at desc`
(`ui_api.py:1704`): a conversa mais antiga sem resposta — a que viola SLA — afunda.
Auto-atribuição é um usuário fixo por caixa (`account.py:735-754`); sem round-robin,
carga ou disponibilidade. Sem filtros por equipe/tag/estágio
(`conversation_list.xml:79-152`).

**A3. Agente comum não transfere conversa e a UI não explica.** `manage_assignment` é
supervisor-only (`ui_api.py:1372`); o select fica cinza sem motivo
(`contact_panel.xml:655`); "Assumir" só aparece sem responsável. Pausa, turno e
escalonamento dependem de canal externo.

**A4. i18n zero.** Nenhum `_t()` no front (grep vazio), ~180 strings pt-BR hardcoded em
XML/JS, sem diretório `i18n/` no addon; e o chrome mistura idiomas — "Service Cases",
"Operations", "Inbox Owner", "New" convivendo com "Caixa de entrada" (confirmado ao vivo
nos menus e no kanban).

**A5. Sem nota interna e sem follow-up.** Nenhum `mail.activity` nem chatter no repo
inteiro (grep vazio); o caso é um registro mudo (`pipeline.py:879-882`). Não há como
registrar contexto interno sem mandar para o cliente, nem agendar "retornar amanhã".
Para gestão de leads, follow-up é o núcleo do trabalho.

**A6. Conteúdo rico do WhatsApp achatado em texto.** Localização vira coordenadas cruas,
contato vira "Contato: Fulano" sem ação, enquete/botões viram texto, sticker é
renderizado como foto emoldurada (`adapter.py:1326-1650`; front ignora esses
`content_type`, `message_content.esm.js:62-80`). Visível ao usuário em cada conversa.

**A7. Lead nasce pobre e a atribuição de anúncio morre no painel.** Criação grava 7
campos — sem `phone`, `email_from`, `description`, `user_id: False`
(`contact_center_case.py:348-358`), apesar de `_suggested_phone` existir
(`ui_api.py:522-549`). UTM/CTWA capturados em `attribution.py:212-216` nunca chegam a
`crm.lead.source_id/medium_id/campaign_id` (grep utm em `contact_center_crm/` vazio). E
`unique(case_id)` + conversa perpétua = 1 lead por cliente para sempre pelo caminho
padrão (`contact_center_case.py:90-96`) — o modelo suporta casos não-default, mas
nenhuma UI os cria.

### Médios

- **M1.** `state.sending` é global do store: envio lento na conversa A bloqueia o
  composer de todas as outras (`message_composer.esm.js:241`,
  `contact_center_store.esm.js:400,1780`). Anti-padrão de multiatendimento.
- **M2.** Sem colar imagem do clipboard e sem drag-and-drop (grep vazio) — o print de
  tela, gesto padrão de suporte, exige salvar em disco e navegar no picker.
- **M3.** Um anexo por vez, sem `multiple` (`message_composer.esm.js:53,510,539`); a
  timeline já sabe renderizar galeria — assimetria desnecessária.
- **M4.** Mídia com erro fica em shimmer infinito: `DeferredImage` seta `status="error"`
  mas o template não tem ramo de erro (`deferred_image.esm.js:205-247`,
  `xml/deferred_image.xml`) — mídia expirada do WhatsApp cai exatamente aqui, sem retry
  nem diagnóstico.
- **M5.** Contador de não lidas por caixa soma só as ~50 conversas carregadas
  (`conversation_list.esm.js:100-107`); o número visível subestima a fila.
- **M6.** Filtro de caixa inalcançável na visão agrupada (default) — o select só
  renderiza em visão plana (`conversation_list.xml:141`).
- **M7.** Sem scroll infinito (botões manuais na timeline e na lista) e sem "voltar ao
  fim" quando não há mensagens novas (`conversation_timeline.xml:373-374`) — e longe do
  fim o `markSeen` nunca dispara.
- **M8.** Sem atalhos de teclado de navegação (próxima conversa, focar busca, resolver,
  assumir). A base de teclado local existe e é boa.
- **M9.** Form do caso não abre a conversa (`pipeline_views.xml:220-304` só tem o m2o
  técnico de `mail.channel`); smart button do lead leva ao caso, não à conversa
  (`crm_lead.py:128-146`).
- **M10.** Zero relatórios/analytics no repo (nenhum `<graph>`/`<pivot>`): sem tempo de
  primeira resposta, resolução, volume por agente/canal, conversão → lead. A
  matéria-prima existe (`opened_at`, `stage_changed_at`, `closed_at`, ledger).
- **M11.** Pré-condições do vínculo CRM viram `ValidationError` sobre um menu que o
  vendedor não enxerga (admin-only, `contact_center_crm/views/menus.xml:3-9`).
- **M12.** Sem edição inline de telefone/e-mail no painel (só `<dd>` estático,
  `contact_panel.xml:171-192`); corrigir um e-mail = form completo em modal.
- **M13.** Sem eventos de grupo na timeline (entrou/saiu/renomeou —
  `control_events.py:10-17` é fechado em call/segurança); participantes aparecem e somem
  sem explicação.
- **M14.** Health popup com aritmética confusa ao vivo: "7/8 conectadas · verificando
  5" + chips "1 Login necessário / 5 Verificando / 7 Conectada" somando mais que o
  total; e do estado "Login necessário" não há caminho direto para a reconexão (QR) — só
  "Verificar".
- **M15.** Perda de tempo real informada mas não acionável (span sem botão de
  reconectar; espera de até 30 s sem feedback).
- **M16.** Sem indicador de digitação para o cliente (o cliente reenvia a pergunta) —
  depende de capability do provider, mas nem o esqueleto existe.

### Baixos

- Preferência de visão agrupada/plana e grupos colapsados não persistem (densidade
  persiste — inconsistente); painel `detailsOpen` decidido uma vez sem listener de
  resize (`contact_center_store.esm.js:403`); waveform de áudio fake (24 barras
  constantes); popover de exclusão sem focus trap (`role="group"`); `dayLabel` usa TZ do
  browser enquanto bolhas usam TZ do Odoo; lightbox mostra filename-hash
  (`ACCB42...jpg`, confirmado ao vivo); vídeo sem poster (retângulo cinza "Abrir vídeo",
  confirmado ao vivo); contraste marginal em texto de 0,56rem; conversa preservada
  artificialmente vai ao fim da lista (`store:872`); badge de caixa repetido em todo
  card mesmo com uma única caixa (ruído visual, confirmado ao vivo); "Ver webhook de
  origem" no menu do atendente é vocabulário técnico; sem dark mode; hint "Enter envia ·
  Assinada como" só visível com foco; Ctrl+Enter na edição vs Enter no envio.

## O caminho para o kanban de conversa e leads

O diagnóstico honesto: **falta apresentação, não modelagem.** `contact.center.case` +
estágios + ledger + sync CRM bidirecional já existem e são sólidos. Para o kanban dos
sonhos:

1. **Ponte UI↔caso/lead** — expor caso/estágio/lead no payload da conversa e no painel
   (seletor de estágio + "Transformar em lead" dentro do atendimento). Sem isso o kanban
   sempre estará desatualizado, porque quem atende não o alimenta.
2. **Deep-link nos dois sentidos** — `ContactCenterApp` aceitar `channel_id` do contexto
   da ação; botão "Abrir conversa" no caso e no lead.
3. **Card de kanban que pareça uma conversa** — avatar, última mensagem, não lidas,
   canal, tags, aging/SLA (campos related/computed no caso).
4. **Lead enriquecido** — telefone/e-mail/descrição/vendedor na criação + UTM da
   atribuição para `source_id/medium_id/campaign_id`.
5. **"Novo atendimento nesta conversa"** — criar caso não-default para destravar o
   cliente recorrente.
6. **`mail.thread` + `mail.activity` no caso** — nota interna, seguidores, follow-up.
7. **Smart button em `res.partner`** + histórico cross-canal no painel — o passo que
   transforma multicanal em omnichannel.

## Roadmap recomendado (3 ondas)

**Onda 1 — Confiança e produtividade (dias, não semanas):** C2 (mapear
`blocked`/`unsupported` + reenvio), C4 (rascunho por conversa — incluindo `media_ref` já
upado), C1 (título da aba + som + Notification API), A1 (respostas rápidas), M1 (sending
por conversa), M2/M3 (colar/multi-anexo), M7 (jump-to-bottom).

**Onda 2 — Multiatendimento de verdade:** C3 (presença/colisão por conversa), A3
(transferência com fluxo de pedido ao supervisor), A2 (ordenação por espera + filtros
por equipe/tag + contadores server-side + round-robin), A5 (nota interna), M8 (atalhos),
M14/M15 (health e tempo real acionáveis).

**Onda 3 — Omnichannel + CRM (o kanban):** C5/C6 e a lista da seção anterior, A4 (i18n
antes de crescer mais — cada onda adiciona strings), A6 (localização/contato/sticker),
M10 (analytics de SLA e conversão).

## Contexto operacional que condiciona a UX (não é defeito de código)

- Meta segue fail-closed no lab (falta `pages_read_engagement`, Live mode, System User)
  — atendente poderá ver conversa Meta e não poder responder; outbound Meta é só
  texto/reply por decisão de fase.
- 276 jobs `failed` e 1 100 inbox events `dead` acumulados no lab (liberação de
  2026-09-01) — triagem pendente.
- Conexão "teste" em papel `migration` com login necessário — visível no health popup
  durante o walkthrough.

### Errata da contrachecagem

- `pages_read_engagement` é capability opcional de metadados e não deve bloquear o
  health de mensageria por si só. Readiness Meta precisa ser avaliada por ativo, perfil
  e operação efetiva.
- As contagens de 276 jobs `failed` e 1.100 inbox events `dead` são fotografia daquele
  walkthrough. Devem ser recontadas e classificadas antes de virar gate atual.

## Evidência

- Capturas do walkthrough (29 imagens, desktop + mobile):
  `output/playwright/2026-09-02-ux-review/` (gitignored, local).
- Relatórios por frente (shell/inbox, conversa, integração CRM, contexto documental)
  consolidados neste documento; evidências file:line conferidas por leitura direta.
- Nenhuma escrita foi feita no banco do laboratório: navegação, filtros e aberturas de
  registro apenas (o rascunho digitado no composer não foi enviado e foi limpo; nenhum
  wizard foi salvo).

## Complemento (2026-09-02)

Esta análise foi consolidada com o plano native-first do marketing-center (e sua segunda
opinião de 2026-09-02) e com o benchmark do portfólio RD Station
(Marketing/CRM/Conversas) em um plano unificado de execução por trilhas:
`reviews/2026-09-02-suite-roadmap-rdstation.md`. Em particular, o achado A7 (UTM → lead)
fica condicionado ao contrato de projeção do marketing-center
(`first_trusted_assignment`, fill-only atômico) — não deve ser corrigido com escrita
direta.
