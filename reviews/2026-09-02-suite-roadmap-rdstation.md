# Plano consolidado da suíte Soloz — régua RD Station (2026-09-02)

> **Status após contrachecagem independente:** benchmark funcional orientativo, não
> especificação nem evidência de arquitetura. A ordem executiva original deste arquivo
> foi corrigida e é substituída pela Seção 6 e por
> `reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`. Os planos canônicos
> `contact-center/plan.md` e `marketing-center/plan.md` prevalecem.

- Data: 2026-09-02
- Autor: Claude (validação e consolidação)
- Insumos:
  1. `reviews/2026-09-02-ux-critical-review.md` (este repo — análise crítica de UX da
     Central de Atendimento);
  2. `../../marketing-center/reviews/2026-09-01-native-first-adaptation-plan.md` +
     `../../marketing-center/reviews/2026-09-02-native-first-second-opinion.md`
     (veredito: **aceitar com alterações obrigatórias**; só ADR, inventário, correções
     isoladas e shadow podem avançar; sem escrita UTM e sem cutover);
  3. `../../marketing-center/reviews/2026-09-01-contact-marketing-next-stages.md`
     (fronteira de domínios e sequência Google/evidência/atribuição/conversões);
  4. Benchmark do portfólio RD Station (Marketing, CRM e Conversas), levantado em
     2026-09-02.

## 1. Tese

O portfólio RD Station oferece uma comparação funcional aproximada para o stack Soloz.
Não existe mapeamento 1:1: fronteiras, modelo de dados, integração ERP e riscos de canal
são diferentes. A régua ajuda a levantar capacidades esperadas, mas não define
ownership, prioridade nem gate técnico:

| Produto RD           | Equivalente Soloz                                                            | Estado                                                          |
| -------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------- |
| RD Station Conversas | `contact-center` (base/wuzapi/meta/ui)                                       | Fundação forte; UX de multiatendimento incompleta               |
| RD Station Marketing | `marketing-center` + nativos Odoo (website, utm, link_tracker, mass_mailing) | Atribuição/evidência fortes; automação e projeção UTM ainda não |
| RD Station CRM       | Odoo CRM + `contact_center_crm`                                              | Backend pronto; invisível para o atendente                      |

**Vantagem estrutural pretendida no desenho Soloz:** tudo dentro do ERP — a conversa, o
lead, o pedido, a fatura e o recebimento no mesmo banco, sem integração de terceiros. É
o que a atribuição do marketing-center já explora (business events de CRM/Sale/Account)
e o que a visão 360° do atendimento deve explorar.

**Desvantagem estrutural a respeitar:** a RD Station declara ser Provedor de
Soluções/BSP oficial da Meta e operar sobre a WhatsApp Business Platform oficial; nosso
WhatsApp hoje é WuzAPI (não-oficial). Broadcast em massa e automação agressiva de
chatbot elevam risco de ban. A régua RD se adapta: automação conservadora, opt-out
respeitado e escala de campanhas ativas somente com canal oficial no futuro. Pacing em
WuzAPI pode limitar exposição, mas não torna o canal autorizado nem equivalente à Cloud
API.

### 1.1 Fontes e limites do benchmark

