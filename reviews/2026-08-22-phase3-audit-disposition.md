# Disposição da auditoria — Fases 2.1 e 3

- Data: 2026-08-22
- Fonte: [auditoria das Fases 2.1 e 3](2026-08-22-phase3-audit.md)
- Escopo: correções pequenas e verificáveis antes do piloto; mudanças semânticas ou de
  maior alcance permanecem explicitamente adiadas
- Versões da entrega: `contact_center_base` `16.0.1.6.0`, `contact_center_wuzapi`
  `16.0.1.4.0`; `contact_center_ui` permanece em `16.0.1.4.1`
- Status de implantação: concluída e validada no SERVIDOR05
- Adendo M4 de 2026-08-23: concluído somente na árvore local; deploy, upgrade e
  validação remota ainda pendentes

## Decisão

Os achados M1–M11 foram aceitos como problemas reais. Esta entrega trata M1, M2, M3, M5,
M6 e M9 porque possuem correções locais, compatíveis com a arquitetura vigente e
passíveis de teste sem alterar o contrato UiDTO. M4, M7, M8, M10 e M11 permanecem
abertos para uma iteração própria naquele release. O status posterior do M4 está no
adendo abaixo; os números de teste e hashes desta disposição continuam pertencendo
somente à entrega `1.6.0/1.4.0/1.4.1` de 2026-08-22.

## Adendo de 2026-08-23 — M4

M4 foi implementado na árvore local como `contact_center_base` `16.0.1.7.0`,
`contact_center_wuzapi` `16.0.1.5.0` e `contact_center_ui` `16.0.1.5.0`, sem declarar
implantação ou aceite do ambiente:

- cron de um minuto que apenas agenda jobs idempotentes no canal
  `root.contact_center.health`, prioridade 30 e jitter determinístico de 0–29 s; HTTP
  ocorre apenas dentro do worker OCA;
- estados canônicos `connected`, `degraded`, `disconnected` e `authentication_required`;
  `unknown` derivado e `checking` transversal;
- polling de `/session/status` combinado com os eventos WuzAPI exatos `Connected`,
  `KeepAliveRestored`, `Disconnected`, `ConnectFailure`, `StreamReplaced`,
  `KeepAliveTimeout`, `StreamError`, `LoggedOut`, `QRTimeout`, `ClientOutdated` e
  `TemporaryBan`;
- dedupe por ciclo que permite nova transição do mesmo tipo depois que um health job
  observou outro estado, sem duplicar retries do ciclo;
- comparação segura entre a identidade própria configurada e o JID normalizado da
  sessão; mismatch vira `degraded/identity_mismatch`, sem expor JID bruto;
- painel/UiDTO incremental para aproximadamente 20 números, respeitando roster, empresa
  e papel; refresh manual apenas agenda health e exige Supervisor/Administrator;
- dispatch permitido somente para conexão ativa, outbound ativa e `connected`;
  recuperação restrita a `pending`/`retry` comprovadamente pré-boundary, nunca
  `processing`, `uncertain`, `dead`, `done` ou `cancelled`;
- nenhum caminho automático chama connect, login, QR ou outra mutação de sessão.

O runner atual `root:1` permanece gate do piloto. A prioridade 30 evita que probes
pendentes ultrapassem inbox/outbox de prioridade 10, mas não preempta um probe já em
execução. `root:4,root.contact_center.health:1` é apenas uma hipótese a testar: sintaxe,
hierarquia e capacidade real devem ser validadas antes de alterar a configuração.

Evidência final pendente: `[testes isolados]`, `[testes integrados]`, `[deploy/hash]`,
`[dry-run]`, `[upgrade]`, `[validate]`, `[webhooks lifecycle]`,
`[smoke disconnect/reconnect]` e `[teste de capacidade do JobRunner]`. O procedimento
está no incident/runbook
`odoo16/incidents/2026-08-23-odoo16-contact-center-m4-health-recovery.md`.

## Aplicado nesta entrega

- **M1 — aplicado:** download de mídia usa `retry_after` explícito do provider quando
  existente e, nos demais casos, deixa o `retry_pattern` do OCA `queue_job` decidir o
  backoff. O teto passa de 8 para 12 tentativas e o pattern cobre as novas tentativas.
- **M2 — aplicado com escopo manual:** Supervisor recebe lista/formulário de downloads
  de mídia, filtro de falhas e ação **Retry download**. A ação zera tentativas e erros
  antes de reenfileirar. Requeue automático por cron não foi adotado nesta entrega, pois
  repetiria também falhas permanentes sem uma classificação adicional.
