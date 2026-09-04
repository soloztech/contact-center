# Validação da Fase 5.2 — metadata técnica de grupos

- Data: 2026-08-24
- Ambiente: SERVIDOR05, banco neutralizado `odoo16`
- Produção: não acessada nem alterada
- Resultado: implantada e validada no laboratório; próxima fase funcional é a 5.3

## Escopo entregue

- Profile provider-neutral 1:1 por binding de grupo, com nome seguro, avatar privado,
  estado de sincronização e agregados do roster.
- Roster técnico com aliases PN/LID, sem promoção automática para `mail.guest`,
  identity, `res.partner` ou `mail.channel.member`.
- `GroupInfo`, `JoinedGroup` e `Picture` usados somente como hints; o snapshot
  autoritativo vem de `GET /group/info?groupJID=...` por OCA `queue_job`.
- Snapshot completo com reconciliação de remoções e snapshot parcial sem remoção,
  mantendo estado `stale`, retry em 15 minutos e TTL completo de seis horas.
- Avatar obtido por `POST /user/avatar`, validado até 2 MiB, persistido como attachment
  privado e servido somente por rota Odoo autenticada.
- UI inbound/read-only com nome, avatar e metadata agregada, sem expor roster, aliases,
  JID bruto, URL remota ou credenciais.
- Compatibilidade com o formato legado de grupo `numeric-hyphen-numeric@g.us`, além do
  formato numérico atual.

## Revisões implantadas

Commits principais desta entrega:

- `6513ff2`, `42c85f8`, `60f7425`, `e61df44`, `6bfd14f`, `2703afc`;
- `90a5cbe`, `0f30d1a`, `67337b8`, `5956309`, `e4f8386`.

O handoff UI foi integrado no intervalo principal `5fd59ff..79c8211`. O primeiro QUnit
detectou uma falha no conjunto de 335 asserções: o sanitizer aceitava o avatar `ready`,
mas omitia `state` no item interno. O commit `8b83773` corrigiu a projeção e `a1b61f6`
elevou a versão para forçar rebuild inequívoco dos assets. Essa execução intermediária
não é contabilizada como validação final.

O smoke real posterior encontrou um segundo defeito que o QUnit não detectava: o viewer
de item único preservava o grid de três colunas e comprimia a imagem a aproximadamente
13 px. O template `message_content.xml` e o SCSS foram corrigidos, e a UI foi elevada a
`16.0.1.8.3`. O redeploy e a revalidação visual confirmaram a correção nas dimensões
desktop e mobile registradas abaixo.

O incremento final integrou agrupamento das conversas por caixa, recortes globais de
responsabilidade e carregamento diferido de imagens com concorrência limitada. Os
commits principais desse fechamento são `19c7f3e`, `ff8669e`, `0ea583e`, `9c855da` e
`469d8ea`.

Versões validadas:

- OCA `queue_job` `16.0.3.0.2`;
- `contact_center_base` `16.0.1.11.0`;
- `contact_center_wuzapi` `16.0.1.8.0`;
- `contact_center_ui` `16.0.1.9.0`.

Tree hash do código runtime implantado:
`39721ec9cdbb368999f0587b5a20da53f2a7f988f2f70040fb0b7b0e3cd23481`.

## Testes e evidências automatizadas

- Odoo base: **147/147**, sem falhas ou erros.
- Odoo integrado: **222/222**, sem falhas ou erros.
- QUnit UI em bundle minificado e `debug=assets`: **47/47**, 416/416 asserções.
- Viewer base em bundle minificado e `debug=assets`: **4/4**, 15/15 asserções.
- Handoff UI integrado com agrupamento de mensagens, viewer interno de imagem/vídeo,
  player de áudio e ações touch/acessíveis.
- Testes Odoo do checkpoint de metadata:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/test/20260824T140331333948Z`.
- Deploy final:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/deploy-ui-1-9-0-fix2/`.
- Upgrade final da base/UI:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/upgrade-ui-1-9-0/`.
- Validate final:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/validate-ui-1-9-0/`.
- Testes Odoo finais:
  `scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/test-ui-1-9-0-fix1/`.
- Artefatos do navegador: `output/playwright/phase5-2-group-metadata/`.

