# Pente-fino greenfield — Contact Center Base e CRM

- Data: 2026-09-03
- Escopo: `contact_center_base` e `contact_center_crm`
- Complementa: `2026-09-03-contact-adapters-ui-greenfield-review.md`
- Estado: baseline greenfield implantado e validado no SERVIDOR05

## Veredito

O núcleo possui invariantes fortes de autorização, idempotência, concorrência e
multiempresa. A revisão não encontrou motivo para redesenhar o agregado nem fundir
addons. Encontrou, porém, cinco lacunas concretas que foram corrigidas antes do primeiro
baseline produtivo:

1. uma alteração indireta dos grupos de acesso podia escapar da proteção que olhava
   apenas o grupo escrito diretamente;
2. DTOs aceitavam `NaN` e infinito, valores válidos para o parser Python, mas não para
   JSON interoperável;
3. o onboarding do base conhecia indiretamente o XMLID da interface;
4. a ponte CRM precisava fechar a mesma ordem de locks em roster, inventário, bindings,
   estágio, lead e caso;
5. snapshots de uma relação CRM aposentada misturavam a identidade original do lead com
   a última identidade observada.

As correções preservam o domínio: conversa, identidade, guest, contato, caso e lead
continuam entidades separadas. O CRM é autoridade apenas onde existe binding explícito;
caixas e equipes nativas continuam válidas fora dele.

## Correções aplicadas

### Base

- A proteção dos grupos Contact Center considera o grupo escrito e todos os seus
  `trans_implied_ids`. Alterar membros pelo grupo Administrador/System não permite
  contornar o fluxo administrativo do módulo.
- `MessageDTO`, `MediaDTO`, `AttributionDTO` e `AdapterResult` rejeitam números não
  finitos em qualquer profundidade; serialização usa `allow_nan=False`.
- O onboarding do base retorna uma ação provider-neutral. A troca para o client action
  moderno passou a ser extensão de `contact_center_ui`, removendo a dependência reversa
  Base → UI.
- O helper runtime antes chamado `_contact_center_rebind_legacy_guest_memberships` foi
  renomeado para `_contact_center_rebind_retired_guest_memberships`. Ele repara
  memberships de guests efetivamente aposentados por um merge e não é compatibilidade
  legada.
- Os helpers usados exclusivamente pelas migrations pré-produtivas de identidade foram
  removidos junto com essa cadeia histórica. O baseline não conserva código morto para
  bancos que nunca serão uma origem produtiva suportada.
- Os blocos `<record>` que regravavam grupos dos menus técnicos durante upgrade também
  foram removidos. Cada `menuitem` continua protegido explicitamente por
  `base.group_system`; portanto, a simplificação elimina comportamento redundante sem
  ampliar acesso.
- Testes de regressão cobrem esses contratos. A versão final é
  `contact_center_base 16.0.1.43.5`, com inventário de **505** testes Python.

### CRM

- Roster, inventário, bindings, estágios, leads e casos obedecem a uma ordem canônica
  única de locks. Revisões são revalidadas após cada fronteira, evitando uma visão
  parcialmente antiga do CRM e parcialmente nova do Contact Center.
- Escritas dinâmicas dos formulários nativos de usuário (`in_group_*` e `sel_groups_*`)
  passam a contar como decisão humana explícita sobre a propriedade da permissão, além
  de `groups_id`.
- Exclusão/tombstone de lead preserva o usuário real da chamada, verifica ACL e regra
  antes do `sudo()` técnico e não atribui a operação ao superusuário.
- O binding aposentado guarda separadamente o snapshot imutável do lead original e o
  snapshot mais recente. O baseline corrente preserva a melhor evidência disponível;
  dados sem origem comprovável não são inventados.
- Bindings de pipeline são archive-only. Exclusão física existe somente no contexto
  controlado de desinstalação do addon.
- O inventário de mapeamento exige simultaneamente Administrador do Contact Center e
  Gerente de Vendas. Isso é validado em código porque ACLs do Odoo são aditivas e não
  expressam a interseção de dois grupos.
- O inventário de mappings é pré-computado em lote, em vez de repetir buscas por fonte e
  por equipe; isso elimina o crescimento aproximado `fontes × equipes²` do caminho
  anterior.
- Snapshots de pipeline consultam somente os bindings relevantes à fonte em análise, e
  os vínculos de lead/caso são carregados em lote. A otimização preserva os mesmos
  fences, locks e regras de autorização; não introduz cache compartilhado nem estado
  eventualmente consistente.
