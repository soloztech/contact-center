# Caixa prioritária, densidade compacta, fluxo binário e reabertura inbound

Data: 2026-09-01  
Alvo: Odoo 16 neutralizado no SERVIDOR05  
Versões: `contact_center_base 16.0.1.35.0` e `contact_center_ui 16.0.1.23.0`

## Decisões entregues

- A caixa lógica passou a ser o principal metadado operacional nos cartões da lista. O
  cabeçalho da conversa mantém plataforma e provider e acrescenta o nome exato da caixa,
  permitindo distinguir rapidamente a mesma pessoa atendida por números diferentes.
- A lista ganhou densidades `comfortable` e `compact`. A escolha é persistida em
  `localStorage` sob chave versionada; leitura ou escrita indisponível degrada com
  segurança para o modo confortável. No modo compacto os filtros ficam recolhidos e
  podem ser abertos por um controle acessível próprio. O comportamento responsivo móvel
  não é reduzido para a coluna estreita de desktop.
- O workflow de conversa ficou binário: `open` e `resolved`. A seleção, bootstrap,
  filtros e mutações da API local não aceitam mais `pending`. A migração converte
  `pending` de canais Contact Center para `open` e limpa esse valor em canais de outro
  tipo.
- O seletor de três estados foi substituído por uma ação contextual única: **Resolver**
  em conversa aberta e **Reabrir** em conversa resolvida. Quando a mudança tira a
  conversa do filtro ativo, a lista é recarregada e seleciona o próximo item válido.
- A configuração da caixa ganhou **Reabrir conversa ao receber mensagem**, desligada por
  padrão. Com ela ativa, somente uma mensagem inbound realmente nova, depois da
  deduplicação pelo provider, reabre uma conversa resolvida. Replay, receipt, mutation e
  eco `from_me` não disparam a transição. A atribuição existente é preservada e o bus
  recebe `conversation_updated`.

## Cobertura

- **408/408** testes de `contact_center_base`, sem falhas ou erros;
- **204/204** testes de `contact_center_wuzapi`, sem falhas ou erros;
- **742/742** testes integrados de base, WuzAPI, Meta, CRM e UI, sem falhas ou erros;
- QUnit autenticado, bundle minificado: **104/104** testes e **949/949** asserções;
- QUnit autenticado, `debug=assets`: **104/104** testes e **949/949** asserções.

As regressões cobrem o default seguro da política inbound, reabertura de mensagem nova
com preservação do responsável, replay idempotente sem reabertura, contrato binário da
API, ação contextual, reconciliação do filtro e persistência/fallback da densidade.

## Validação funcional no navegador

Foi criado no banco neutralizado um fixture isolado, acessível somente por um usuário de
QA, com uma caixa WhatsApp/WuzAPI e conversas sintéticas aberta e resolvida. O fluxo
autenticado confirmou:

- nome da caixa como metadado principal de cada conversa e no cabeçalho
  `whatsapp · wuzapi · caixa`;
- alternância entre as listas confortável e compacta, com persistência após recarregar a
  página e filtros recolhíveis no modo compacto;
- apenas **Resolver** em conversa aberta e apenas **Reabrir** em conversa resolvida;
- remoção imediata da conversa quando a mudança de estado deixa de corresponder ao
  filtro ativo, seguida da seleção do próximo item válido;
- interface móvel completa em `390 × 844`, sem aplicar a coluna estreita do desktop;
- flag **Reabrir conversa ao receber mensagem** visível e marcada na configuração da
  caixa do fixture;
- console do navegador com **0 erros e 0 avisos** durante toda a sequência.

Capturas:

- [lista confortável](../output/playwright/inbox-two-state-comfortable-20260901.png);
- [lista compacta](../output/playwright/inbox-two-state-compact-20260901.png);
- [conversa resolvida e ação Reabrir](../output/playwright/inbox-two-state-resolved-20260901.png);
- [layout móvel](../output/playwright/inbox-two-state-mobile-20260901.png);
- [flag de reabertura na caixa](../output/playwright/account-reopen-flag-20260901.png).

Depois da validação, a caixa, a conexão, os dois canais e o usuário de QA foram
arquivados sem exclusão. A senha temporária foi rotacionada, os memberships do fixture
foram removidos e uma segunda sessão read-only confirmou que a caixa não aparece mais no
bootstrap operacional.

## Release no laboratório

O release atômico `20260901T091706970057Z` terminou com `status=applied_and_validated`
no SERVIDOR05. A árvore implantada possui hash
`d248475e0ac582d05d65d877e9edb4c10bc2926ddade1e5d6b0b5ccc7d227870`.

Evidência bruta:

`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T091706970057Z/`

O upgrade offline terminou com código zero, os três bancos de testes temporários e seus
containers foram removidos, os cinco addons ficaram instalados nas versões esperadas e
nenhuma atualização de módulo permaneceu pendente. A rota pública
`odoo16-teste.soloz.com.br` voltou com HTTP 200 e o arquivo Traefik foi restaurado com o
hash anterior `2fd9e569856478dfa336391bd226f3c8af05454db56033be6352ef80aad56403`.

O backup do banco neutralizado foi dispensado conforme autorização já registrada para o
laboratório descartável. O aplicador e a evidência registram `production_touched=false`:
SERVIDOR02 e produção não foram acessados nem alterados.
