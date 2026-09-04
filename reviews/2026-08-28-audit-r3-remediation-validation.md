# Auditoria R3 — disposição e validação

Data operacional: 2026-08-28 (America/Sao_Paulo)

## Veredito

A auditoria encontrou problemas reais, mas A1 precisava de uma correção diferente da
sugerida. A implementação foi ajustada, implantada e homologada somente no laboratório
descartável SERVIDOR05. Produção não foi acessada ou alterada.

| Achado | Disposição                               | Resultado                                                                                                                                                                                   |
| ------ | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A1     | Confirmado; solução sugerida era parcial | A direção do binding correlacionado é canônica. `target_from_me` do provider fica apenas como evidência; participante observado é opcional e, quando presente, deve coincidir com o ledger. |
| M1     | Confirmado                               | Texto Meta válido é projetado mesmo com attachment/provider content não suportado; a extensão registra a degradação.                                                                        |
| M2     | Confirmado                               | URL/attachment/item inválido é sanitizado e colocado em quarentena individual; lote assinado não é perdido e respostas não-2xx registram somente digest/evidência limitada.                 |
| M3     | Confirmado                               | Health calcula o conjunto desejado por autorização/Page; a união do App é usada somente ao aplicar subscriptions do App.                                                                    |
| M4     | Confirmado                               | 429/códigos Graph 4, 17, 32 e 613 usam `ProviderRateLimitError`; outbox volta a `pending`, devolve a tentativa e respeita o cooldown compartilhado.                                         |
| M5     | Confirmado                               | Administrator recebeu regra company-scoped no ledger `identity.conflict`, sem ampliar Supervisor/Agent.                                                                                     |
| M6     | Confirmado; decisão de produto aceita    | Delete de grupo é aceito pelo autor ou por `admin`/`superadmin` ativo no roster. Edit continua author-only.                                                                                 |

## Baixos tratados

- verify token Meta compara bytes UTF-8;
- conta Meta sem equipe gera inbox `blocked/AccountNotReady`, reprocessável após a
  configuração;
- flags semânticos inválidos tornam somente o item `unsupported`; campos opcionais e
  locators inválidos não descartam texto/mídia segura restante;
- 403 Graph é decodificado de forma limitada antes do fallback de autenticação, para
  preservar códigos reais de rate limit;
- falha do avatar não apaga `display_name` já obtido;
- delivery Meta ganhou cron de recuperação, adoção/revival de job e ação administrativa
  coerente com seus estados;
- reconciliação de atribuição usa a ordem canônica identity → channel → binding;
- health Meta tem jitter determinístico; rótulos Facebook/Instagram não se misturam;
- datetime de touchpoint é ISO-8601, item opcional inválido não apaga a projeção inteira
  e o composer restaura foco no fluxo do gravador.

## Não alterados por decisão técnica

- `group_inbound_enabled` continua bloqueando somente novas mensagens de participantes.
  Metadata, receipts, mutations e ecos permanecem processáveis por desenho explícito do
  plano; mudar isso deixaria o ledger divergente do WhatsApp.
- Os 19 baixos não verificados não foram tratados como fatos. Permanecem backlog apenas
  após reprodução/medição: custo de refresh/bus, particionamento físico dos arquivos,
  migrações históricas em lote, coordenação de quota Meta por App e `store.start()`.
- O teste de foco cobre a lógica e o browser confirmou o caminho sem microfone; um tour
  OWL com `MediaRecorder` real continua melhoria de CI, não blocker desta correção.

## Migração e dados do laboratório

A migração WuzAPI `16.0.1.19.0` selecionou somente eventos `dead` de grupo, tipos
`message.reaction`/`message.deleted`, que possuíam exatamente uma das duas mensagens de
erro legadas. Após o JobRunner drenar a fila, a leitura ORM confirmou:

- **132/132** eventos marcados em `done`;
- **129** deletes e **3** reactions;
- zero `pending`, `processing`, `blocked`, `unsupported` ou `dead` nesse conjunto.

## Validação

- Black, isort, Flake8 C901≤16, Pylint obrigatório, OCA module checks, compileall, XML,
  Prettier, ESLint e `git diff --check`: aprovados;
- suíte base: **295/295**, zero falha/erro;
- suíte integrada: **565/565**, zero falha/erro;
- bancos temporários removidos;
- QUnit UI minificado e `debug=assets`: **76/76**, **634/634** asserções em ambos;
- aplicação autenticada: 482 conversas, realtime ativo, WuzAPI 5/5 conectada, zero erro
  ou warning de console; caminho de erro do gravador devolveu foco ao composer;
- versões instaladas: OCA `queue_job 16.0.3.0.2`, base `16.0.1.24.0`, WuzAPI
  `16.0.1.19.0`, Meta `16.0.1.6.0`, UI `16.0.1.17.4`;
- árvore implantada: `01eb8395c222e6bf53959c7b53cee365ae89d81794e10d51096d9beb48c5a1e2`.

Evidências canônicas:

- release: `scans/raw/20260828-odoo16-contact-center-audit-r3/release-final-apply/`;
- testes: `scans/raw/20260828-odoo16-contact-center-audit-r3/tests-final/`;
- estado instalado: `scans/raw/20260828-odoo16-contact-center-audit-r3/validate-final/`;
- QUnit/browser: `scans/raw/20260828-odoo16-contact-center-audit-r3/qunit-final/`.

## Observação operacional da release

A primeira tentativa de upgrade encontrou transações concorrentes do container
`odoo16_dbmanager` enquanto o Odoo principal já estava parado, causando deadlock e
rollback automático. O orquestrador foi corrigido para parar/verificar e restaurar os
dois containers durante o upgrade offline. A release final terminou com HTTP 200 e a
rota Traefik de teste restaurada byte a byte. O backup do banco descartável foi
dispensado conforme orientação explícita do operador.
