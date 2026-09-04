# Resposta crítica à análise de UX da Central de Atendimento

> **Addendum de contrachecagem independente — 2026-09-02:** a disposição foi confrontada
> novamente com o código, os logs e o artefato canônico do release. A implementação
> descrita existe e as três suítes Odoo são reproduzíveis. As contagens QUnit e o smoke
> autenticado foram reportados pela sessão de entrega, mas seus logs/JSON/screenshots
> pós-release não foram preservados no artefato; por isso não constituem, isoladamente,
> evidência reproduzível de release. A disposição normativa e a ordem corrigida estão em
> `reviews/2026-09-02-native-first-roadmap-and-ux-disposition.md`.

- Data da disposição: 2026-09-02
- Documento analisado: `reviews/2026-09-02-ux-critical-review.md`
- Escopo desta resposta: código e arquitetura do `contact-center`, com as pontes
  existentes para CRM e Marketing Center
- Método: contrachecagem estática dos símbolos citados, contratos dos modelos, regras de
  acesso, testes existentes e remediações concorrentes visíveis no worktree
- Resultado do lote: achados confirmados de atenção, rascunho, concorrência de envio,
  clipboard/drop, erro de imagem e retorno ao fim foram corrigidos, testados e
  implantados somente no laboratório SERVIDOR05

## Estado de validação deste lote

| Verificação                          | Estado                                                                     |
| ------------------------------------ | -------------------------------------------------------------------------- |
| Revisão estática da disposição       | Concluída                                                                  |
| Execução QUnit do addon              | **Reportada: 111/111 casos; 1010/1010 asserções; artefato não preservado** |
| Execução dos testes Odoo             | **408/408 base; 204/204 WuzAPI; 742/742 integrados**                       |
| Atualização dos addons no SERVIDOR05 | **Concluída e validada**                                                   |
| Smoke autenticado no navegador       | **Reportado em desktop e 390 x 844; artefato não preservado**              |
| Deploy em produção                   | **Não realizado nem autorizado por esta revisão**                          |

“Corrigido neste lote” abaixo significa implementação presente no worktree, validada
estaticamente e pelas suítes Odoo persistidas, e publicada no laboratório. QUnit e
walkthrough permanecem como relato da sessão até serem repetidos com artefato durável.
Não significa deploy em produção.

## Veredito consolidado

A crítica acerta o diagnóstico principal: a base de mensageria é madura, enquanto
produtividade multiatendente, SLA e a superfície CRM ainda precisam evoluir. Ela, porém,
superestima algumas severidades e contém quatro imprecisões arquiteturais relevantes:

1. `blocked` e `unsupported` são estados de **inbox event**, não de outbox. Falhas
   permanentes da outbox já chegam a `dead` e projetam `delivery_state=failed`.
2. Nota interna não é inexistente no backend: `mail.mt_note` sem destinatários já é
   permitido e isolado do provider. O que falta é sua superfície operacional,
   atualização em tempo real e follow-up.
3. Uma conversa não está limitada a um lead “para sempre”. Ela possui um caso default,
   aceita casos não-default e cada caso pode ter um lead; o mesmo lead também pode estar
   ligado a vários casos.
4. A atribuição de Marketing não morre no Contact Center: já há convergência M:N entre
   touchpoints, casos e leads. Falta a projeção controlada para os campos UTM singulares
   nativos do CRM.

Para um piloto restrito à mensageria, a priorização adotada trata C5 e C6 como P1 altos;
para um gate de produto omnichannel/CRM eles voltam a ser bloqueadores. Essa
classificação é decisão de produto, não conclusão verificável apenas no código. C1–C4
afetam diretamente o trabalho diário; neste lote há remediação em worktree para C1 e C4,
além de M1, M2, M4 e parte de M7.

## Disposição dos achados críticos

