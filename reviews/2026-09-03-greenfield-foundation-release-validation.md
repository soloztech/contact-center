# Fundação greenfield — validação de release

- Data: 2026-09-03
- Ambiente: SERVIDOR05, `odoo16-teste.soloz.com.br`
- Produção: **não acessada e não alterada**
- Escopo: `contact_center_base`, `contact_center_wuzapi`, `contact_center_meta`,
  `contact_center_crm` e `contact_center_ui`
- Estado deste documento: **R5 aplicada e validação integral concluída**

## Veredito

A fundação do Contact Center está íntegra no laboratório e pronta para continuar o
desenvolvimento funcional. O release R5 comprovou o conjunto completo de addons,
dependências, migrations, testes isolados e integrados, upgrade offline, restauração da
rota, disponibilidade pública, QUnit minificado/debug e smoke autenticado.

Durante esse smoke foi encontrado um defeito de consistência do catálogo de tags: uma
tag criada depois do bootstrap da sessão chegava corretamente pelo bus e atualizava a
conversa selecionada, mas não existia ainda no catálogo local usado para renderizar os
botões. A correção R3 reconcilia incrementalmente esse catálogo, sem refazer o bootstrap
e sem substituir arrays quando nada mudou. Ela possui três regressões QUnit e é o único
delta funcional entre R2 e R3.

O release R3 foi aplicado e a prova realtime específica passou. Sua primeira execução
QUnit revelou sete casos e 17 asserções incompatíveis com contratos fail-closed já
vigentes. A contrachecagem confirmou fixtures/expectativas obsoletas, não defeito do
runtime. A R4 publicou somente a correção desses testes e fechou o gate: UI **144/144**,
**1248/1248** em minificado e `debug=assets`; viewer base **4/4**, **15/15** nos dois
modos; zero falhas, skips, TODOs, erros ou warnings de console.

A R5 mantém todos esses gates e publica uma única evolução arquitetural no CRM: o hook
`_contact_center_before_tombstone(reason)`. Ele é chamado sob o lock/fence canônico e
após a segunda validação de autorização, enquanto a FK viva do lead ainda existe. Isso
permite que bridges opcionais revoguem projeções derivadas na mesma transação do
tombstone, sem acoplar o core do Contact Center ao Marketing Center ou a outro
consumidor.

## Alicerce consolidado

### Core e CRM

- Conta lógica, equipe, usuário, pipeline, canal e caso compartilham ordem de locks,
  revisions e revalidação contra write skew.
- Pipeline e caso padrão não podem ser arquivados; arquivamentos permitidos passam por
  ações explícitas e validam referências e atividades abertas.
- Nota interna, resposta rápida, follow-up e agendamento mantêm ledgers/bindings
  próprios sem criar outra conversa ou mensagem canônica.
- O agendamento conserva o `outbound_request_id` admitido; a execução posterior revalida
  autorização e usa o envio canônico.
- A ponte CRM mantém autoridade técnica para o primeiro binding e proveniência dos
  grants. Somente permissões criadas e gerenciadas pelo bridge podem ser revogadas.
- Escrita UTM automática e criação heurística de bindings CRM continuam bloqueadas por
  decisão arquitetural.

### WuzAPI e Meta

- JSON não padrão (`NaN`/`Infinity`) e payload excessivamente profundo são rejeitados
  antes de ledger/job; `Retry-After` fornecido pelo provider é limitado a 3.600 s.
- O adapter Meta preserva falhas transitórias de avatar e seu retry, sem convertê-las em
  indisponibilidade permanente.
- `contact_center_meta` permanece um adapter fino sobre `meta_api_base` e
  `meta_webhook_base`; não recria app, credencial, controller, delivery ledger, health
  ou ACL paralelos.
- Provider e UI continuam desacoplados pelo contrato DTO/capabilities do core.

### Interface

- O composer libera a referência local ao `File` depois que o servidor assume o upload.
  Previews grandes também têm o Blob URL revogado, reduzindo retenção de RAM; previews
  pequenos permanecem disponíveis.
- O journal local usa fingerprint apenas para idempotência e recuperação de intenção;
  não o apresenta como mecanismo criptográfico.
- Atualizações usam bus como fonte primária e reconciliação periódica como reparo.
  Timeline e lista preservam cursores e histórico carregado.
- A R3 acrescenta ao catálogo local tags descobertas na conversa atual, deduplica por
  ID, preserva a ordem existente e evita bootstrap completo.

## Release atômico R2 — validado

Evidência canônica:
`scans/raw/20260903-contact-center-greenfield-foundation-r2-release-final10/summary.json`.

