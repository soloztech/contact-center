# Disposição consolidada — native-first, roadmap e UX

- Data: 2026-09-02
- Estado: **normativo antes da próxima implementação**
- Escopo: Contact Center, Marketing Center, pontes CRM/Website e roadmap comparativo
- Regra: `contact-center/plan.md` e `marketing-center/plan.md` continuam canônicos; esta
  disposição resolve divergências entre as revisões de 2026-09-01/02.

## 1. Veredito

A direção native-first é aceita: modelos nativos do Odoo são autoridade operacional onde
já representam adequadamente o conceito, e os ledgers próprios preservam evidência
externa, correlação M:N, replay, causalidade e atribuição que o Odoo 16 não modela. Isso
não autoriza escrita UTM nem cutover; ambos continuam condicionados a shadow,
reconciliação e go/no-go.

A correção UX implantada no SERVIDOR05 é real. Entretanto, a resposta original
superestimou parte da cobertura de browser e chamou de “gate encerrado” algo que foi
somente o encerramento do lote técnico. O código foi aceito; a evidência QUnit/smoke
deve ser persistida novamente antes de um release fora do laboratório.

O roadmap baseado em RD Station é uma heurística de produto. Não é fonte de verdade
arquitetural, não substitui teste local e não cria um mapeamento 1:1 entre produtos.

## 2. Contrachecagem da UX

| Item                       | Disposição final                    | Observação                                                                                                                     |
| -------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| C1 — atenção fora de foco  | **implementado**                    | somente inbound; dedupe; opt-in; título, som e Notification API sem nome/conteúdo do cliente                                   |
| C4 — rascunho por conversa | **implementado, cobertura parcial** | texto A→B→A foi exercitado; upload/gravação ativos, resposta tardia e descarte único de object URL ainda pedem teste integrado |
| M1 — envio por conversa    | **implementado**                    | `sendingChannelIds` e `pendingSends` por canal; este último guarda `{signature, requestId}`, não promises                      |
| M2 — clipboard/drop        | **implementado, cobertura parcial** | QUnit cobre extração; ainda faltam handlers, feedback, `preventDefault` e upload integrados                                    |
| M4 — imagem erro/retry     | **implementado e bem coberto**      | erro deixa de ser shimmer infinito; retry respeita fila e geração atual                                                        |
| M7 — ir ao fim             | **parte implementada**              | botão/estado existem; scroll infinito continua fora do lote e o teste atual não monta todo o componente                        |
| C2 — falha/reenvio         | **achado parcialmente válido**      | a revisão confundiu inbox com outbox; a ausência de reenvio auditável continua real                                            |
| A5 — nota interna          | **achado parcialmente válido**      | backend aceita `mail.mt_note` sem provider/outbox; faltam UI, publisher e follow-up no caso                                    |
| C5 — casos/CRM             | **lacuna de produto real**          | backend existe; UiDTO, deep-link e kanban operacional ainda não                                                                |
| C6 — visão 360°            | **lacuna de produto real**          | identidade não está em silo, mas a experiência ainda está                                                                      |

A mudança de C5/C6 de P0 para P1 é aceitável apenas para piloto restrito à mensageria.
Se o gate for “produto omnichannel com CRM”, ambos voltam a ser bloqueadores. Severidade
é decisão de produto, não fato demonstrável no código.

## 3. Evidência do release UX

Confirmado no artefato `20260902T053039152348Z`:

- 277 arquivos e árvore
  `9597aeecf8ee5e0b25f4e67d54a1b4f0d617d978dc6af43d8c7bfd2af17d154a`, ainda idênticos à
  árvore local verificada;
- base `16.0.1.35.1`, UI `16.0.1.23.1`, WuzAPI `16.0.1.27.0`, Meta `16.0.2.0.0` e CRM
  `16.0.2.0.1`;
- Odoo base **408/408**, WuzAPI **204/204** e integrado **742/742**, sem falhas de
  teste;
- upgrade concluído, containers sem restart, `/web/login` interno e público HTTP 200 e
  rota Traefik do ambiente de teste preservada.

Limites da evidência:

- existem 111 declarações QUnit e logs externos da sessão reportam 1010 aprovações, mas
  o diretório canônico do release não armazena seu log/JSON;
- snapshots externos apoiam o walkthrough, mas o release não preserva
  screenshot/trace/console pós-deploy nem prova sozinho o viewport `390 × 844`;
- um smoke público independente posterior confirmou `/web/login` HTTP 200 sem erro de
  console, mas não substitui o fluxo autenticado;
- o artefato prova o alvo SERVIDOR05 e os guards da rota de teste; a afirmação “produção
  não tocada” não possui auditoria independente do SERVIDOR02;
- o release não comprova tráfego externo real WuzAPI/Meta.