- Quatro regressões adicionais caracterizam os caminhos em lote. A versão final é
  `contact_center_crm 16.0.2.4.6`, com inventário de **91** testes Python.

## Disposição dos helpers antigos de identidade e migrations

O corte do primeiro baseline produtivo foi realizado no código. Os métodos abaixo, que
existiam somente para transformar bancos pré-produtivos, foram removidos:

- `_contact_center_backfill_fallback_phone_names`;
- `_contact_center_prepare_name_sources`;
- `_contact_center_backfill_observed_names`;
- `_contact_center_reconcile_portable_duplicates`;
- `_contact_center_portable_duplicate_components`.

As migrations pré-produtivas de `contact_center_base`, `contact_center_crm` e
`contact_center_wuzapi` também foram removidas. O contrato passa a ser um primeiro
baseline produtivo exato: instalação nova ou banco já materializado na versão atual. Um
banco antigo de laboratório não é silenciosamente aceito como origem de upgrade; o
release deve falhar fechado em vez de fingir compatibilidade não testada.

Dois métodos não são legado e permanecem:

- `_contact_center_merge_portable_component`, usado pelo fluxo runtime de resolução de
  uma identidade que chega por aliases diferentes;
- `_contact_center_rebind_retired_guest_memberships`, usado por esse merge para não
  deixar membros presos ao guest aposentado.

## Complexidade e fronteiras

O alerta sobre métodos e arquivos grandes é válido como risco de manutenção, mas não
como prova de defeito. O base contém serializers extensos em `ui_api.py`, serviços de
aplicação e modelos de fila com muitas regras. Um split mecânico por número de linhas
criaria mais chamadas cruzadas sem definir uma autoridade nova.

A decomposição continuará como refactor orientado por mudança, e não como uma meta
cosmética de tamanho de arquivo. A regra para as próximas mudanças é incremental:

1. caracterizar o comportamento com teste;
2. extrair somente uma fronteira com vocabulário próprio — serializer, política de
   identidade, operação outbound ou API da UI;
3. manter locks, authorization fence e transação no serviço que continua dono do caso de
   uso;
4. provar que nenhum provider ou UI passou a ser dependência do base.

As referências a WhatsApp que permanecem no núcleo descrevem semântica de plataforma
(`whatsapp.pn`, formatação humana e prioridade de alias), não detalhes da WuzAPI. Antes
de um terceiro ecossistema com semântica equivalente, extraí-las para mais um addon
seria abstração especulativa. Um registro pequeno de semânticas de plataforma é o
próximo passo caso esse terceiro canal apareça.

## Retenção

Não será implementado um cron que apague indiscriminadamente inbox, outbox, touchpoints
ou evidências. Esses registros também sustentam dedupe, replay, reconciliação e
auditoria. O gate produtivo deve definir por classe de dado:

- finalidade e base legal;
- prazo padrão e `retain_until`;
- legal hold;
- quais identificadores podem ser criptodestruídos ou anonimizados mantendo hash, chave
  de dedupe e prova operacional;
- lote máximo, observabilidade e teste de restauração/replay após expurgo.

Isso confirma o risco da auditoria, mas refuta que apagar o ledger inteiro seja uma
correção segura de código.

## Validação desta revisão

- Release atômico concluído com estado `applied_and_validated` e árvore
  `6554c71d87d0111c5338ef4bb2a2fb2ed4db5afe21376a3b81ca66c77543e59b`.
- Versões finais instaladas: Base `16.0.1.43.5`, WuzAPI `16.0.1.29.2`, Meta
  `16.0.2.1.1`, CRM `16.0.2.4.6` e UI `16.0.1.26.5`.
- Passaram **505/505** testes Base, **199/199** WuzAPI e **895/895** integrados, todos
  sem falha ou erro. O CRM conserva inventário próprio de **91** testes dentro da suíte
  integrada.
- Formatação, imports, flake8/complexidade, compilação, XML e contratos estáticos:
  executados durante o pente-fino.
- O contrato estático também impede a reintrodução de diretórios `migrations/` no
  baseline Contact Center sem uma decisão explícita de versão.
- O upgrade offline terminou com código `0`. QUnit passou no bundle minificado e em
  `debug=assets`: viewer Base **4/4**, **15/15** asserções; UI **144/144**,
  **1248/1248** asserções, sem falhas, skips ou TODOs.
- A rota Traefik foi restaurada byte a byte e o endpoint interno e público respondeu
  HTTP `200` ao final.
- Produção não foi acessada nem alterada (`production_touched: false`). Evidência:
  `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904-final-r4/summary.json`.