| Item                                  |                                                          Resultado |
| ------------------------------------- | -----------------------------------------------------------------: |
| Árvore                                | `7a48cea98e7c07c0f3ef6e8cef7b2eac8b267b6fc038654e9a45add2eadc8660` |
| Arquivos verificados no servidor      |                                                                298 |
| Base isolado                          |                                     **493/493**, 0 falhas, 0 erros |
| WuzAPI isolado                        |                                     **210/210**, 0 falhas, 0 erros |
| Integração                            |                                     **878/878**, 0 falhas, 0 erros |
| Upgrade offline                       |                                                         status `0` |
| Addons instalados/pendentes           |                                                                5/0 |
| `queue_job`                           |                                            `16.0.3.0.2`, instalado |
| `meta_api_base` / `meta_webhook_base` |                            `16.0.1.1.1` / `16.0.1.4.0`, instalados |
| HTTP interno e público                |                                                              `200` |
| Rota Traefik                          |                              restaurada byte a byte, mesmo SHA-256 |
| Produção tocada                       |                                                            **não** |

Versões publicadas na R2:

| Addon                   |        Versão |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.0` |
| `contact_center_wuzapi` | `16.0.1.29.1` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.0` |
| `contact_center_ui`     | `16.0.1.26.3` |

O QUnit observado após o release R2 completou os **141** casos sem falha. O smoke
autenticado mostrou **651 conversas** e confirmou lista, seleção, timeline, texto,
imagem, áudio, encaminhamento, reação, composer, respostas rápidas, nota interna,
follow-up e agendamento, sem criar envio externo. Console: zero erros e zero avisos.

No viewport `390 × 844`, lista, conversa, timeline, composer e ferramentas de mídia
permaneceram utilizáveis e sem overflow horizontal. Evidências visuais:

- `output/playwright/contact-center-final10-desktop.png`
- `output/playwright/contact-center-final10-mobile.png`
- `output/playwright/contact-center-final10-mobile-conversation.png`
- `output/playwright/contact-center-final10-mobile-timeline.png`

O teste realtime controlado comprovou alteração de tag sem `F5` em aproximadamente três
segundos. A tag temporária foi removida ao final e não deixou vínculo nem registro. Esse
teste revelou o caso de catálogo tardio corrigido pela R3.

## Release atômico R3 — backend/deploy validados

Alvo da R3:

| Addon                   |        Versão |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.0` |
| `contact_center_wuzapi` | `16.0.1.29.1` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.0` |
| `contact_center_ui`     | `16.0.1.26.4` |

O dry-run e o release aplicado preservaram a mesma árvore
`ec47e213ab78830b1369d0bf8877b6662cb9c0a165ddd8b627b00fa9b22338b5` e os mesmos 298
arquivos. Evidência canônica do apply:
`scans/raw/20260903-odoo16-contact-center-greenfield-foundation-atomic-release-r3/release/20260903T205800301468Z/summary.json`.

| Item                        |                             Resultado |
| --------------------------- | ------------------------------------: |
| Base isolado                |        **493/493**, 0 falhas, 0 erros |
| WuzAPI isolado              |        **210/210**, 0 falhas, 0 erros |
| Integração                  |        **878/878**, 0 falhas, 0 erros |
| Upgrade offline             |                            status `0` |
| Addons instalados/pendentes |                                   5/0 |
| HTTP interno e público      |                                 `200` |
| Rota Traefik                | restaurada byte a byte, mesmo SHA-256 |
| Produção tocada             |                               **não** |

O smoke autenticado R3 abriu **651 conversas**, timeline, mídia e ferramentas de
produtividade, com zero erro e zero warning no console da aplicação. A prova exata da
regressão criou `SMOKE REALTIME R3 20260903` **depois** do bootstrap, anexou-a ao canal
672 e observou o botão surgir automaticamente com `aria-pressed=true`, sem `F5`. O
detach foi refletido como `false`; ao final, busca da tag retornou zero e o canal ficou
sem tags. Screenshot: `output/playwright/contact-center-r3-realtime-new-tag.png`.

O viewer QUnit do base passou **4/4** casos e **15/15** asserções. Já a primeira
execução do QUnit da UI R3 encontrou sete casos e 17 asserções falhando. A análise
individual confirmou:

- dois fixtures de mídia ainda usavam o array legado de capabilities em vez do mapa
  estruturado que o runtime rejeita por desenho;
- quatro fixtures de timeline omitiam `payload.channel_id`, obrigatório para descartar
  eventos de outra conversa;
- uma expectativa de mutação exigia um refresh RPC redundante, embora a UiDTO já
  retornada seja mesclada diretamente.

Somente os testes foram ajustados; o store não mudou. `node --check`, Prettier, ESLint e
`git diff --check` passaram. A execução integral posterior é registrada na R4 abaixo.

## Release atômico R4 — validação integral concluída

Evidência canônica:
`scans/raw/20260903-odoo16-contact-center-greenfield-foundation-atomic-release-r4/release/20260903T211640162203Z/summary.json`.

