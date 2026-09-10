# Auditoria pós-implementação — 2026-09-08 a 2026-09-10

Data: 2026-09-10. Fonte: branch `16.0`, `c5d37bb~1..5bd6381` (22 commits, 255 arquivos),
sem alterações locais versionadas. `origin/16.0` está no mesmo commit. Auditoria somente
leitura; nenhum arquivo do produto, laboratório ou produção foi alterado.

## Escopo

| Entrega                                           | Commits                                    | Addons           |
| ------------------------------------------------- | ------------------------------------------ | ---------------- |
| Iniciar conversa por telefone                     | `10df9c2`, `e096ed6`, `a389b06`, `ebe69fa` | Base, UI, WuzAPI |
| Motivos de resolução e auditoria de reabertura    | `4ff6991`, `83403da`                       | Base, UI         |
| Respostas rápidas pessoais, marcadores, workspace | `8ed0661`, `17b9773`                       | Base, UI         |
| Filtros combinados de estado, responsável e tags  | `9d5c6cb`, `10df9c2`                       | Base, UI         |
| Leitura a partir do conteúdo visível, histórico   | `9ef0da8`, `66c9bd5`, `2b1d22f`, `5d690e0` | Base, UI         |
| Cotações pela empresa comercial                   | `0685e2a`, `b7f4819`                       | CRM, UI          |
| Roteamento de chamadas por identidade verificada  | `dc01a21`, `29caa73`, `03a6a38`, `5bd6381` | WuzAPI           |
| Números centrais liberados a agentes              | `66c9bd5`                                  | Base             |

## Parecer de arquitetura

A separação núcleo, adaptadores, interface, CRM e Kanban foi preservada. As novas
capacidades entraram pelos mecanismos já canônicos:

- O início de conversa estende o contrato do adapter com dois métodos opt-in
  (`supports_direct_conversation_start`, `resolve_direct_address`) e um DTO congelado
  (`DirectAddressResult`) que valida um único endereço primário com confiança de
  protocolo. O I/O com o provedor acontece depois da autorização e antes de qualquer
  lock; a projeção reutiliza `_resolve_identity`, `_resolve_channel` e
  `_enrich_channel_aliases` dentro de um savepoint, revalidando configuração e escopo
  após o I/O, na ordem canônica conta → conexões → identidade → vínculo. O contrato
  antigo de `_resolve_channel` foi mantido para os chamadores inbound.
- A resolução com motivo é uma faixa de serviço sobre a cerca de produtividade e o
  ledger de notas internas existentes: recibo imutável, replay idempotente por UUID e
  hash de payload, revisão otimista do canal e supressão da nota genérica de transição.
- Respostas pessoais adicionam um escopo ao modelo existente com defesa em profundidade
  nos `create`/`write`/`unlink` do vínculo e do `mail.shortcode`.
- O roteamento de chamadas passa a depender de prova de identidade própria gravada pelo
  job de saúde em campo protegido por token; o ingresso continua sem I/O.
- A normalização de telefone é deliberadamente conservadora: nunca insere ou remove o
  nono dígito; só o provedor confirma a equivalência.

Não foi identificado motivo para nova camada, tabela auxiliar ou mudança de fila. A
dependência Python `phonenumbers` é a única adição de infraestrutura e segue a base
técnica do `phone_validation` nativo.

## Evidência de execução

| Verificação                                     | Resultado                                                                                                                                                                                                                                        |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| CI GitHub `tests`, run `34523064000` no HEAD    | Sucesso em 2026-09-10 19:54–20:02 UTC, `workflow_dispatch` com `peer_ref` `69e589a` (head do Marketing Center). `0 failed, 0 error(s) of 1156 tests` na instalação conjunta; `25` testes CRM independente; `42` testes de documentos do cliente. |
| Por addon (mesmo run)                           | Base 717, WuzAPI 257, Kanban 156, Meta 141, CRM 35, UI 4 (Python).                                                                                                                                                                               |
| CI GitHub `pre-commit` no HEAD                  | Sucesso.                                                                                                                                                                                                                                         |
| Runs de `push` dos quatro últimos commits       | Cancelados pelo grupo de concorrência do próprio workflow; a validação do HEAD depende exclusivamente do dispatch manual acima.                                                                                                                  |
| Lint local reproduzido (mesmas versões fixadas) | flake8, black, isort, pylint-odoo obrigatório e opcional (Python 3.10.20), oca-checks-odoo-module, oca-checks-po, prettier, XML bem formado, CSV de acesso íntegro: zero mensagens nos 68 arquivos alterados.                                    |
| eslint (config do repositório)                  | 0 erros; 17 avisos, dos quais 2 introduzidos: complexidade 20 em `setFilter` e 19 em `startConversation` (limite 15).                                                                                                                            |
| QUnit (5 suítes, 4 novas)                       | **Não executado.** O CI não roda `web.qunit_suite_tests`; não há registro em `reviews/` para esta faixa. Ver achado 2.                                                                                                                           |
| Runtime Odoo nesta máquina                      | Inexistente (sem Odoo, PostgreSQL ou `phonenumbers`). Nenhum teste foi executado no laboratório por esta auditoria.                                                                                                                              |

