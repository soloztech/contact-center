# Extração do Contact Center Kanban

Data: 2026-09-04

## Decisão

Pipelines e casos não são requisitos da mensageria. A funcionalidade foi extraída do
`contact_center_base` para o addon opcional `contact_center_kanban`, sem camada de
compatibilidade, campos residuais ou imports condicionais no base.

O `contact_center_base` continua responsável por conversa, mensagem, identidade,
provider, filas, segurança de acesso, respostas rápidas, notas internas e mensagens
agendadas. O novo addon depende somente dele e acrescenta o aggregate operacional por
extensões `_inherit` normais do Odoo.

## Inventário transferido

- Modelos `contact.center.pipeline`, `contact.center.pipeline.stage`,
  `contact.center.case` e `contact.center.case.transition`.
- Campos de pipeline em `contact.center.team` e `contact.center.account`, além dos
  campos e ações de caso em `mail.channel`.
- Provisionamento idempotente do pipeline padrão e backfill do caso padrão.
- Ledger e regras de transição, incluindo o token process-local usado pela ponte CRM.
- Follow-ups baseados em `mail.activity` e seu receipt
  `contact.center.followup.request`, pois o alvo obrigatório é um caso.
- Views tree/form/Kanban, actions, menus, ACLs, record rules e testes correspondentes.
- Extensão do lock graph que preserva a ordem conta → equipe → usuário → pipeline →
  conversa → caso sem fazer o base conhecer tabelas opcionais.

Respostas rápidas, notas internas, mensagens agendadas e seus testes permaneceram no
base. A API de produtividade continua retornando um envelope válido sem o plugin; o
plugin acrescenta casos, atividades e a capability `followups` por `super()`.

## Dependência CRM

`contact_center_crm` passou a depender explicitamente de `contact_center_kanban`.
Referências de views/actions e o token de transição apontam para o novo namespace. Não é
possível instalar a ponte CRM sem o aggregate de casos que ela sincroniza.

## Ausências intencionais

- Nenhuma migração ou alias de XML ID foi mantido no base.
- Nenhum import opcional, teste de registry ou referência SQL a pipeline/caso foi
  mantido no base.
- A UI continua dependendo apenas do base e degrada pela capability; não ganhou uma
  dependência Python/manifest do plugin.
- Nomes físicos das tabelas e da relação `contact_center_team_pipeline_rel` foram
  preservados pelo novo addon, portanto a extração não reinventa o schema funcional.

## Cutover de uma base já instalada

Em banco novo, instalar `contact_center_base` e, quando desejado,
`contact_center_kanban`; instalar `contact_center_crm` inclui o plugin pela dependência.

Em banco que já recebeu a implementação antiga, base, Kanban e CRM precisam ser
atualizados no mesmo carregamento. As antigas record rules tinham `noupdate="1"` e seus
XML IDs pertencem a `contact_center_base`; elas não devem coexistir com as novas regras
de `contact_center_kanban`. O runbook de cutover deve remover explicitamente esses XML
IDs antigos antes da validação final ou partir de uma base greenfield. Essa limpeza
pertence ao rollout, não a uma compatibilidade permanente no código do base.

Os scripts externos de laboratório que enumeram addons de forma rígida também precisam
incluir `contact_center_kanban` e o test tag `/contact_center_kanban` antes do próximo
release atômico.

## Gates de validação

1. Instalação isolada de `contact_center_base`, provando que os modelos de pipeline,
   caso e follow-up não existem no registry.
2. Instalação de `contact_center_kanban`, provisionamento idempotente e execução de sua
   suíte de pipeline, concorrência e produtividade.
3. Instalação integrada de `contact_center_crm`, validando bindings e sincronização de
   etapas.
4. Auditoria do banco garantindo ausência dos XML IDs antigos de segurança e exatamente
   um conjunto ativo de regras do plugin.
