# Validação — dono/equipe, pipelines e ponte CRM

- Data: 2026-09-01
- Alvo: laboratório descartável SERVIDOR05, banco neutralizado `odoo16`
- Produção/SERVIDOR02: não acessada nem alterada

## Resultado

O modelo de acesso por dono individual e/ou equipe, o core de pipelines/casos e a ponte
opcional com CRM foram implantados no SERVIDOR05. A liberação terminou com status
`applied_and_validated`, integridade da árvore confirmada e HTTPS público 200.

Arquitetura validada:

- `owner_user_id` é um grant individual opcional da caixa;
- `default_team_id` é o grant compartilhado opcional, e o escopo efetivo é a união;
- vínculo CRM de equipe 1:1 torna o roster comercial autoridade do roster Contact
  Center, sem reutilizar record rules do CRM;
- vínculo de pipeline é independente do vínculo de roster;
- casos são o objeto operacional do Kanban e permitem vários assuntos/leads na mesma
  conversa sem fundir registros.

## Suítes e versões

- core: **387/387**, zero falha e zero erro;
- integrada: **719/719**, zero falha e zero erro;
- `contact_center_base 16.0.1.30.1`;
- `contact_center_wuzapi 16.0.1.26.5`;
- `contact_center_meta 16.0.2.0.0`;
- `contact_center_crm 16.0.2.0.1`;
- `contact_center_ui 16.0.1.18.9`;
- `meta_api_base 16.0.1.1.0`, `meta_webhook_base 16.0.1.1.1` e OCA
  `queue_job 16.0.3.0.2` preservados.

Evidência canônica:
`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T051354186268Z`.
O hash da árvore implantada é
`82ca63eb0b388df5ee516f1d47762f0a6ec273bf7904e8bfb8e12e6e2a932803`.

## Tentativas recuperadas antes do corte final

Duas tentativas falharam antes de qualquer upgrade e foram recuperadas automaticamente;
em ambas, a fonte anterior e a rota byte-idêntica foram restauradas e o endpoint voltou
a responder 200:

1. `20260901T045634886308Z`: um teste pressupunha incorretamente o campo `active` em
   `crm.stage` no Odoo 16, e uma requisição de healthcheck concorrente interferiu em um
   `HttpCase`;
2. `20260901T050427237439Z`: permaneceu apenas a interferência do healthcheck no cursor
   compartilhado do `HttpCase`.

A correção removeu a hipótese inválida sobre `crm.stage`, ativou a guarda estrita nos
testes HTTP e desabilitou o healthcheck somente no contêiner efêmero das suítes. O
override temporário é validado, tem modo 0600 e é removido no caminho normal e no de
falha. O contêiner principal e seu healthcheck não foram enfraquecidos.

## Liberação

- upgrade offline dos cinco addons: concluído;
- módulos pendentes: zero;
- contêiner Odoo e gerenciador do banco: `running`;
- rota Traefik restaurada com o mesmo SHA-256;
- banco temporário, contêiner efêmero e override temporário: removidos;
- backup do laboratório: dispensado conforme decisão explícita do operador;
- produção tocada: `false`.

## Estado operacional pós-deploy

- Odoo e PostgreSQL: `running/healthy`, sem reinício inesperado;
- provedores operacionais: cinco WuzAPI e duas Meta em `connected/healthy`;
- a sexta conexão WuzAPI permanece deliberadamente em papel `migration`, com tráfego
  inbound/outbound desativado e autenticação pendente;
- 559 conversas Contact Center possuem exatamente 559 casos ativos/padrão e 559
  transições de criação; não há conversa sem caso nem caso padrão duplicado;
- uma pipeline padrão com quatro etapas e exatamente uma etapa inicial foi provisionada;
- não existe job aberto nem falha criada após a liberação. Um conflito transitório de
  serialização foi repetido automaticamente e terminou em `done`;
- os bindings administrativos com equipes/pipelines reais do CRM permanecem em zero. O
  código e o schema estão prontos, mas as equipes atuais do laboratório não possuem
  correspondência de nomes ou membros suficiente para inferir uma associação segura.

O banco ainda contém débito histórico anterior ao release: 276 jobs `failed` e 1.100
inbox events `dead`. Nenhum deles foi reexecutado ou apagado nesta liberação para evitar
uma avalanche retroativa; não há registro novo dessas classes após o deploy.
