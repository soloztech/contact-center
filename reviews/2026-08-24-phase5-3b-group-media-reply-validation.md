# Validação da Fase 5.3b — mídia outbound e reply em grupos

- Data: 2026-08-24
- Ambiente: SERVIDOR05, banco neutralizado `odoo16`
- Produção: não acessada nem alterada
- Resultado: implantada e validada; nenhum envio real para grupo foi executado

## Escopo entregue

- DTO provider-neutral para o participante protocolar próprio do grupo e para o
  participante da mensagem-alvo em replies.
- Seleção PN/LID a partir do roster autoritativo e do `AddressingMode` da sessão WuzAPI.
- Envio de texto, imagem, áudio, vídeo e documento para grupos com opt-in; áudio sem
  legenda e um único anexo por mensagem.
- Reply de grupo com participante obrigatório e falha fechada quando o alvo não possui
  resolução canônica.
- Revalidação de conexão, profile, revisão de saúde, roster e participante antes do I/O
  e depois da resposta do provider.
- Eco `from_me` idempotente, equivalência PN/LID limitada ao mesmo participante do
  roster e conflito de target/reply bloqueado.
- Migração `16.0.1.11.0` para capabilities, metadata, limpeza de evidência obsoleta e
  backfill histórico de participantes.

## Invariantes de segurança e consistência

- O composer cria mensagem, binding, mídia e outbox; nunca chama o provider diretamente.
- Somente sucesso confirmado copia ID externo e participante para a projeção. Resposta
  ambígua permanece `uncertain` e não inventa confirmação.
- Mudança de conexão, health revision, endereço do grupo, roster ou participante entre
  criação e dispatch invalida o comando antes do envio.
- O participante histórico persistido prevalece sobre uma inferência posterior; PN e LID
  alternativos só são equivalentes quando o roster atual comprova a mesma pessoa.
- A migração respeita o lock order `connection -> channel/profile -> binding` e também
  retropreenche bindings históricos quando ainda não existe profile local.

## Versões implantadas

- OCA `queue_job`: `16.0.3.0.2`;
- `contact_center_base`: `16.0.1.14.0`;
- `contact_center_wuzapi`: `16.0.1.11.0`;
- `contact_center_ui`: `16.0.1.12.0`;
- tree hash: `b8c82a93e419a63c373a7012cda7c442c3e766d95f69b96ab0f58502b7828372`.

O upgrade terminou sem módulos pendentes e sem backup do banco descartável, conforme a
decisão operacional do laboratório.

## Testes e qualidade

- Odoo base: **165/165**, zero falha/erro.
- Odoo integrado: **252/252**, zero falha/erro.
- QUnit UI minificado: **48/48**, 438/438 asserções.
- QUnit UI `debug=assets`: **48/48**, 438/438 asserções.
- Console do smoke real: zero erro e zero warning.
- Black, isort, Flake8, Pylint obrigatório, Prettier, ESLint e checks OCA: aprovados.
- `py_compile`, `node --check`, XML e `git diff --check`: aprovados.

Hashes dos logs finais:

- base: `ee39d0bc61756da4e091f81d1a3909e57bb8bee50c70856bd24f5a8fe6cac620`;
- integrado: `286a28ade7659c2ccf845a1783515d2465e07db7d6f054dcecda1b3bb21b6b0d`.

## Estado comprovado no laboratório

- **62/62** profiles de grupo em `ready`.
- **62/62** profiles com participante canônico e revisão igual à saúde vigente.
- **4/4** mensagens históricas outbound de grupo com evidência de remetente
  retropreenchidas.
- Cinco contas ativas; uma com opt-in de grupo e quatro mantidas bloqueadas por padrão.
- Frota **5/5 conectada**.
- UI autenticada com 159 conversas; grupo, participantes observados e metadata
  renderizados.
- Uma conta sem opt-in mostrou `Envio não habilitado`, comprovando falha fechada na UI.
- Nenhuma mensagem real foi enviada a grupo durante o smoke.

Os seis jobs antigos de metadata em `failed` são histórico do `queue_job`; não
correspondem a profiles atualmente falhos, pois todos os 62 profiles convergiram para
`ready`.

## Evidências

- Código implantado:
  `scans/raw/20260824-odoo16-contact-center-phase5-3b-group-media-reply/deploy-final5-isolated/`.
- Testes isolados:
  `scans/raw/20260824-odoo16-contact-center-phase5-3b-group-media-reply/test-final4/`.
- Upgrade:
  `scans/raw/20260824-odoo16-contact-center-phase5-3b-group-media-reply/upgrade-final/`.

Durante uma tentativa anterior, o restart foi saturado por requisições públicas. O
deploy fez rollback automático da árvore de código. A rota do laboratório foi então
isolada temporariamente, preservando seu SHA-256, e o mesmo deploy passou em 18
segundos. A rota foi restaurada byte a byte e o endpoint público terminou respondendo
HTTP 200. Esse evento foi operacional e não alterou o resultado dos testes do código.

## Limites preservados

- Reaction, edit e delete em grupos continuam na Fase 5.3c.
- Receipts por participante continuam na Fase 5.3d.
- O opt-in de grupo permanece desligado por padrão e deve ser liberado por caixa.
- Esta homologação não substitui um smoke real controlado de mídia/reply nem o aceite do
  piloto em produção.
