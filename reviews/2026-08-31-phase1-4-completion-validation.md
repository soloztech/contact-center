# Fechamento dos itens 1–4 — validação consolidada

Data: 2026-08-31

Ambiente: SERVIDOR05, banco neutralizado de laboratório

Produção acessada ou alterada: não

## Veredito

Os quatro itens estão concluídos e homologados no laboratório. O callback Meta
compartilhado está ativo, a autoridade está `shared` e o ingresso legado está pausado.

| Item                                         | Resultado em 2026-08-31                                 |
| -------------------------------------------- | ------------------------------------------------------- |
| 1 — PDF inline e limite de equipe da caixa   | Concluído, implantado e validado                        |
| 2 — consumidor do webhook Meta compartilhado | Concluído, com cutover e verificação independente       |
| 3 — regressão integrada, QUnit e browser     | Concluído sem falhas no escopo canônico                 |
| 4 — plano e evidência consolidada            | Concluído por este registro e pela atualização do plano |

## Candidato implantado

- `contact_center_base 16.0.1.29.4`;
- `contact_center_wuzapi 16.0.1.26.4`;
- `contact_center_meta 16.0.1.12.1`;
- `contact_center_ui 16.0.1.18.8`;
- OCA `queue_job 16.0.3.0.2`;
- árvore Contact Center
  `e3c150627485992500325646a241839a4ea359e7a84b27b113e364afcffac8c1`, com 257 arquivos
  verificados.

O release atômico terminou com upgrade `0`, containers em execução, endpoint interno e
público HTTP 200 e rota exata do laboratório restaurada. Evidência:
`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260831T212908283635Z`.

## Item 1 — comportamento funcional

### PDF inline

- PDF pronto, com MIME exato e URL autenticada local, abre no PDF.js empacotado pelo
  Odoo;
- demais documentos continuam em download e URLs externas, divergentes ou inseguras
  falham fechadas;
- a prova autenticada abriu a mídia real `3490` pela rota local, renderizou uma página e
  não produziu erro de console.

### Equipe fixa e empresa do contato

- o card exibe “Equipe da caixa” somente como leitura;
- o único seletor operacional restante é o responsável e continua limitado aos agentes
  da equipe da caixa;
- a busca de empresa vinculável foi aberta e exercitada com resultado vazio, sem criar
  nem alterar cadastro.

Capturas, snapshots e console:
`scans/raw/20260831-odoo16-contact-center-phase1-4-completion/browser`.

## Item 2 — ingresso Meta compartilhado

O `integration-core` está instalado no laboratório com:

- `meta_api_base 16.0.1.1.0`;
- `meta_webhook_base 16.0.1.0.1`;
- árvore `19333942d3430f43c66000e7957feb4a286d263dd9549a66240be44d87db7242`;
- 36 testes isolados do API base e 27 do webhook base, sem falha ou erro.

Evidência canônica:
`scans/raw/20260831-odoo16-integration-core-shared-meta/release/20260831T230007445621Z`.

A separação é intencional: `meta_webhook_base` conserva a prova técnica da entrega e faz
fan-out; `contact_center_meta` reivindica apenas o consumidor `contact_center.meta` e
projeta o item na Inbox/EventDTO canônica. Não existe um segundo guest, canal ou
`mail.message`.

Os três segredos foram preparados no host e o runtime final monta o diretório somente no
container Odoo, em modo read-only; o dbmanager não recebe segredos Meta. Evidência do
staging:
`scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T220957043882Z`.

O `prepare` provou challenge e POST assinado. O `switch` aguardou o job exato de
reconcile, comprovou as 13 subscriptions e os dois ativos, vinculou as conexões 6 e 7,
validou health fresco com autoridade `shared`, estabilizou a janela de entrega e pausou
o ingresso legado por último. Uma execução read-only separada repetiu todos os checks.
Evidências:

- prepare:
  `scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T230343936546Z`;
- switch:
  `scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T230400287480Z`;