## Achados

| Prioridade | Achado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Localização                                                                                                                                                                     |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alta       | **A confidencialidade da resposta pessoal não é garantida.** O Odoo 16 carrega todas as respostas prontas em `res.users._init_messaging` com `self.env['mail.shortcode'].sudo().search_read([], ['source', 'substitution'])` (fonte oficial 16.0, `addons/mail/models/res_users.py`). A regra global `rule_contact_center_shortcode_personal_content` não se aplica a esse caminho: todo usuário interno recebe atalho e corpo de todas as respostas pessoais ao abrir o Discuss. Não há override de `_init_messaging` nos addons e nenhum teste cobre leitura por usuário fora da Central.    | `contact_center_base/security/contact_center_productivity_security.xml:11-22`, `contact_center_base/models/quick_reply.py`                                                      |
| Média      | **Camada JS sem evidência de execução.** Cerca de 1.800 linhas de QUnit novas e mudanças substanciais em store, lista, timeline e compositor; o CI executa apenas Python. As entregas anteriores registravam QUnit em `reviews/` nos dois modos de assets; esta faixa não tem registro.                                                                                                                                                                                                                                                                                                        | `contact_center_ui/static/tests/*.esm.js`, `.github/workflows/test.yml`                                                                                                         |
| Média      | **Ampliação de privilégio sem registro.** Agentes passaram a vincular e criar empresas, inclusive números centrais (antes supervisores). As asserções negativas de `search_partner_companies`, `link_partner_company` e `create_and_link_partner_company` foram removidas do teste e não há teste de negação por escopo de conversa. README e `reviews/` não registram a decisão.                                                                                                                                                                                                              | `contact_center_base/models/ui_api.py:1674-1680, 2910-2948`, `contact_center_base/models/identity.py:323-333, 888-896`                                                          |
| Média      | **Lacunas de cobertura.** Sem teste para: multi-empresa e chamada via `sudo()` em `start_conversation`; resultado LID-primário passando pelo `start_conversation`; início concorrente do mesmo telefone; filtro OR de tags com duas ou mais conversas e paginação por cursor (fixture tem uma conversa); cerca de empresa em `search_quick_replies`; `get_health` quando `/user/lid` falha; verificação por módulo de versão em `native_access_upgrade_rehearsal.py`.                                                                                                                          | `contact_center_base/tests/test_start_conversation.py`, `test_multi_access_ui.py:209-236`, `contact_center_wuzapi/tests/test_adapter.py`                                        |
| Média      | **Convenção de idioma quebrada e tradução morta.** O `pt_BR.po` usa msgids em inglês e o plano fixa código em inglês; a faixa introduz 57 strings-fonte em português no Base (msgids, `string=`, `_description`, `_sql_constraints`, dados e views), contra 1 msgid anterior nos modelos. O msgid `"Choose a specific responsible user or a responsibility scope, not both."` do `.po` não corresponde ao código, que omite `specific`: essa tradução nunca é aplicada.                                                                                                                        | `contact_center_base/i18n/pt_BR.po:319` vs `contact_center_base/models/ui_api.py:79`; `models/resolution.py`, `models/start_conversation.py`, `data/resolution_reason_data.xml` |
| Média      | **Documentação e versionamento defasados.** README afirma que os seis addons estão em `16.0.1.1.0`; os manifests estão em `16.0.1.2.0` e `16.0.1.2.2`. README e `operations/` não mencionam início de conversa, motivos de resolução, respostas pessoais nem o gate de deploy da dependência `phonenumbers` (o `-u contact_center_base` falha se o venv não a tiver). O CRM mudou comportamento sem bump. `research/start_conversation_spec.md` é referenciado por documento versionado mas está fora do Git. O `peer_ref` padrão do workflow (`3591efab`) difere do par validado (`69e589a`). | `README.md:9`, `operations/conversation-actions.md`, `contact_center_crm/__manifest__.py`, `.github/workflows/test.yml:50`                                                      |
| Baixa      | `DTOValidationError` herda de `ValueError`, não de `AdapterError`. Um adapter que produza `DirectAddressResult`/`AddressDTO` inválido propaga erro genérico ao agente em vez de `UserError`. Inalcançável com o WuzAPI atual (regex fecham os valores), mas a fronteira do contrato deveria converter.                                                                                                                                                                                                                                                                                         | `contact_center_base/models/start_conversation.py:186-198`, `services/dto.py:85`                                                                                                |
| Baixa      | Semente de motivos só para `base.main_company` com `noupdate="1"`; outras empresas começam sem motivos. A ação de motivos abre lista `tree,form` em `target="new"`.                                                                                                                                                                                                                                                                                                                                                                                                                            | `contact_center_base/data/resolution_reason_data.xml`, `views/resolution_views.xml:29-35`                                                                                       |
| Baixa      | Assimetria no modelo nativo: um usuário interno que seja agente da Central perde a edição de respostas prontas nativas criadas por outros (não vinculadas), enquanto usuários sem papel na Central a mantêm.                                                                                                                                                                                                                                                                                                                                                                                   | `contact_center_base/models/quick_reply.py:37-45`                                                                                                                               |
| Baixa      | Com latch de identidade, todo evento de chamada gera `TransientAdapterError` e fica em retry até o teto da fila; comportamento coerente com o bloqueio de saída, mas precisa de nota operacional. O probe de saúde faz uma requisição extra `/user/lid` por execução e engole 429 sem registrar cooldown.                                                                                                                                                                                                                                                                                      | `contact_center_wuzapi/services/adapter.py:786-813, 4225-4245`                                                                                                                  |
| Baixa      | Complexidade ciclomática acima do limite do repositório em `setFilter` e `startConversation`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `contact_center_ui/static/src/js/contact_center_store.esm.js:1983, 2080`                                                                                                        |