As capacidades foram conferidas nas páginas oficiais de
[RD Conversas](https://www.rdstation.com/produtos/conversas/),
[RD Marketing](https://www.rdstation.com/produtos/marketing/),
[RD CRM](https://www.rdstation.com/produtos/crm/) e nos respectivos planos em
2026-09-02. Disponibilidade e profundidade variam por plano. “Lead Tracking” não é
sinônimo de atribuição multitoque; a comparação também não prova Google Ads Data Manager
no produto RD. O aplicativo móvel e as notificações de navegador são capacidades
distintas. A referência de risco do canal atual é o
[README do WuzAPI](https://github.com/asternic/wuzapi), que alerta sobre banimento e
recomenda API oficial para uso comercial.

## 2. Benchmark de capacidades — onde estamos

### 2.1 Régua RD Conversas → Central de Atendimento

| Capacidade RD                                  | Status Soloz                                                                                     | Onde resolver                                         |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Multiatendimento omnichannel (WA/IG/Messenger) | Parcial — WuzAPI operacional; Meta depende das capabilities e credenciais efetivas de cada ativo | Gates Meta por capability, Live mode e perfil técnico |
| Caixas por setor, times, distribuição          | Parcial — caixa/equipe/dono ok; auto-assign = 1 usuário fixo                                     | UX A2: round-robin/carga (Onda 2)                     |
| Respostas rápidas                              | **Falta**                                                                                        | UX A1 (Onda 1)                                        |
| Notificação de nova mensagem                   | **Parcial entregue** — título, som opt-in e Notification API; sem systray/push persistente       | Acabamento posterior; não reimplementar C1            |
| Transferência entre atendentes/setores         | Parcial — supervisor-only, sem fluxo de pedido                                                   | UX A3 (Onda 2)                                        |
| Chatbot / triagem automática / agente IA       | **Não existe** (nem esqueleto)                                                                   | Fase nova pós-piloto (§4, Trilha A4)                  |
| Horário de atendimento + resposta automática   | **Falta**                                                                                        | Onda 2 (config por caixa)                             |
| Broadcast / campanhas ativas WhatsApp          | Fora do MVP declarado ("campaigns/templates outside the pilot")                                  | Fase condicionada a canal oficial (§4, Trilha A5)     |
| Pesquisa de satisfação (CSAT)                  | **Falta**                                                                                        | Onda 3                                                |
| Relatórios de atendimento (TMA/TME/volume)     | **Falta** (zero graph/pivot no repo)                                                             | UX M10 (Onda 3); matéria-prima já existe              |
| Apps/mobile                                    | Parcial — layout web responsivo; sem app/push persistente                                        | Fase própria posterior                                |

### 2.2 Régua RD Marketing → Marketing Center + nativos

| Capacidade RD                           | Status Soloz                                                                              | Onde resolver                                                                                                      |
| --------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Landing pages / formulários / pop-ups   | Odoo Website nativo + bridge próprio                                                      | Native-first Fase 2 — bloqueada pelos bloqueadores 1–2 da segunda opinião (MRO `website_crm`, semântica de clique) |
| E-mail marketing                        | Odoo `mass_mailing` nativo                                                                | Não recriar; integrar segmentos depois do UTM nativo                                                               |
| Fluxos de automação (nutrição)          | **Não existe** (`marketing_automation` é Enterprise)                                      | Decisão futura: OCA `automation_oca` vs motor próprio mínimo sobre `queue_job` — só depois do cutover native-first |
| Segmentação de leads                    | Parcial — tags/utm/listas nativas                                                         | Depois da projeção UTM confiável                                                                                   |
| Lead scoring                            | **Falta**                                                                                 | Baixa prioridade; regra simples pós-fundação                                                                       |
| Lead tracking (jornada multi-touch)     | Ledger de touchpoints auditável já existe; a comparação não prova superioridade comercial | Manter evidência e reduzir produtores duplicados no native-first                                                   |
| ROI / funil por campanha                | Parcial — attribution foundation + dashboard v1                                           | Native-first Fase 4 — bloqueada pelo bloqueador 8 (dependências do dashboard, baseline reproduzível)               |
| Conversões para Ads (CAPI/Data Manager) | Planejado                                                                                 | Next-stages etapa 5, pós-cutover                                                                                   |

### 2.3 Régua RD CRM → Odoo CRM + ponte

| Capacidade RD                                        | Status Soloz                                                                                   | Onde resolver                                                                                      |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Funil kanban / múltiplos funis                       | Backend pronto (caso default/ledger/sync CRM); UI administrativa vazia de contexto             | UX C5 + seção kanban (Onda 3)                                                                      |
| CRM e WhatsApp no mesmo fluxo                        | **Falta** — sem deep-link conversa↔lead nos dois sentidos                                      | UX kanban path 1–2, 7–8; a referência RD usa extensão do CRM no WhatsApp Web, não equivalência 1:1 |
| Tarefas / follow-up                                  | Parcial — backend aceita nota interna na conversa; UI e `mail.activity`/chatter do caso faltam | UI de nota + follow-up no caso/lead                                                                |
| Automação de funil                                   | Parcial — sync bidirecional de estágio caso↔lead existe                                        | Suficiente por ora                                                                                 |
| Lead enriquecido na criação (telefone/e-mail/origem) | **Falta** — lead nasce com 7 campos, sem UTM                                                   | UX A7 **via contrato do marketing-center** (§3)                                                    |
| Metas / relatórios de vendas                         | Odoo CRM nativo                                                                                | Nada a construir                                                                                   |
| Telefonia                                            | Fora de escopo (FreePBX existe na infra como opção futura)                                     | —                                                                                                  |

## 3. Ponto de costura obrigatório entre as trilhas

O achado A7 da análise de UX ("lead nasce pobre; UTM morre no painel") **não deve ser
corrigido com escrita direta** de `source_id/medium_id/campaign_id` pelo
`contact_center_crm`. A segunda opinião do native-first define exatamente o contrato
para isso: mapeamento versionado + `first_trusted_assignment` + fill-only com
assignment/receipt atômico (bloqueadores 3, 4 e 6). A evidência passa por
`marketing_center_contact_center`, converge com caso/lead em
`marketing_center_contact_center_crm` e somente `marketing_center_crm` pode aplicar a
tupla nativa. Assim Website, Lead Ads e Conversas usam uma única mecânica de projeção.
Corrigir A7 antes desse contrato criaria um terceiro escritor concorrente de UTM, que é
precisamente o que a adaptação quer eliminar. Já o enriquecimento **não-UTM** do lead
(telefone via `_suggested_phone`, e-mail, descrição, vendedor) não depende do contrato e
pode ser feito na Onda 3 sem esperar.

Segunda costura: a fronteira de domínios do next-stages permanece — Contact Center não
ganha campanha/gasto/credencial Ads; Marketing Center não envia mensagem. O
chatbot/fluxos (`CC-POST`) é do Contact Center; a automação de nutrição por e-mail
(`MKT-POST`) é do Marketing/nativo. Nenhuma das duas trilhas cria dependência rígida na
outra.

## 4. Plano corrigido — trilhas e gates

### Contact Center

- **`CC-DONE` — implantado:** atenção fora de foco, rascunho por conversa, envio isolado
  por canal, clipboard/drag-and-drop de um arquivo, erro/retry de imagem e salto ao fim.
  Systray, push persistente, multi-anexo e scroll infinito não fazem parte dessa
  entrega.
- **`CC-SEND` — próximo:** motivo honesto de espera/falha e reenvio por nova tentativa
  auditável. Inbox e outbox não compartilham estados; `uncertain` nunca recebe retry
  cego.
- **`CC-PROD`:** respostas rápidas, UI de nota interna, filtro de caixa, transferência
  explicada e atalhos.
- **`CC-MULTI`:** presença/colisão, espera/SLA, agregados server-side, estratégias de
  distribuição, horário de atendimento e health/tempo real acionáveis.
- **`CC-CRM`:** UiDTO de casos, deep-links, caso não-default, kanban enriquecido,
  enriquecimento não-UTM e follow-up.
- **`CC-OMNI`:** outras conversas acessíveis, bridges documentais opcionais, conteúdo
  rico, eventos de grupo, analytics e CSAT.
- **`CC-POST`:** chatbot/triagem conservadora; broadcast compatível somente por canal
  oficial. Qualquer experimento pelo canal não-oficial exige aceitação explícita do
  risco e continua sem equivaler a conformidade.

Capacidade Meta deve ser verificada por ativo/perfil e por operação de mensageria;
`pages_read_engagement` isoladamente não é health de mensagens. As contagens de 276 jobs
`failed` e 1.100 inbox events `dead` eram snapshot histórico do laboratório: recontar e
classificar antes de tratá-las como bloqueio atual.

### Marketing Center native-first

- **`MKT-N0` — autorizado:** ADR/disposição, inventário, baseline reproduzível, menus
  técnicos e flags sem escrita operacional.
- **`MKT-N1`:** corrigir MRO cooperativo de `website_crm` com `HttpCase` e formalizar o
  contrato de clique nativo sem prometer um registro por clique físico.
- **`MKT-N2`:** mapping UTM versionado e assignment/receipt atômico somente em shadow,
  com CAS/lock, preimage, after-image, fencing e tombstone de override humano.
- **`MKT-N3`:** validar causalidade pedido–linha–fatura–reconciliação, dependências/ACL
  do dashboard, dry-run/backfill e go/no-go. Escrita UTM e cutover continuam bloqueados
  até o aceite desta etapa.
- **`MKT-READ`:** Google read-only e captura first-party já têm entrega substancial;
  restam paridade declarada e evidência externa real, não uma implementação do zero.
- **`MKT-POST`:** dashboard native-first definitivo, CAPI/Data Manager e decisão sobre
  automação de nutrição, somente após cutover.

### CRM

- **`CRM-MAP`:** gerar inventário de candidatos entre equipes/pipelines. Ativar um
  binding real requer validação humana; nomes e supervisores não provam equivalência.
- **`CRM-UI`:** executar `CC-CRM`, preservando a independência do UI core.
- **`CRM-ACT`:** expor nota interna na UI e adicionar follow-up via
  `mail.thread`/`mail.activity` no modelo adequado, sem duplicar atividade do lead.
- **`CRM-UTM`:** depende de `MKT-N3`; providers/bridges propõem e somente
  `marketing_center_crm` aplica a tupla nativa.

### Fundação transversal com gates proporcionais

- baseline Git recuperável nos três repositórios (`contact-center`, `marketing-center`,
  `integration-core`) é gate de release/cutover, não de cada iteração no servidor05;
- i18n é gate antes de ampliar significativamente a UI;
- lifecycle de identificadores protegidos é decisão de pré-cutover/outbox. Conforme a
  decisão de produto vigente, LGPD não bloqueia o desenvolvimento atual
  read-only/shadow.

## 5. Bloqueios explícitos

| Item                                                 | Bloqueio                                       | Desbloqueio                                                   |
| ---------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------- |
| Escrita UTM / cutover native-first                   | Ownership e contrato ainda sem prova em shadow | `MKT-N1..N3` + reconciliação + go/no-go                       |
| Binding CRM real                                     | Mapeamento operacional ambíguo                 | inventário `CRM-MAP` + validação humana                       |
| Broadcast WhatsApp em massa                          | Canal não-oficial e risco operacional          | canal oficial; pacing não converte WuzAPI em canal autorizado |
| Meta outbound por ativo                              | Capability/credencial efetiva                  | health específico de mensageria e prova live                  |
| Dashboard native-first definitivo                    | causalidade/dependências ainda não provadas    | `MKT-N3`                                                      |
| Automação de nutrição completa                       | prematura durante adaptação                    | pós-`MKT-POST`                                                |
| Merge destrutivo de leads e IA generativa no chatbot | fora da decisão de produto                     | não planejados                                                |

## 6. Validação final corrigida

O GO não é irrestrito nem exige iniciar quatro frentes de código ao mesmo tempo.
Primeiro devem ser fechados disposição, ownership e baseline. Depois podem avançar em
paralelo `CC-SEND`, `MKT-N1`, o inventário `CRM-MAP` e o scaffold de i18n. Binding CRM
real, escrita UTM e cutover mantêm seus gates.

Como critério funcional orientativo, o atendente deve futuramente conseguir, sem sair da
Central, ser notificado, responder com template, transferir, registrar nota, criar/ver
lead e mover o caso no funil, enquanto o gestor acompanha TMA/volume/conversão. "Origem
de campanha" só entra nesse critério depois de `MKT-N3` + `CRM-UTM`; não é promessa de
`CC-CRM`. A comparação com RD permanece referência de produto, nunca gate de arquitetura
ou substituto de teste no Odoo.
