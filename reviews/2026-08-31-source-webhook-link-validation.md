# Rastreabilidade da mensagem ao webhook — validação de 2026-08-31

## Resultado

Implementação concluída e implantada somente no SERVIDOR05. Usuários do grupo nativo
`base.group_system` veem **Ver webhook de origem** no menu da mensagem quando existe uma
origem comprovada. A ação abre o `contact.center.inbox.event` exato em modal.

O modo `?debug=1` não concede acesso: ele é uma preferência do cliente e pode ser
habilitado por qualquer usuário interno. A autorização efetiva é a ACL do administrador
do sistema, aplicada no campo, no UiDTO, na capability do bootstrap e novamente na UI.

## Contrato implementado

- `contact.center.message.binding.source_inbox_event_id` é readonly, indexado,
  `ondelete=restrict` e write-once.
- A origem precisa compartilhar provider connection, conta, empresa e escopo de conversa
  com a mensagem.
- Somente mensagens de origem `provider` ou `external_device` recebem o vínculo.
- Mensagens já criadas pelo composer do Odoo permanecem sem origem quando o echo apenas
  as reconcilia.
- O backfill associa apenas candidatos históricos únicos por ID externo ou ID do
  cliente; casos ausentes ou ambíguos permanecem vazios.
- O frontend normaliza o ID, exige capability booleana estrita, revalida no clique e
  fecha menus abertos se a autorização ou a origem forem revogadas.

## Evidências

- Backfill no banco real do laboratório: **10.148** bindings vinculados, sendo **8.033**
  `provider`, **2.115** `external_device`, zero `agent` e zero `automation`.
- Testes canônicos: **365/365** base e **714/714** integrados, sem falha ou erro.
- QUnit filtrado/minificado: **83/83** testes e **724/724** asserções.
- Smoke autenticado: menu visível, navegação para o Inbox Event correto e console com
  zero erro e zero warning.
- A execução adicional com `debug=assets` não chegou ao QUnit. Centenas de requisições
  paralelas de arquivos no Odoo single-process elevaram a memória até o container sair
  com código `137`; portanto não houve falha de assertiva e esse modo não é declarado
  como aprovado. A sessão de navegador que gerava a carga foi encerrada. O Odoo, o DB
  Manager e o arquivo exato da rota de laboratório foram restaurados e validados por
  HTTP 200; o SHA-256 da rota permaneceu
  `2b06d3c38cbac9df1d320a5ec8e1c3dda24c80883eafe059307020c572b34383`.

Capturas:

- `/home/lucaszotelli/infra-ai-ops/output/playwright/source-webhook-menu.png`
- `/home/lucaszotelli/infra-ai-ops/output/playwright/source-webhook-dialog.png`
- `/home/lucaszotelli/infra-ai-ops/output/playwright/source-webhook-qunit-min.png`

Release final do laboratório:

- árvore: `a3eb067e3ba38a4052f0976b580126472049a3d4e845b533ba47bcb6166ac4e5`;
- versões: base `16.0.1.29.1`, WuzAPI `16.0.1.26.4`, Meta `16.0.1.10.0`, UI
  `16.0.1.18.4`;
- release:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260831T145454952461Z`;
- testes finais da árvore combinada:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260831-odoo16-contact-center-group-fromme-bootstrap/test`;
- teste dedicado anterior do vínculo:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260831T125619720345Z`.

Produção não foi acessada nem alterada.
