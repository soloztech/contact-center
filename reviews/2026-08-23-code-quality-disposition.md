# Disposição da revisão de qualidade de código

- Data: 2026-08-23
- Fonte: [revisão do Claude](2026-08-23-code-quality.md)
- Escopo: qualidade e manutenibilidade sem alterar o contrato funcional das Fases 2.1, 3
  ou do M4
- Versões implantadas: `contact_center_base` `16.0.1.7.1`, `contact_center_wuzapi`
  `16.0.1.5.1` e `contact_center_ui` `16.0.1.5.3`
- Status: aplicada e validada somente no SERVIDOR05

## Veredito

A recomendação central fazia sentido: as funções acima dos limites configurados pelo
próprio repositório deveriam ser decompostas antes das próximas mudanças semânticas.
Entretanto, parte do diagnóstico estava desatualizada e o resultado real do conjunto de
hooks era diferente do relatório:

| Verificação                | Resultado encontrado antes da correção                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Black                      | três arquivos fora do formato, não uma execução limpa                                                                           |
| Flake8 C901                | cinco funções, incluindo `_apply_normalized_health` do M4, não quatro                                                           |
| isort                      | um arquivo de teste real fora de ordem; `__init__.py` continuam excluídos pelo hook OCA                                         |
| Prettier                   | os dois XML citados já estavam formatados                                                                                       |
| ESLint                     | duas funções acima de `complexity: 15`, como informado                                                                          |
| Pylint obrigatório do hook | traduções nos controllers, SQL dinâmico de migração, commits intencionais de teste, `except-pass` e import absoluto em migração |

Os avisos `no-raise-unlink`, `no-wizard-in-models`, chaves de manifest e cobertura de
docstrings vieram de uma execução indicativa mais ampla, não do gate obrigatório atual.
Eles não demonstravam defeito funcional e não foram usados para justificar churn neste
release.

## Aplicado

- `_send_message`, `_send_message_mutation`, `update_conversation`, `download_media` e
  `_apply_normalized_health` foram divididos em helpers coesos. A ordem crítica de ACL,
  locks, idempotência, persistência e I/O foi preservada.
- `loadConversations` e `loadTimeline` foram decompostos em helpers de revisão,
  paginação, merge e falha; foram adicionados testes para respostas concorrentes, stale
  e paginação.
- Os limites comuns de retry passaram para `services/job.py` como
  `QUEUE_ATTEMPT_CEILING = 12` e `PROVIDER_PAUSED_RETRY_SECONDS = 60`. Não foi criado um
  mixin genérico porque inbox, outbox, mídia e health possuem estados terminais e
  boundaries diferentes.
- Mensagens dos controllers passaram por `_()`, o módulo `uuid` deixou de sombrear o
  parâmetro público da rota, o SQL da migração usa `psycopg2.sql.Identifier` e a
  migração histórica WuzAPI deixou de importar o addon corrente.
- Commits necessários aos testes com dois cursores foram documentados com disable local.
  O `except-pass` do adapter foi substituído por tratamento explícito.
- Black, isort e os demais ajustes mecânicos foram aplicados nos arquivos realmente
  apontados pelas ferramentas.

## Adiado deliberadamente

- A separação física de `application.py`, do store e do SCSS fica para um release
  estrutural próprio. Fazê-la junto desta correção aumentaria muito o diff sem ganho de
  comportamento.
- Um `contact.center.job.mixin` amplo não foi adotado; compartilhar somente constantes
  eliminou os literais sem apagar as diferenças de retry e segurança entre ledgers.
- Não foi criado helper genérico `_as_system()` para concentrar `sudo()`. A elevação
  permanece visível no ponto de uso, junto da checagem de empresa, roster e membership.
- Wizard, manifests, docstrings e suppressions de `unlink` não foram movimentados para
  satisfazer apenas checks que não fazem parte do gate configurado.
- M7, M8, M10 e M11 continuam separados por alterarem semântica ou transporte; esta
  rodada não os implementa implicitamente.

## Validação

- gates estáticos: Black, isort, Flake8, Pylint obrigatório, Prettier, ESLint,
  `node --check`, `compileall` e checks OCA passaram sem erro; Flake8 e ESLint ficaram
  sem violações de complexidade;
- testes Odoo isolados: **94/94** no core e **145/145** integrados, sem falhas ou erros;
- QUnit autenticado: **30/30 testes e 192/192 asserções**, sem falhas;
- browser: cliente Owl carregado, frota expandida com cinco números, **5/5 conectadas**
  após a convergência dos probes e zero erro no console;
- árvore implantada: `340b759bb389b6063b173a474a81f8d3cc86c4de20c9613c7576cc01a3af36c1`;
- upgrade sem backup do banco descartável, conforme decisão explícita; registros de
  negócio permaneceram inalterados, nenhuma mensagem WhatsApp foi enviada e produção não
  foi acessada nem modificada.

Evidências principais:

- deploy:
  `scans/raw/20260823-odoo16-contact-center-code-quality/deploy/20260823T230214250641Z/`;
- testes:
  `scans/raw/20260823-odoo16-contact-center-code-quality/test/20260823T230241566914Z/`;
- dry-run:
  `scans/raw/20260823-odoo16-contact-center-code-quality/dry-run/20260823T230351070423Z/`;
- upgrade:
  `scans/raw/20260823-odoo16-contact-center-code-quality/upgrade/20260823T230414859960Z/`;
- validação:
  `scans/raw/20260823-odoo16-contact-center-code-quality/validate/20260823T230445237011Z/`.