### Interface (Owl)

| Prioridade | Achado                                                                                                                                                                                                                                                                                                                                                                                                         | Localização                                                                                                        |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Média      | **Resultado de "Abrir conversa" descartado se o popover fechar durante o RPC.** Clique fora e Escape fecham o painel sem checar `startPending`; o store devolve `false` quando `!isCurrent()` e não insere, seleciona nem notifica a conversa criada/reutilizada. `ui.startPending` só é limpo no próximo toggle. Idempotência no servidor evita duplicata, mas o agente não recebe retorno e tende a repetir. | `js/conversation_list.esm.js:442-459, 531-540, 853-870`; `js/contact_center_store.esm.js:2105-2107, 518`           |
| Baixa      | `markSeen` reemite `mark_seen` idêntico a cada evento de scroll enquanto `unread_count` está defasado (rajada limitada pelo dedup em voo, não é laço infinito).                                                                                                                                                                                                                                                | `js/contact_center_store.esm.js:3320-3351`; `js/conversation_timeline.esm.js:695`                                  |
| Baixa      | O reconhecimento de leitura verifica viewport, foco e visibilidade da aba, mas não oclusão: mensagem que chega com o painel de contato deslizado (z-index 20, 88vw) ou um modal aberto é marcada como lida. O caso da lista mobile está correto.                                                                                                                                                               | `js/conversation_timeline.esm.js:734-768`; `scss/contact_center_app.scss:6073-6090`                                |
| Baixa      | `ConversationTags` atribui `payload.items` sem validar o envelope; resposta malformada quebra o getter `visibleTags` na renderização em vez de mostrar `local.error`.                                                                                                                                                                                                                                          | `js/conversation_tags.esm.js:61-65, 121-123, 167-171`                                                              |
| Baixa      | O diálogo de resolução lê `submittedValues`, propriedade não reativa; após um confirmar com falha (revisão obsoleta) motivo, justificativa e gestão ficam desabilitados até cancelar e reabrir. Intencional pelo comentário, mas frágil.                                                                                                                                                                       | `xml/conversation_resolution.xml:21, 40, 53`; `js/conversation_resolution.esm.js:23, 112-118`                      |
| Baixa      | Popovers de filtro, início e marcadores não prendem o foco; o Escape dos marcadores está ligado só à raiz e deixa de funcionar quando o foco sai por Tab. Aceitável para popover não modal.                                                                                                                                                                                                                    | `xml/conversation_list.xml`; `xml/conversation_tags.xml:37-43`; `js/conversation_tags.esm.js:92-98`                |
| Baixa      | Strings novas em pt-BR fixas apesar de `_t` importado; `Intl.Collator("pt-BR")` fixo em vez do idioma do usuário.                                                                                                                                                                                                                                                                                              | `js/contact_center_store.esm.js:2073-2099, 2159`; `js/conversation_list.esm.js:29, 423, 476-484`                   |
| Baixa      | `formatTime()` sem referência; o contador de filtros ativos conta o padrão `states=["open"]` (badge "1" ao abrir) e "Limpar filtros" resulta em `states: []`, mais amplo que o padrão.                                                                                                                                                                                                                         | `js/message_content.esm.js:364`; `js/conversation_list.esm.js:393-406`; `js/contact_center_store.esm.js:2035-2052` |

