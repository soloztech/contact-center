# Pipeline de atendimento no core

Data da decisão: 2026-08-31.

Status: implementado e implantado no SERVIDOR05 em 2026-09-01 no
`contact_center_base 16.0.1.30.1` e `contact_center_crm 16.0.2.0.1`. Evidência e totais
de teste:
[`../reviews/2026-09-01-owner-team-pipeline-crm-release-validation.md`](../reviews/2026-09-01-owner-team-pipeline-crm-release-validation.md).

## Separação canônica

- `mail.channel` continua sendo a conversa e o histórico de mensagens.
- `mail.channel.contact_center_state` continua sendo o estado operacional da conversa
  (`open`, `pending`, `resolved`).
- `contact.center.case` representa um assunto comercial ou de atendimento. Uma conversa
  pode ter vários casos, cada um em uma etapa diferente.
- Mover um caso no Kanban não altera o estado operacional da conversa, e alterar o
  estado operacional não move casos.

Essa separação permite que duas propostas, tickets ou oportunidades coexistam na mesma
conversa sem forçar um único estágio para todos os assuntos.

## Modelos do core

- `contact.center.pipeline`: pipeline neutro por empresa, reutilizável por várias caixas
  e equipes.
- `contact.center.pipeline.stage`: etapas estáveis por pipeline; existe no máximo uma
  etapa inicial por pipeline.
- `contact.center.case`: caso ligado à conversa, pipeline e etapa, com responsável,
  prioridade, datas e revisão monotônica.
- `contact.center.case.transition`: ledger imutável de transições, com UUID idempotente,
  ator, origem e revisões anterior/nova.

O pipeline padrão possui as etapas `new`, `in-progress`, `waiting` e `done`. O
provisionamento é idempotente e cria um caso padrão para conversas históricas.

## Caixa exclusiva ou compartilhada

O caso projeta a equipe da conversa: `case.team_id` deve ser exatamente igual a
`channel.contact_center_team_id`. Portanto:

- caixa apenas com dono: o caso fica sem equipe;
- caixa compartilhada: o caso usa a equipe da conversa;
- caixa com dono e equipe: o responsável pode pertencer à união do dono com o roster da
  equipe.

Trocar a equipe de uma caixa é fail-closed: a nova equipe precisa ter todos os pipelines
já usados pelos casos da conversa habilitados previamente. A troca de acesso nunca
amplia silenciosamente o catálogo operacional da equipe.

A escolha do pipeline segue esta ordem: pipeline informado explicitamente,
`account.default_pipeline_id`, `team.default_pipeline_id` e, por fim, pipeline padrão da
empresa.

## Transições

Toda mudança de etapa passa por:

```python
case.action_transition(
    target_stage_id,
    expected_revision=case.stage_revision,
    request_uuid="UUID",
    source="integration",
)
```

O bloqueio pessimista evita duas mudanças concorrentes sobre a mesma revisão. O UUID
torna a repetição segura, e o histórico não pode ser alterado nem apagado. Existe o hook
transacional `_contact_center_after_transition(transition)` para os addons de CRM e
Helpdesk projetarem a mudança sem acoplar esses módulos ao core.

## Integrações opcionais

O CRM é integrado pelo addon opcional `contact_center_crm`, com dois contratos
separados:

- o binding de equipe 1:1 projeta o roster CRM na equipe nativa do Contact Center;
- o binding de pipeline projeta o catálogo de `crm.stage` e mantém a sincronização dos
  casos e leads, independentemente do ciclo de vida do espelho de roster.

No Odoo 16, `crm.stage` não possui campo `active`. A convergência do catálogo é coberta
por criação, edição, mudança de equipe e exclusão; não existe uma operação genérica de
arquivamento de etapa a ser espelhada.

Cada caso aponta para no máximo um `crm.lead`; como uma conversa pode ter vários casos e
um lead pode receber casos de várias conversas, conversa e lead formam uma relação N:N
explícita através dos casos. Nenhum dos modelos é mesclado ou substituído.

Helpdesk permanece uma integração futura em addon próprio e deve seguir o mesmo padrão
tipado. Nenhuma ponte pode substituir `mail.channel`, escrever `stage_id` diretamente
nem reutilizar `contact_center_state` como etapa comercial.
