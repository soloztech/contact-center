# Produtividade operacional do Contact Center — decisão de arquitetura

Data: 2026-09-02

## Decisão

Esta fase acrescenta produtividade sem furar o fluxo canônico
`UI -> UiAPI -> application service -> outbox -> provider`.

- **Respostas rápidas:** reutilizam `mail.shortcode`, o cadastro nativo de respostas
  prontas do Odoo. Não é criado um catálogo paralelo no Contact Center.
- **Nota interna:** é uma `mail.message` com subtipo `mail.mt_note`, sem destinatários,
  binding externo ou outbox. A criação pela UI é idempotente e a atualização usa o bus
  do próprio Contact Center.
- **Follow-up:** usa `mail.activity` sobre `contact.center.case`. Assim uma conversa com
  mais de um assunto mantém atividades separadas por caso e, futuramente, o caso ligado
  ao CRM pode conviver com as atividades comerciais do `crm.lead` sem duplicação
  automática.
- **Filtros e atalhos:** são apenas projeções da API autorizada. O filtro de não lidas é
  calculado para o membro atual do canal; a contagem de outro atendente não pode tornar
  a conversa visível por engano.
- **Rascunho:** anexos e objetos `File` continuam somente em memória. Apenas texto e
  modo do composer são persistidos em `sessionStorage`, isolados por banco/usuário e
  removidos após a ação concluída.
- **Mensagem agendada:** não usa `mail.message.schedule`. O modelo nativo posterga
  somente `_notify_thread()` de uma `mail.message` já criada e não cria outbox nem chama
  adapter. O Contact Center usa um intent durável, provider-neutral, cancelável e
  idempotente. Quando vence, ele revalida acesso e chama o envio canônico; nunca chama o
  provider diretamente.

## Limites do primeiro corte

- Agendamento aceita somente texto. Upload temporário não é uma referência durável;
  mídia agendada exigirá cópia antecipada para anexo privado e fica para outro corte.
- Respostas rápidas são globais aos usuários internos, seguindo a semântica nativa de
  `mail.shortcode`. Escopo por empresa/equipe só deve ser adicionado se surgir uma
  necessidade real.
- Atividade operacional fica no caso. Não há sincronização automática com atividade do
  lead neste corte.
- Um job com ETA não é a única fonte da verdade: uma recuperação periódica reprograma
  intents vencidos que perderam o job durante restart ou falha do runner.

## Critérios de aceite

1. Repetir a mesma requisição de nota ou agendamento converge no mesmo registro.
2. Nota interna nunca produz `contact.center.message.binding` nem
   `contact.center.outbox.command`.
3. Usuário sem acesso à conversa/caso não consegue criar, concluir ou cancelar itens.
4. Cancelar antes do vencimento torna qualquer job já enfileirado um no-op.
5. Reiniciar o runner não perde um agendamento vencido.
6. O envio agendado cria mensagem/outbox somente no vencimento e preserva a idempotência
   em retry após falha intermediária.
7. Trocar de conversa e recarregar a aba restaura apenas texto/modo do rascunho correto;
   nenhum blob, token de upload ou anexo é serializado no navegador.
8. Filtros, atalhos e ferramentas do composer continuam utilizáveis em desktop e mobile,
   com foco visível e sem capturar teclas durante digitação.

## Estado da implementação

Implementação implantada e validada no SERVIDOR05 para `contact_center_base 16.0.1.39.0`
e `contact_center_ui 16.0.1.26.0`:

- respostas rápidas pela estrutura nativa `mail.shortcode`;
- notas internas como `mail.message`/`mail.mt_note`, sem provider, binding externo ou
  outbox;
- follow-up por `mail.activity` em `contact.center.case`;
- filtros operacionais e atalhos de teclado na interface;
- rascunho por conversa em `sessionStorage`, limitado a texto e modo do composer;
- agendamento provider-neutral durável, cancelável e idempotente, executado por
  `queue_job` e pelo fluxo canônico de envio.

O agendamento nativo `mail.message.schedule` não foi reutilizado porque posterga a
notificação de uma `mail.message` já criada. Ele não representa um intent de envio
externo, não cria a outbox do Contact Center e não atravessa o adapter do provider.

Validação final:

- **Deploy SERVIDOR05:** release atômico
  `scans/raw/20260902-cc-productivity-release-r3`, árvore
  `3767a84028f4e61bcc278ebd40cd3127117420b5d972fab057e574efc362e7bf`, upgrade concluído,
  todos os cinco addons instalados e HTTP público `200`.
- **Testes Odoo:** base **432/432**, WuzAPI **204/204** e integração **783/783**, sem
  falha ou erro.
- **QUnit:** bundles minificado e `debug=assets` com **128/128** testes e **1144/1144**
  asserções, sem falha, erro ou warning de console.
- **Smoke autenticado:** desktop `1440 x 1000` e mobile `390 x 844`, realtime ativo e as
  quatro ferramentas visíveis. A troca A -> B -> A restaurou o rascunho correto e ele
  foi removido ao final; nenhuma mensagem externa ou registro operacional foi criado.
  Evidência local em `output/playwright/20260902-cc-productivity/summary.json`.
- Uma tentativa anterior (`...release-r2`) falhou fechada apenas por quatro fixtures de
  data e uma precondição de unread nos testes novos. O gate parou antes do upgrade,
  restaurou a fonte anterior e confirmou HTTP `200`; os fixtures foram corrigidos antes
  do release final.

Gate conhecido antes de produção: concluir o contrato de concorrência para edição e
arquivamento simultâneos do catálogo de pipelines. Isso não bloqueia o laboratório
enquanto o catálogo permanecer congelado durante a validação desta fase.