| ID     | Disposição                                                                 | Evidência contrachecada                                                                                                                                                                                                                                                                                           | Decisão                                                                                                                                                                                                                                                                                                            |
| ------ | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **C1** | **Confirmado no baseline; corrigido e validado**                           | O baseline não possuía atenção fora de foco e o Discuss é deliberadamente silenciado para canais CC. O worktree agora contém `browser_attention.esm.js`, integração no app/store e `direction` provider-neutral no evento `message_created`.                                                                      | Mantido o mínimo seguro: contador no título, sinal sonoro opt-in e Notification API sem nome ou conteúdo do cliente. Favicon/systray permanecem opcionais. QUnit, suítes Odoo, update e smoke passaram.                                                                                                            |
| **C2** | **Parcialmente confirmado**                                                | A ausência de reenvio seguro é real. Entretanto a revisão mistura estados: a outbox usa `pending/processing/retry/done/uncertain/dead/cancelled`; `blocked/unsupported` pertencem à inbox. `dead` de `send_message` já projeta falha, enquanto provider pausado pode permanecer `pending` sem motivo claro na UI. | Backlog de confiança: expor motivo de espera/falha e criar **nova** mensagem/binding/outbox ao reenviar, com linhagem `retry_of`, ACL, capability, health, lock e idempotência. Nunca reabrir uma outbox terminal. `uncertain` exige resolução/consentimento explícito; não pode ser reenviado automaticamente.    |
| **C3** | **Confirmado**                                                             | Não existe presença de agente por conversa, indicador “está respondendo” entre atendentes nem typing provider-neutral. O badge de responsável não evita colisão, especialmente em fila não atribuída.                                                                                                             | Backlog P1: presença efêmera por conversa/usuário, TTL curto e bus com dados mínimos; não persistir rascunho/conteúdo. Separar presença interna de typing enviado ao cliente.                                                                                                                                      |
| **C4** | **Confirmado no baseline; corrigido e validado**                           | O baseline descartava corpo, reply e mídia ao trocar de canal. O worktree agora mantém drafts por `channel_id`, preserva `media_ref` já enviado ao servidor e bloqueia a troca durante upload ou gravação ativos até o usuário concluir ou cancelar.                                                              | Mantido draft local por conversa, com limpeza de object URLs e proteção contra resposta assíncrona obsoleta. A alternância A→B→A foi provada no navegador. Persistência após reload continua fora do escopo deste primeiro ajuste.                                                                                 |
| **C5** | **Confirmado como lacuna de produto; severidade recalibrada para P1 alta** | A UI operacional e seu DTO não expõem caso, pipeline, etapa ou lead. O modelo, porém, já possui caso default único, múltiplos casos não-default, revisão, lock, ledger e sincronização CRM bidirecional. O menu backoffice `Service Cases` já permite ao agente criar outros casos.                               | Não adicionar dependência de CRM ao `contact_center_ui`. Expor no core a projeção e ações genéricas de caso; o `contact_center_crm` deve acrescentar capabilities/projeção CRM, ou um futuro `contact_center_crm_ui` deve depender de UI+CRM. Deep-link por `channel_id` e “novo caso” entram antes da camada CRM. |
| **C6** | **Parcialmente confirmado; expressão “silos” refutada na modelagem**       | Não há smart button em `res.partner` nem lista de outras conversas no painel. Porém identidade já possui `partner_id`, múltiplos bindings e reconciliação cross-account para identificadores portáveis. O form nativo do contato já oferece os documentos permitidos pelas ACLs do Odoo.                          | Core: outras conversas acessíveis e smart button “Conversas”, sempre filtrados por membership e empresas ativas. CRM, Sale, Account e Helpdesk entram por addons bridge opcionais; o base não deve serializar documentos nem revelar registros sem record rules.                                                   |

### Cobertura executada e riscos residuais dos críticos

Para C1 e C4, os itens abaixo são matriz de regressão/resíduo, não afirmação de que a
implementação esteja ausente. Para C2, C3, C5 e C6, continuam critérios de aceite de
entregas futuras.

- **C1:** deduplicação de replay, somente inbound, aba focada, permissão negada,
  browsers sem Notification/AudioContext, privacidade da tela bloqueada e evento de
  controle/outbound sem alerta.
- **C2:** concorrência de dois cliques, acesso cruzado, mídia e reply, provider pausado,
  capability removida, `dead` versus `uncertain` e auditoria completa da linhagem.
- **C3:** TTL, fechamento/troca da conversa, perda de conexão, usuário removido da
  caixa, ACL do canal e múltiplas abas do mesmo usuário.
- **C4:** alternância rápida A/B, reply, anexo pronto, upload/gravação ativos,
  cancelamento, resposta tardia e liberação única dos recursos locais.
- **C5:** optimistic locking de etapa, replay, múltiplos casos/leads, usuário sem CRM,
  vendedor sem acesso ao lead e deep-link para canal fora da membership.