Decisão: aceitar o lote no laboratório e exigir QUnit + smoke autenticado com artefatos
persistidos antes de release produtivo.

## 4. Disposição native-first

### Aceito

- `utm.*`, Website, CRM, Sale e Account são as fontes operacionais nativas;
- `link.tracker.click` é o fato nativo de links controlados pelo Odoo, sem promessa de
  representar cada clique físico;
- touchpoints e Business Events permanecem como evidência, correlação, ocorrência,
  reversão e insumo de atribuição;
- o controller Website/CRM precisa de MRO cooperativo comprovado por `HttpCase`;
- UTM usa taxonomia global no Odoo 16; o mapping é company-scoped, mas os cadastros
  `utm.*` não são privados por empresa;
- aplicação UTM usa `first_trusted_assignment`, tupla atômica, fill-only, lock/CAS,
  revisão/fencing, preimage, after-image, receipt e tombstone de override humano;
- causalidade financeira deve seguir pedido/linha/fatura/reconciliação; a tupla de uma
  fatura agrupada não prova crédito;
- kill switch interrompe escrita futura; rollback exige compensação condicionada ao
  receipt e ao valor ainda vigente.

### Rejeitado ou corrigido

- não há GO irrestrito em quatro frentes;
- bindings reais CRM não são tarefa autônoma: somente o inventário de candidatos é;
- Google read-only já possui entrega substancial; o restante é paridade/evidência
  externa, não implementação do zero;
- `pages_read_engagement` isolado não é health de mensageria Meta;
- as contagens históricas de jobs/eventos mortos do lab devem ser recontadas e
  classificadas antes de virar gate atual;
- LGPD/retenção permanece decisão futura de pré-cutover/outbox, conforme decisão do
  produto; não bloqueia desenvolvimento read-only/shadow agora;
- o benchmark RD não prova superioridade nem substitui requisitos próprios.

## 5. Ownership normativo

| Responsabilidade                                        | Dono                                                          |
| ------------------------------------------------------- | ------------------------------------------------------------- |
| conversa, identidade, caso, SLA e evidência operacional | `contact_center_base`                                         |
| enriquecimento não-UTM do lead                          | `contact_center_crm`                                          |
| evidência da jornada e mapping provider→UTM             | `marketing_center_base`                                       |
| tradução da evidência CC para touchpoint de marketing   | `marketing_center_contact_center`                             |
| convergência touchpoint↔caso↔lead                       | `marketing_center_contact_center_crm`                         |
| aplicação atômica/receipt UTM no lead                   | `marketing_center_crm`                                        |
| MRO e correlação Website→CRM                            | `marketing_center_website_crm`                                |
| clique controlado pelo Odoo                             | `link.tracker.click`                                          |
| landing, click IDs, externo/headless e handoff WhatsApp | `marketing_center_web_ingress`                                |
| fatos de lead/pedido/fatura/pagamento                   | CRM, Sale e Account nativos                                   |
| causalidade financeira                                  | bridges Sale/Account tipados                                  |
| dashboard native-first                                  | `marketing_center_dashboard`, com dependências/ACL explícitas |

Providers e bridges propõem atribuição; nenhum deles escreve UTM diretamente.

## 6. Sequência liberada

1. concluir esta disposição, ownership, baseline reproduzível e scaffold de i18n;
2. em paralelo: confiança de envio (`CC-SEND`), MRO/contrato de clique (`MKT-N1`) e
   inventário de candidatos CRM (`CRM-MAP`);
3. produtividade e nota interna na UI; mapping/assignment UTM somente em shadow;
4. presença, fila, SLA e health acionável; validar causalidade financeira/dashboard;
5. dry-run/backfill e go/no-go específico do Marketing Center;
6. somente depois: bindings CRM validados por pessoa, UiDTO/deep-links/kanban e,
   separadamente, escrita UTM autorizada;
7. visão 360°, conteúdo rico, analytics e outboxes de conversão nas fases próprias.

## 7. Gates preservados

- escrita UTM e cutover: `MKT-N1..N3`, shadow, reconciliação e go/no-go;
- binding CRM real: inventário + confirmação humana;
- release: baseline recuperável nos três repositórios e evidência reproduzível;
- ampliação relevante de UI: i18n;
- broadcast compatível: canal oficial. Um experimento WuzAPI com pacing ainda exige
  aceitação explícita de risco e não se torna uso autorizado;
- retenção: antes de cutover/outbox produtivo, não neste ciclo read-only/shadow.

Nenhuma nova funcionalidade foi iniciada durante esta disposição. O objetivo desta pausa
foi firmar o alicerce e impedir que a próxima implementação crie um terceiro escritor
UTM, bindings CRM por heurística ou outra camada operacional paralela ao Odoo.
