# Validação final do Meta greenfield — 2026-09-01

## Veredito

O runtime Meta compartilhado e o adapter `contact_center_meta` foram reinstalados do
zero, provisionados e reexecutados de forma idempotente no SERVIDOR05. Messenger e
Instagram estão `connected + healthy`, com outbound habilitado, sem job pendente e sem
duplicação de caixas ou subscriptions. Produção não foi acessada.

## Correção aplicada

A falha observada não era de token. O token `PAGE` era válido, pertencia ao App correto
e tinha os escopos granulares necessários. O plano de reconciliação misturava os cinco
campos do objeto `instagram` aos oito campos do objeto `page` no POST da borda
`/{page-id}/subscribed_apps`; a Graph API rejeitava a união com HTTP 400/code 100.

`meta_webhook_base 16.0.1.1.1` mantém subscriptions separadas por `object_type` no App e
instala na Facebook Page apenas os campos `page`. Os testes cobrem união global,
reconciliação mista e configuração Instagram-only. O configurador greenfield também
passou a falhar imediatamente quando um job termina em erro ou desaparece, sem aceitar
um estado saudável antigo enquanto a nova execução ainda está ativa.

## Evidências canônicas

- Integration Core:
  `scans/raw/20260831-odoo16-integration-core-shared-meta/release/20260901T040611534230Z`
  — `meta_api_base` **39/39**, `meta_webhook_base` **38/38**, upgrade offline, replay e
  rota pública 200.
- Meta greenfield:
  `scans/raw/20260831-odoo16-contact-center-meta-greenfield/release/20260901T040924773312Z`
  — **172/172**, expurgo somente dos registros Meta provisórios, instalação do zero e
  replay.
- Provisionamento fresco:
  `scans/raw/20260831-odoo16-contact-center-meta-fresh-provision/20260901T041207125457Z`
  — contas/conexões **11/12**, 8 subscriptions `page` + 5 `instagram`, Page e endpoint
  `in_sync`, ambas as conexões `connected + healthy`.
- Release integral do Contact Center:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T041305899881Z`
  — base **386/386**, integrada **714/714**, HTTP interno e público 200.
- Replay idempotente final:
  `scans/raw/20260831-odoo16-contact-center-meta-fresh-provision/20260901T042054598156Z`
  — `created=false`, mesmos IDs, health renovado e nenhum job pendente.
- WuzAPI operacional:
  `scans/raw/20260824-odoo16-contact-center-phase5-3-group-events/webhooks/20260901T042548249655Z/result.json`
  — cinco sessões conectadas, logadas e já reconciliadas, sem alteração planejada.
- Navegador autenticado:
  `output/playwright/contact-center-meta-20260901/.playwright-cli/page-2026-09-01T04-31-05-706Z.png`
  — ambas as caixas Meta aparecem conectadas; RPCs observados retornaram 200 e o console
  apresentou zero erros.

## Observação operacional

O painel mostra **7/8 conectadas** porque existe uma conexão manual WuzAPI chamada
`teste`, em papel de migração, sem sessão autenticada. As cinco conexões WuzAPI
operacionais e as duas conexões Meta estão conectadas. Esse registro de teste não foi
apagado por não fazer parte da reinstalação Meta.