- **C6:** um partner em várias caixas, convidado com identificador opaco, identidade
  mesclada, multiempresa e contagem sem vazamento de existência.

## Disposição dos achados altos

| ID     | Disposição                  | Evidência contrachecada                                                                                                                                                                                                                                                                              | Decisão                                                                                                                                                                                                                                                                                                                  |
| ------ | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **A1** | **Confirmado**              | Não existe catálogo de respostas rápidas, busca ou inserção de snippet no composer.                                                                                                                                                                                                                  | Backlog P1 de produtividade: modelo provider-neutral, escopo por empresa/caixa/equipe, ACL, atalhos e placeholders estritamente allowlisted. Não usar `mail.template` para enviar diretamente ao provider.                                                                                                               |
| **A2** | **Parcialmente confirmado** | Falta espera/SLA, filtros por equipe/tag/etapa e ordenação por risco. A autoatribuição a um usuário fixo, contudo, foi requisito explícito e está implementada como estratégia válida; não é defeito.                                                                                                | Primeiro criar projeção canônica, armazenada e indexada de episódio de espera (`waiting_since`, necessidade de resposta e marcos inbound/outbound), com cursor compatível com cada sort. Depois adicionar estratégias opcionais fixa, round-robin, carga e disponibilidade. O Contact Center continua autoridade do SLA. |
| **A3** | **Parcialmente confirmado** | O select desabilitado sem explicação é uma falha de UX. A restrição supervisor-only é RBAC intencional; liberar write arbitrário ao agente seria regressão de segurança.                                                                                                                             | Ajuste curto: mostrar responsável estático e explicar a política, ou ocultar o select. Backlog: “liberar atendimento” pelo responsável e pedido tipado de transferência/escalonamento com motivo, destino, estado e aprovação.                                                                                           |
| **A4** | **Confirmado**              | A UI operacional continua com strings pt-BR hardcoded e não usa `_t`; o uso de `_t` no JSON viewer do addon base não resolve o frontend da caixa.                                                                                                                                                    | Trilha transversal antes de ampliar muito a UI: extrair strings de JS/XML, criar traduções e padronizar os nomes funcionais. Não é defeito de mensageria nem bloqueio do piloto pt-BR.                                                                                                                                   |
| **A5** | **Parcialmente confirmado** | `MailChannel.message_post` já aceita exclusivamente `mail.mt_note` sem destinatários e bloqueia comment/email/autor forjado; não cria outbox, notificação ou e-mail. A UI não oferece a ação, a nota nativa não usa o publisher de tempo real e `contact.center.case` não possui atividades/chatter. | Core imediato: RPC explícito de nota, visual distinto e evento de UI que não reordene SLA, não reabra conversa e não pareça mensagem ao cliente. Follow-up genérico pode usar `mail.activity` no caso; atividade comercial permanece no lead, sem duplicação automática.                                                 |
| **A6** | **Parcialmente confirmado** | Localização, contato, sticker e interativos não possuem projeção tipada completa na UI. Parte dos metadados existe em snapshots/extensions, mas não há contrato provider-neutral suficiente.                                                                                                         | Backlog arquitetural separado: DTOs tipados/allowlisted, normalização equivalente entre providers, ledger e UiDTO; só então componentes e ações. Não corrigir com leitura direta de snapshots WuzAPI no frontend.                                                                                                        |
| **A7** | **Parcialmente confirmado** | A criação explícita do lead passa poucos campos e não enriquece de forma controlada guest/descrição/vendedor. Já existe, porém, convergência M:N no `marketing_center_contact_center_crm`; `unique(case_id)` significa um lead por caso, não um lead por conversa/cliente.                           | `contact_center_crm`: enriquecer telefone validado (nunca LID), e-mail, partner, descrição curta/deep-link e responsável elegível. UTM pertence ao bridge Marketing↔CC↔CRM, com `first_trusted_assignment`, fill-only e receipt atômico; nunca escrita concorrente direta pelo Contact Center.                           |

### Critérios pendentes para os altos

- **A1:** escopo caixa/equipe/empresa, placeholder desconhecido, XSS, busca, teclado e
  canal sem capability.
- **A2:** inbound inicia espera; resposta do agente/dispositivo encerra conforme
  política; receipt, mutation e nota não alteram SLA; paginação determinística, empates,
  filtros combinados e concorrência da atribuição.