## Pontos verificados sem defeito

- `bootstrap()` continua carregando para administradores e usuários por equipe: o
  domínio de escopo e `_contact_center_effective_users` cobrem o mesmo conjunto, e a
  desativação de equipe referenciada é bloqueada por
  `_contact_center_check_not_referenced`.
- As record rules de vínculo de resposta rápida são cumulativas por grupo; a regra
  pessoal não reduz a visibilidade de escopos empresa, equipe, caixa e conversa.
- `/user/lid` com HTTP 404 retorna `None` antes de `_group_raise_read_error`; 401, 429 e
  5xx continuam classificados como pausa, limite e transitório.
- `_same_registered_phone` aceita apenas o par exato ou o nono dígito móvel brasileiro
  comprovado pelo provedor, nas duas direções.
- O flag `created` usa os mesmos critérios de busca de `_resolve_channel`.
- O teste de rollback de `start_conversation` compara a contagem de onze modelos na
  mesma transação, o que comprova o savepoint; as matrizes de autorização asseguram que
  o adapter não é chamado.
- O ensaio de upgrade nativo passou a validar versão por módulo, coerente com a
  divergência de versões entre addons.
- Interface: todo caminho assíncrono carrega token de geração ou `isCurrent` e revalida
  antes de mutar estado; duplo envio bloqueado em início e resolução; timers, rAF e
  listeners limpos na destruição; `states`, `responsible_id` e `tag_ids` seguem para a
  página inicial, páginas por cursor e refresh realtime; nenhum `t-raw`, `t-out`,
  `innerHTML` ou `markup`; links de download restritos a IDs de mídia local; a correção
  `#{"min(...)"}` para libsass está completa nas onze ocorrências.

## Recomendações, em ordem

1. Decidir o contrato da resposta pessoal: sobrescrever `_init_messaging` para filtrar
   `mail.shortcode` pela regra, ou documentar que o escopo pessoal governa apenas
   gestão, não sigilo. Em qualquer caso, adicionar teste de leitura por usuário fora da
   Central e por usuário de outra empresa.
2. Executar QUnit nos dois modos de assets no LAB e registrar em `reviews/`, como nas
   entregas anteriores, antes de qualquer promoção. Corrigir antes o fechamento do
   popover de início durante o RPC: bloquear Cancelar, clique fora e Escape enquanto
   `startPending`, ou aplicar o resultado mesmo após o fechamento.
3. Registrar a decisão de liberar vínculos de empresa a agentes e restaurar testes de
   negação por escopo de conversa.
4. Fechar as lacunas de cobertura listadas, começando por multi-empresa, OR de tags com
   paginação e início concorrente.
5. Fixar a convenção de idioma do código-fonte e corrigir o msgid divergente; se a
   escolha for inglês com `.po`, mover as 57 strings novas.
6. Atualizar README (versões, capacidades, `phonenumbers`), `operations/`, bump do CRM,
   versionar o spec de início de conversa e alinhar `peer_ref` ao par validado.
7. Instalar `phonenumbers` no venv do LAB e de produção antes do próximo `-u`.

## Limites desta auditoria

Nenhum teste foi executado nesta máquina ou no laboratório; a evidência de execução é o
CI publicado e o lint reproduzido localmente. A revisão da camada JS e da cobertura de
testes foi feita por leitura de código. Produção não foi acessada.