- **M3 — aplicado no backoff:** receipt ou mutação que chegou antes do alvo deixa de
  fixar retries em 10 segundos e passa a usar o pattern declarativo do `queue_job`. O
  estado terminal específico `orphan`/`blocked` ainda não foi introduzido.
- **M5 — aplicado na entrega HTTP:** conteúdo que não seja imagem, áudio ou vídeo seguro
  é servido como anexo; a resposta inclui CSP restritiva e
  `X-Content-Type-Options: nosniff`. O endurecimento adicional por sniffing no download
  inbound permanece no backlog.
- **M6 — aplicado:** `raw_envelope_json` e `normalized_dto_json`, que podem conter
  material técnico como `mediaKey`, ficam restritos ao grupo Administrator no modelo e
  na view. Metadados sanitizados continuam disponíveis ao Supervisor.
- **M9 — aplicado:** regras por empresa para Administrator foram adicionadas aos ledgers
  Inbox e Outbox, tornando as ações administrativas alcançáveis sem exigir participação
  no roster da equipe.

Não foi criado scheduler paralelo: todos os retries continuam sob responsabilidade do
OCA `queue_job`.

## Observabilidade incluída no mesmo release

- Viewer JSON somente leitura reutilizável para os ledgers, com formatação, cópia,
  tamanho, número de linhas e estado vazio; não usa `t-raw` nem HTML dinâmico.
- A aba inbound passa a se chamar **Sanitized Envelope**, deixando explícito que não é
  uma cópia byte a byte do webhook.
- A Outbox passa a persistir `provider_request_json` sanitizado antes do I/O externo, no
  mesmo boundary durável que marca o dispatch como `processing`.
- Para mídia outbound, o snapshot guarda apenas metadados, tamanho e SHA-256; base64,
  binário, headers e credenciais são proibidos. Registros históricos anteriores à versão
  não recebem um request reconstruído artificialmente e exibem estado vazio.

## Adiado deliberadamente

- **M4 — superado pelo adendo de 2026-08-23 no código:** health periódico, eventos de
  sessão e requeue foram implementados em um fluxo único. Aceite operacional continua
  pendente até executar o runbook e preencher as evidências.
- **M7:** ordenação monotônica de edits/reactions exige definir precedência, empate e
  comportamento para eventos sem timestamp confiável.
- **M8:** o ator correto de eco `from_me` requer uma decisão consistente entre autor
  técnico, agente da UI e dispositivo externo.
- **M10:** reduzir o pico de memória no envio de mídia exige revisar acesso ao
  filestore, streaming e limites por tipo, não apenas trocar uma linha do adapter.
- **M11:** merge incremental da timeline precisa preservar cursores, scroll, paginação
  já carregada e convergência com eventos de bus.

Os achados baixos permanecem no backlog da Fase 4. Em especial, sanitizer por
allow-list, streaming da rota de conteúdo, semântica de mutação após delete, reações
nativas do Discuss, acessibilidade da timeline e cobertura DOM/QUnit devem ser tratados
em lotes próprios.

## Pendências de processo preservadas

- Colocar os addons sob Git na branch `16.0`, com ponto de rollback e review por diff.
- Registrar separadamente o incidente histórico da Fase 1.
- Definir expurgo de PII do laboratório antes de copiar banco ou evidências.
- Exercitar e registrar mídia inbound real antes do piloto; fixtures não substituem esse
  ensaio. Os QUnit deste release já possuem resultado e logs brutos preservados.

## Aceite desta entrega

- testes isolados do core: **63 aprovados, 0 falhas/erros**;
- testes integrados base + WuzAPI + UI: **100 aprovados, 0 falhas/erros**;
- QUnit base: **4 testes/15 asserções aprovados** em minificado e `debug=assets`;
- QUnit UI: **20 testes/97 asserções aprovados** em minificado e `debug=assets`;
- upgrade/validate do banco neutralizado: **concluídos**;
- hash da árvore final implantada:
  `bcb10d73b37fb6a0621f4ac54d7fb092900ea57dbc249a406253def44cbd3647`;
- testes isolados finais:
  `scans/raw/20260822-odoo16-contact-center-json-audit/test-format-final/`;
- QUnit final e logs brutos:
  `scans/raw/20260822-odoo16-contact-center-json-audit/qunit-final/`;
- upgrade e validação:
  `scans/raw/20260822-odoo16-contact-center-json-audit/upgrade/20260822T113913806365Z/`
  e `scans/raw/20260822-odoo16-contact-center-json-audit/validate-format-final/`;
- produção: **não acessada nem alterada**.