- **A3:** agente não atribui terceiro, supervisor transfere, release somente pelo
  responsável e corrida claim/release/transfer.
- **A4:** extração de assets, fallback pt-BR, pluralização e smoke nos idiomas
  habilitados.
- **A5:** nota sem binding/outbox/e-mail/provider, sanitização, bus, ACL, nenhuma
  alteração de SLA/reopen/preview e atividade sem duplicar a do lead.
- **A6:** schema estrito, campos desconhecidos, fixtures equivalentes WuzAPI/Meta,
  sanitização e round-trip.
- **A7:** partner e guest, proibição de LID como telefone, vendedor elegível, múltiplos
  casos/leads, UTM sem overwrite, conflito, revisão, revogação e idempotência.

## Disposição dos achados médios

| ID      | Disposição                                              | Evidência contrachecada                                                                                                                                                                           | Decisão                                                                                                                                                                                      |
| ------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **M1**  | **Confirmado no baseline; corrigido e validado**        | `sending` era global. O worktree agora usa `sendingChannelIds` e `pendingSends` com `{signature, requestId}` por `channel_id`; não é um mapa de promises.                                         | Mantida concorrência isolada por conversa e duplicidade impedida apenas no mesmo canal.                                                                                                      |
| **M2**  | **Confirmado no baseline; corrigido e validado**        | O worktree adiciona extração de arquivo do clipboard, paste, dragover/drop e feedback visual, preservando paste textual normal.                                                                   | Mantido o limite atual de um arquivo claramente comunicado; multi-anexo continua sendo M3.                                                                                                   |
| **M3**  | **Confirmado**                                          | Input e estado aceitam um arquivo; composer recusa vários. O backend também limita o DTO/comando e WuzAPI exige exatamente uma mídia em caminhos relevantes; Meta continua text-only nesta fase.  | Backlog de contrato, não ajuste cosmético: definir lote ou múltiplos comandos, idempotência, ordenação e partial failure por provider.                                                       |
| **M4**  | **Confirmado no baseline; corrigido e validado**        | O worktree adiciona fallback de erro, retry real em ramo irmão ao viewer e callback opcional do `DeferredImage`, preservando usos como avatar e ignorando geração obsoleta.                       | Mantido erro acionável sem botão aninhado e sem furar a fila limitada de imagens.                                                                                                            |
| **M5**  | **Confirmado**                                          | `groupConversationsByInbox()` soma `unread_count` apenas das conversas carregadas; não representa a fila inteira.                                                                                 | Backlog backend: agregados server-side por caixa e pelos filtros efetivos, sem carregar todas as conversas e sem furar membership.                                                           |
| **M6**  | **Confirmado**                                          | Na visão agrupada, o seletor de caixa depende de já existir filtro ou de visão plana, criando um controle praticamente inalcançável no default.                                                   | Correção curta de UI: seletor sempre disponível quando houver mais de uma caixa, ou ação no cabeçalho do grupo. Preservar domínio server-side.                                               |
| **M7**  | **Parcialmente confirmado; parte corrigida e validada** | Paginação de lista/timeline continua manual. O worktree agora mantém `awayFromLatest` e mostra “Ir ao fim” mesmo sem mensagem nova.                                                               | O salto está resolvido. Scroll infinito permanece backlog separado, com guarda de cursor repetido e preservação de posição.                                                                  |
| **M8**  | **Confirmado**                                          | Há teclado local em composer, menus e viewer, mas não navegação global por conversa/assumir/resolver/busca.                                                                                       | Backlog de produtividade: mapa de atalhos documentado, sem capturar teclas durante digitação, modal ou leitor de tela.                                                                       |
| **M9**  | **Confirmado**                                          | Caso e lead não abrem diretamente a conversa operacional; o smart button do lead termina no caso.                                                                                                 | Entregar com C5: app aceita `channel_id` validado, e caso/lead usam ação “Abrir conversa”. Fallback para form nativo quando UI não estiver instalada.                                        |
| **M10** | **Confirmado**                                          | Não há graph/pivot de atendimento no repo. Existem marcos e ledger, mas ainda não uma semântica consolidada de TME/TMA/SLA.                                                                       | Backlog após A2: facts/projeções auditáveis e views graph/pivot por empresa, caixa, equipe, canal e agente. Não calcular indicadores apenas no cliente.                                      |
| **M11** | **Parcialmente confirmado**                             | Bindings são configuração administrativa e as ações comerciais falham de forma segura quando pré-condições não existem. O vendedor, porém, recebe orientação sobre menu que não pode abrir.       | Expor capability e motivo funcional antes da ação; ocultar/desabilitar criar/vincular quando a ponte não estiver configurada e orientar “procure o administrador”, sem revelar IDs técnicos. |
| **M12** | **Confirmado**                                          | Telefone/e-mail do partner vinculado são somente leitura no painel; os inputs existentes pertencem aos fluxos de criar/vincular, não à edição do cadastro ligado.                                 | Backlog curto: edição inline por RPC allowlisted, expected partner/identity, ACL de `res.partner`, normalização e tratamento de conflito.                                                    |
| **M13** | **Confirmado**                                          | O conjunto de control events é fechado em chamadas e mudança de segurança; alterações de participante/nome do grupo não viram itens da timeline.                                                  | Backlog provider-neutral: DTO tipado de eventos de grupo, dedupe/ordenação e projeção como evento de sistema, nunca texto específico do WuzAPI.                                              |
| **M14** | **Parcialmente confirmado**                             | `checking` é uma atividade sobreposta ao estado-base, então a soma aparente maior que o total é tecnicamente intencional, mas confusa. “Login necessário” ainda não oferece caminho direto ao QR. | Separar estado (“conectada/atenção/desconectada”) de atividade (“verificando”) e adicionar CTA de autenticação somente quando a capability/provider fornecer fluxo seguro.                   |
| **M15** | **Parcialmente confirmado**                             | A UI não oferece ação manual, mas o store já tenta `busService.start()` novamente e reconcilia RPC a cada 30 s; não há ausência total de recuperação.                                             | Backlog curto: “Reconectar agora” idempotente/debounced, estado de progresso e mensagem de resultado, sem duplicar listeners.                                                                |
| **M16** | **Confirmado**                                          | Não existe esqueleto provider-neutral para typing outbound ao cliente.                                                                                                                            | Backlog condicionado à capability: comando efêmero, throttled, sem fila durável e falha silenciosa; não misturar com presença interna de C3.                                                 |