## Prova funcional no laboratório

- As cinco instâncias WuzAPI ficaram inscritas em `GroupInfo`, `JoinedGroup` e
  `Picture`; a frota permaneceu 5/5 conectada.
- O backfill convergiu em **51/51** profiles `ready`, com 49 avatares, 19.365
  participantes técnicos e 38.697 aliases.
- Durante a convergência, os totais permaneceram invariáveis em 209 `mail.guest`, 3.555
  `res.partner` e 574 `mail.channel.member`, comprovando que não houve criação em massa
  de pessoas ou memberships pelo roster.
- Um snapshot read-only posterior ao `16.0.1.8.3`, com novos dados ainda chegando,
  mostrou **53/53** profiles `ready`, 51 avatares, 19.860 participantes técnicos, 39.686
  aliases e 5/5 conexões ativas `connected`. Esse retrato vivo não substitui a prova
  controlada de invariância do backfill.
- A ação pública administrativa `action_retry_metadata_sync`, protegida por ACL, record
  rule e lock, foi aplicada às 15 sincronizações inicialmente falhas; todas convergiram.
- O browser autenticado carregou 117 conversas, mostrou grupo somente leitura com nome,
  avatar e agregados e terminou com zero erro ou warning no console.
- Em uma conversa real com oito mensagens, o agrupamento produziu um início, sete
  continuações e a última mensagem também fechou o run; o composer permaneceu ausente.
- Os 18 players de áudio reais chegaram a `readyState=4`, sem erro; o controle de
  velocidade alternou corretamente de 1x para 1,5x.
- A imagem usada no smoke tinha dimensão natural 1200x1600. Ela revelou o defeito de
  grid unitário descrito acima. Após a correção da UI `16.0.1.8.3`, o desktop mediu
  viewer/stage de 1138 px e imagem 464x618, com `navCount=0`; no viewport mobile
  390x844, o stage mediu 378 px e a imagem 366x488, sem overflow.
- `Esc` restaurou o foco ao elemento de origem. O console da UI terminou com zero erro e
  zero warning.
- A visão agrupada organizou as caixas sem perder a alternativa plana. Os filtros
  globais retornaram duas conversas em `Minhas` e 131 em `Não atribuídas`, selecionando
  a primeira linha válida após cada reset.
- Antes de selecionar a conversa de mídia, os resource timings foram zerados. A fila
  manteve imagens fora da viewport sem `src`, iniciou três recursos de mídia com pico
  máximo de duas requisições simultâneas e, após scroll incremental, totalizou cinco sem
  superar o mesmo teto. O viewer abriu a mídia imediata e o console permaneceu sem erro.
- Uma verificação individual recuperou o único estado transitório de atenção; o painel
  estabilizou em **5/5 conectadas**.
- A varredura read-only dos logs desde o restart do container às 16:33:33Z encontrou
  zero `PoolError`, zero resposta HTTP 500 e zero request exception. Sete eventos de
  receipt sofreram `SerializationFailure` concorrente, mas o `queue_job` os repetiu e
  todos convergiram para `done` em duas ou três tentativas, sem evento `dead`.
  Permaneciam 64 jobs de inbox em `pending` com ETA programado; isso é backlog de retry,
  não erro HTTP, e deve continuar sendo observado antes do piloto.
- Foram renderizados 32 avatares. A rota local autenticada respondeu `200 image/jpeg`,
  `X-Content-Type-Options: nosniff` e cache privado; acesso anônimo frio foi
  redirecionado para autenticação.
- O upgrade final alterou somente `contact_center_base` e `contact_center_ui` (IDs
  técnicos 3817 e 3818); os dados de negócio permaneceram invariáveis e nenhum backup de
  banco foi executado no laboratório. O deploy reteve somente a cópia automática e
  recuperável da árvore de código anterior.

## Limites preservados

- A Fase 5.2 não habilita composer, reply, reaction, edit, delete, receipts ou gestão de
  participantes em grupos.
- Os resultados homologam o laboratório descartável, não a produção nem o aceite geral
  do piloto.
- A Fase 5.3 deverá planejar e implementar as próximas capacidades de grupo com gates
  explícitos por operação e fixtures reais.