| Item                             |                                                          Resultado |
| -------------------------------- | -----------------------------------------------------------------: |
| Árvore                           | `28cc595f52bf9daa4091ded013302f41e200c60eb9c89645299c4ae0dfeb5d79` |
| Arquivos verificados no servidor |                                                                298 |
| Base isolado                     |                                     **493/493**, 0 falhas, 0 erros |
| WuzAPI isolado                   |                                     **210/210**, 0 falhas, 0 erros |
| Integração                       |                                     **878/878**, 0 falhas, 0 erros |
| Upgrade offline                  |                                                         status `0` |
| Addons instalados/pendentes      |                                                                5/0 |
| HTTP interno e público           |                                                              `200` |
| Rota Traefik                     |                              restaurada byte a byte, mesmo SHA-256 |
| Produção tocada                  |                                                            **não** |

Versões finais:

| Addon                   |        Versão |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.0` |
| `contact_center_wuzapi` | `16.0.1.29.1` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.0` |
| `contact_center_ui`     | `16.0.1.26.4` |

Evidência de navegador: `output/playwright/contact-center-r4-acceptance-summary.json`.

| QUnit             |                 Minificado |             `debug=assets` |
| ----------------- | -------------------------: | -------------------------: |
| Contact Center UI | **144/144**, **1248/1248** | **144/144**, **1248/1248** |
| Viewer base       |         **4/4**, **15/15** |         **4/4**, **15/15** |

Todas as quatro execuções terminaram com zero falhas, zero skips e zero TODOs. O smoke
autenticado abriu `Odoo - Caixa de entrada`, encontrou o cabeçalho **Conversas** e o
indicador **Tempo real ativo**, com zero erro e zero warning no console.

Screenshots:

- `output/playwright/contact-center-r4-qunit-ui-minified.png`
- `output/playwright/contact-center-r4-qunit-ui-debug-assets.png`
- `output/playwright/contact-center-r4-app-smoke.png`

## Release atômico R5 — validação integral concluída

Evidência canônica:
`scans/raw/20260903-odoo16-contact-center-greenfield-foundation-atomic-release-r5/release/20260903T231220190392Z/summary.json`.

| Item                             |                                                          Resultado |
| -------------------------------- | -----------------------------------------------------------------: |
| Estado                           |                                            `applied_and_validated` |
| Árvore                           | `8396abc8f9bc497db315dfb63dbc0a0330fb8ad2b0995f40ad81a2d935296a8a` |
| Arquivos verificados no servidor |                                                                298 |
| Base isolado                     |                                     **493/493**, 0 falhas, 0 erros |
| WuzAPI isolado                   |                                     **210/210**, 0 falhas, 0 erros |
| Integração                       |                                     **878/878**, 0 falhas, 0 erros |
| Upgrade offline                  |                                                         status `0` |
| HTTP público                     |                                                              `200` |
| Rota Traefik                     |                                                         restaurada |
| Produção tocada                  |                                                            **não** |

Versões publicadas na R5:

| Addon                   |        Versão |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.0` |
| `contact_center_wuzapi` | `16.0.1.29.1` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.1` |
| `contact_center_ui`     | `16.0.1.26.4` |

O QUnit foi repetido nos bundles minificado e `debug=assets`: UI **144/144** casos e
**1248/1248** asserções; viewer base **4/4** casos e **15/15** asserções. A R5 preserva
todo o comportamento validado na R4 e acrescenta o hook
`_contact_center_before_tombstone(reason)` em `contact_center_crm`.

O hook roda somente depois do lock/fence e da revalidação de autorização, antes que o
tombstone remova a FK do lead. O bridge Marketing pode, portanto, revogar suas projeções
enquanto a identidade CRM ainda está disponível, na mesma transação do tombstone. Se a
revogação falhar, toda a operação é revertida; se passar, tombstone e revogação são
confirmados juntos. O Contact Center continua independente: o método base é um no-op e
não conhece o Marketing Center.

## Disposição

- A separação em cinco addons permanece justificada por fronteiras reais: domínio,
  adapter WuzAPI, adapter Meta, bridge CRM e UI provider-neutral.
- Não foi criada compatibilidade de runtime ou camada paralela para preservar desenho
  aposentado. Migrations remanescentes existem apenas para atualizar o banco de
  laboratório já utilizado.
- Nenhum split foi feito apenas por tamanho de arquivo. Extrações ocorreram quando havia
  fronteira de responsabilidade, concorrência ou complexidade mensurável.
- O risco residual relevante continua sendo o pico de memória do outbound WuzAPI para
  mídia grande, imposto pelo protocolo base64 pinado. Deve ser medido antes de carga
  produtiva e pode ser limitado operacionalmente.

## Próximo gate funcional

Com a R5 fechada, a base técnica deixa de ser o limitante. As próximas entregas devem
voltar ao roadmap de produto: multiatendimento/presença e SLA; projeção de casos e CRM
na UI; depois visão 360 e analytics. Elas devem reutilizar os contratos atuais, sem
escrita UTM no core e sem atalhos diretos ao provider.