### Critérios pendentes para os médios

- **M1/M4:** há cobertura QUnit específica; as suítes Odoo passaram e o smoke foi
  reportado. **M2:** o QUnit cobre o helper de extração, mas não handlers,
  `preventDefault`, feedback ou upload. **M7:** há teste da decisão de scroll, não do
  estado/componente/botão. Esses fluxos ainda precisam de cobertura integrada e de
  artefato de navegador persistido.
- **M3:** lote misto, ordem, retry parcial, duplicate click, provider que só aceita uma
  mídia e Meta sem capability.
- **M5/M6:** totais com paginação, filtros, multiempresa, membership e caixas vazias.
- **M8:** foco no composer, modal aberto, mobile e conflito com atalhos nativos Odoo.
- **M9/M11/M12:** usuários sem CRM/partner write, record rules, multiempresa e IDs
  adulterados.
- **M10:** definição de relógio, timezone, pausa/fim de expediente, reabertura e
  reconciliação com eventos atrasados.
- **M13:** replay, reorder, roster incompleto e providers que não emitem o evento.
- **M14/M15:** invariantes de contagem, capability QR, clique repetido e transportes já
  reconectando.
- **M16:** throttle, troca de canal, fechamento do composer, provider offline e ausência
  de capability.

## Disposição dos achados baixos e observações de acabamento