- verify:
  `scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T230933867551Z`.

## Item 3 — testes e aceitação

- backend base: **368/368**, zero falha e zero erro;
- backend integrado: **754/754**, zero falha e zero erro;
- bancos temporários removidos ao final;
- QUnit UI: **92/92** testes e **817/817** asserções, nos bundles minificado e
  `debug=assets`;
- QUnit do viewer base: **4/4** testes e **15/15** asserções, nos dois modos de assets;
- aplicação autenticada carregou a interface própria, realtime e as sete conexões sem
  erro de fundação ou de console; a observação final mostrou **7/7** conectadas.

Evidências:

- backend:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260831T213013020027Z`;
- QUnit e browser: `scans/raw/20260831-odoo16-contact-center-phase1-4-completion`;
- estado instalado depois do cutover e da verificação de estabilidade:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260831T231159897713Z`.

O preflight da árvore final também passou Black nos 137 arquivos Python, Flake8, isort,
Prettier em XML/JavaScript/SCSS, ESLint, compilação Python e `git diff --check`.

## Runtime do laboratório

O hardening final recriou apenas o runtime neutralizado do SERVIDOR05, com dois workers,
um cron thread, `db_maxconn=16`, HTTP em `8169`, realtime em `8172` e segredo Meta em
`/run/secrets/odoo-meta-api` somente no Odoo. A validação comprovou container saudável,
HTTP 200, websocket 101, porta realtime alcançável pelo SERVIDOR04 e rota pública do
laboratório restaurada. Evidência:
`scans/raw/20260831-odoo16-lab-runtime-hardening/20260831T221840452367Z`.

## Falhas recuperadas e evidência descartada

- Uma primeira tentativa de aplicar o mount parou em modo fail-closed porque o guard
  classificou o publisher já conhecido da porta `8172` como externo. Não houve mudança
  de runtime, rota ou produção. O guard foi restringido ao mapeamento exato revisado e a
  reaplicação posterior foi validada. Evidência da parada segura:
  `scans/raw/20260831-odoo16-lab-runtime-hardening/20260831T221555171844Z`.
- Uma execução inicial de QUnit usou o parâmetro incorreto `mod` e iniciou a suíte geral
  do Odoo. Ela foi descartada como evidência do Contact Center. As quatro execuções
  canônicas foram repetidas em sessão limpa com `filter=contact_center_ui` ou
  `filter=contact_center_base` e são somente as registradas acima.
- Um processo escritor concorrente foi interrompido antes da consolidação. O release e
  todas as provas finais ficaram ancorados na árvore imutável `e3c150627485...`; nenhum
  resultado anterior com drift foi promovido a evidência canônica.
- O primeiro `switch` parou antes da mutação porque o usuário system não pertencia ao
  grupo separado `Job Queue Manager`. O contrato passou a exigir os dois grupos e a
  permissão foi aplicada somente ao operador do laboratório.
- A tentativa seguinte encontrou uma ação que retornava um recordset Odoo não
  serializável por XML-RPC. A compensação automática comprovou `legacy/in_sync`, removeu
  os bindings compartilhados e deixou `recovery_required=false`. O retorno foi trocado
  pelo UUID textual, coberto por regressão e promovido em `meta_webhook_base 16.0.1.0.1`
  antes do cutover final. Evidência:
  `scans/raw/20260831-odoo16-contact-center-meta-shared-cutover/20260831T225354623115Z`.

Nenhuma dessas ocorrências tocou produção. Backup do banco foi dispensado conforme a
autorização explícita para o banco descartável e neutralizado do SERVIDOR05.

## Pendências externas ao fechamento técnico

- observar tráfego real externo no ledger compartilhado durante o piloto; o cutover, a
  verificação técnica e a janela vazia de transição já foram concluídos;
- antes do piloto Meta externo: credencial gerenciada por System User, Live/App Review,
  Advanced Access/Business Verification e prova com remetente sem App role;
- esta homologação de laboratório não é autorização de implantação em produção.
