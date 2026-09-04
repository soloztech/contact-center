# Separação dos serviços de aplicação

Data: 2026-08-29 Estado: implantado e homologado no SERVIDOR05

## Objetivo

Reduzir o acoplamento físico de `application.py` sem alterar comportamento, nomes de
modelos, RPCs, DTOs, regras de acesso, ordem de locks ou integração com providers.

## Cortes realizados

1. `contact.center.ui.api` foi movido integralmente para `models/ui_api.py`.
2. Vinte métodos exclusivamente outbound foram movidos para
   `models/application_outbound.py`, que estende a fachada existente com
   `_inherit = "contact.center.application"`.
3. `_group_own_protocol_participant` e `_group_echo_protocol_participant` permaneceram
   no core porque também atendem inbound, queue e correlação de ecos.
4. `IdentityConflictError` permaneceu em `models/application.py`, preservando os imports
   usados pela queue e pelos testes.

O arquivo original caiu de 5.306 para aproximadamente 2.300 linhas. A UI ficou isolada
em aproximadamente 2.100 linhas e o outbound em aproximadamente 900 linhas.

## Contratos preservados

- modelos `contact.center.application` e `contact.center.ui.api`;
- 19 RPCs públicos da UI e seus formatos de resposta;
- entrada provider → DTO → inbox → application;
- chamadas da queue, controllers e modelos auxiliares;
- idempotência, ACLs, locks, outbox e notificações do bus;
- categoria histórica do logger outbound.

## Validação local

- 57/57 métodos da UI com AST idêntica;
- 62/62 métodos da aplicação com AST idêntica após a composição core + outbound;
- uma única definição de cada método e nenhum contrato ausente;
- Black, isort, Flake8, compileall, Pylint obrigatório, checks OCA e `git diff --check`:
  aprovados;
- teste de contrato do registry adicionado para a fachada e todos os RPCs;
- base implantada `16.0.1.24.1`;
- commit de código `3b323ab`;
- árvore implantada: `27f632b9a91f93c0761b2faacffb17f5596c50210439bf105c2d6f04c8383a57`.

## Homologação remota

O primeiro preflight foi interrompido antes de qualquer mudança porque a estação estava
na rede `192.168.68.0/24`, sem rota para SERVIDOR04/05 em `192.168.2.0/24`. Após a rota
ser restabelecida, o ciclo foi retomado sem repetir trabalho já validado.

- release atômica: `applied_and_validated`, com upgrade offline e HTTP 200 após retorno;
- versões instaladas: base `16.0.1.24.1`, WuzAPI `16.0.1.19.0`, Meta `16.0.1.6.0` e UI
  `16.0.1.17.4`;
- fonte remota comprovada pela mesma árvore de 219 arquivos antes das suítes;
- testes base **296/296** e integrados **566/566**, sem falhas ou erros, em bancos
  temporários removidos ao final;
- registry instalado com `contact.center.application` e `contact.center.ui.api`, sem
  módulo pendente;
- QUnit minificado e `debug=assets`: **76/76** testes e **634/634** asserções em cada
  bundle, zero erro ou warning de console;
- smoke autenticado: 485 conversas, realtime ativo, saúde `5/7`, composer disponível e
  erro de microfone devolvendo foco à mensagem, com console limpo;
- tag final: `16.0.1.24.1-lab`.

Evidências:

- `scans/raw/20260829-odoo16-contact-center-application-refactor/release-apply/`;
- `scans/raw/20260829-odoo16-contact-center-application-refactor/tests-apply/`;
- `scans/raw/20260829-odoo16-contact-center-application-refactor/validate-installed/`;
- `scans/raw/20260829-odoo16-contact-center-application-refactor/browser-smoke/summary.json`.

O backup de banco foi dispensado no laboratório conforme a autorização permanente para
iterações de desenvolvimento. A release criou o backup recuperável da fonte em
`/home/administrador/odoo16/deploy-backups/contact-center/contact-center-20260829T170349203935Z`.
Produção SERVIDOR02 não foi acessada ou alterada.