| Tema                                                | Disposição                                                                                                                             | Decisão                                                                                                                   |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Preferência agrupada/plana e grupos recolhidos      | **Parcialmente confirmado.** Densidade confortável/compacta já é persistida; visão e colapsos não.                                     | Backlog de preferência local versionada por usuário/navegador, com fallback seguro.                                       |
| `detailsOpen` sem reação a resize                   | **Confirmado.**                                                                                                                        | Backlog baixo: listener debounced ou política CSS/estado explícita, sem fechar painel onde o usuário o abriu manualmente. |
| Waveform de áudio decorativa                        | **Confirmado.**                                                                                                                        | Assumir visualmente como decorativa ou gerar peaks reais quando o pipeline puder fornecê-los; não fingir precisão.        |
| Confirmação de exclusão sem focus trap              | **Confirmado.**                                                                                                                        | Correção de acessibilidade: `role=dialog`, foco inicial, ciclo de Tab, Escape e restauração do foco.                      |
| Divisor de dia e timezone                           | **Confirmado como risco.** `deserializeDateTime()` usa contexto Odoo, enquanto “agora” parte de `DateTime.local()`.                    | Usar a mesma timezone efetiva para mensagem e relógio; testar virada do dia e horário de verão.                           |
| Nome-hash no lightbox/download                      | **Confirmado.**                                                                                                                        | Exibir rótulo humano sanitizado e preservar o nome técnico apenas no download/diagnóstico.                                |
| Vídeo sem poster                                    | **Confirmado.**                                                                                                                        | Backlog visual condicionado a thumbnail segura do provider ou geração local limitada.                                     |
| Fonte/contraste muito pequenos                      | **Parcialmente confirmado.** Há tamanhos extremamente pequenos; a afirmação de contraste precisa de medição.                           | Fazer auditoria WCAG automatizada e visual antes de alterar a escala global.                                              |
| Conversa preservada artificialmente no fim da lista | **Confirmado.** É mecanismo para não perder a seleção, mas quebra a ordem visual.                                                      | Fixar temporariamente a conversa selecionada em área própria ou reinseri-la na posição anterior até retornar ao domínio.  |
| Badge da caixa repetido                             | **Refutado como defeito geral.** O usuário solicitou identificação explícita da caixa e ela é essencial em operação com várias caixas. | Manter; reduzir ruído no modo compacto ou quando o cabeçalho agrupado já fornece o mesmo contexto.                        |
| “Ver webhook de origem” técnico                     | **Refutado para o usuário comum.** DTO e capability só o expõem a `base.group_system`, conforme requisito de debug/system admin.       | Manter restrito; nenhum ajuste funcional obrigatório.                                                                     |
| Dark mode                                           | **Ausência confirmada, mas não defeito.**                                                                                              | Backlog de design sem prioridade operacional.                                                                             |
| Hint do composer apenas com foco                    | **Comportamento intencional plausível.**                                                                                               | Manter por ora; revisar por teste de usabilidade, não por suposição.                                                      |
| Ctrl/Cmd+Enter na edição versus Enter no envio      | **Não caracteriza defeito isoladamente.** São contextos diferentes e evitam edição acidental.                                          | Documentar nos hints e validar com uso real antes de unificar.                                                            |

## Arquitetura consolidada para Contact Center, casos, kanban e CRM

O caminho proposto pela revisão é aceito com separação mais rigorosa de domínios. Esta
aprovação cobre Contact Center e CRM. A parte UTM abaixo é arquitetura-alvo do Marketing
Center e não autoriza escrita antes de shadow e go/no-go native-first.

### 1. Core do Contact Center

O `contact_center_base` continua autoridade de:

- conversa, identidade, membership e segurança por caixa;
- caso de atendimento, pipeline, etapa e ledger de transições;
- estado operacional, espera/SLA e responsável;
- nota interna da conversa;
- links genéricos para abrir uma conversa e criar um caso não-default.

O UiDTO deve expor uma coleção **limitada e tipada** de casos com `id`, `case_ref`,
`name`, pipeline, etapa, `stage_revision`, prioridade e `is_default`. A mudança de etapa
deve enviar `expected_revision`; o servidor permanece autoridade.

### 2. Ponte CRM

O `contact_center_crm` continua autoridade de:

- bindings equipe/pipeline/etapa com CRM;
- vínculo explícito e imutável caso↔lead;
- sincronização bidirecional de etapa;
- criação, vínculo e abertura de lead sob ACLs do CRM;
- enriquecimento não-UTM do lead.

Ele pode estender o serviço abstrato do UiDTO com um namespace CRM e capabilities. Se
forem necessários assets próprios, criar `contact_center_crm_ui` dependendo de
`contact_center_ui` e `contact_center_crm`. O addon UI genérico não ganha dependência
obrigatória de CRM.

### 3. Ponte Marketing↔Contact Center↔CRM

O `marketing_center_contact_center_crm` permanece autoridade da convergência M:N de
touchpoints com leads. A projeção para `campaign_id`, `source_id` e `medium_id` só pode
ocorrer pelo contrato native-first do Marketing Center:

- mapeamento versionado;
- `first_trusted_assignment`;
- fill-only, sem sobrescrever valor nativo existente;
- assignment e receipt atômicos;
- conflito ou múltiplos touchpoints permanecem no ledger, sem escolha silenciosa;
- revisão/revogação preservam auditabilidade.

No estado atual, providers e bridges podem produzir propostas e evidência, mas a
aplicação dessa tupla no lead continua bloqueada.

### 4. Visão 360°

O core pode mostrar outras conversas acessíveis do partner/identidade. Pedidos, faturas,
oportunidades e tickets são projeções de addons opcionais como `contact_center_sale`,
`contact_center_account`, `contact_center_crm` e futuro `contact_center_helpdesk`. Cada
bridge aplica suas próprias ACLs e record rules; o Contact Center não usa `sudo()` para
montar um painel documental universal.

### 5. Cardinalidade preservada

- uma conversa possui exatamente um caso default;
- uma conversa pode possuir vários casos não-default;
- um caso possui no máximo um lead;
- um lead pode estar associado a vários casos/conversas;
- touchpoints e leads convergem em relação M:N auditável.

Essa cardinalidade cobre cliente recorrente, duas propostas na mesma conversa e o mesmo
negócio discutido em mais de uma caixa sem criar um M2M opaco diretamente na conversa.

## Ordem de execução recomendada

1. **Fundação antes de ampliar código:** fechar disposição/ownership, baseline
   reproduzível e scaffold de i18n. O mapeamento real de equipes/pipelines requer
   inventário de candidatos e validação humana.
2. **Concluído neste lote:** C1, C4, M1, M2, M4 e salto de M7.
3. **Confiança de envio:** C2, com status honesto e reenvio por nova tentativa
   auditável.
4. **Produtividade curta:** A1, M6, A3 explicativo, M8 e UI de nota interna de A5.
5. **Fila multiatendente:** C3, A2, agregados M5, transferência/escalonamento e health
   M14/M15.
6. **Casos e CRM:** deep-links M9, UiDTO de casos C5, caso não-default, lead enriquecido
   A7 e follow-up A5.
7. **Omnichannel:** C6 e bridges documentais opcionais.
8. **Conteúdo/relatórios:** A6, M13, M16, M10 e acabamento restante.

## Validação e release do lote

- Release canônico do laboratório:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260902T053039152348Z`.
- Estado final: `applied_and_validated`; árvore
  `9597aeecf8ee5e0b25f4e67d54a1b4f0d617d978dc6af43d8c7bfd2af17d154a`.
- Versões alteradas: `contact_center_base 16.0.1.35.1` e
  `contact_center_ui 16.0.1.23.1`. WuzAPI `16.0.1.27.0`, Meta `16.0.2.0.0` e CRM
  `16.0.2.0.1` foram preservados e validados no conjunto.
- Suítes Odoo: base **408/408**, WuzAPI **204/204** e integração **742/742**, todas com
  zero falhas e zero erros.
- A sessão de entrega reportou QUnit filtrado por `contact_center_ui`: **111/111** casos
  e **1010/1010** asserções, zero falhas, zero skips e zero TODO. Uma execução acidental
  da suíte web global encontrou somente a conhecida interferência do banner de
  neutralização em teste nativo do `MockServer`; isso não pertence ao addon nem aparece
  na execução filtrada. O artefato de release não preservou o log dessa execução
  filtrada.
- A sessão também reportou smoke autenticado: aplicação carregou por HTTP 200 sem erro
  de console; o controle opt-in “Ativar som e notificações fora desta aba” apareceu; o
  draft `A -> B -> A` foi restaurado e depois limpo sem envio; o layout permaneceu
  acessível em desktop e `390 x 844`. Screenshot/trace/log pós-release não foi
  incorporado ao diretório canônico.
- A rota pública do laboratório e o Traefik foram restaurados byte a byte após o update.
  Nenhum backup foi criado, conforme a política do laboratório descartável. Produção não
  foi acessada nem alterada.

O lote técnico C1/C4/M1/M2/M4/M7 está encerrado. O gate de evidência de release continua
exigindo repetição persistida de QUnit e smoke. C2, C3, C5, C6 e os demais itens
mantidos em backlog não foram mascarados como resolvidos; eles exigem contratos próprios
e seguem na ordem de execução acima.
