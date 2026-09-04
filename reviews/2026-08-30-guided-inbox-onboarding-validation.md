# Validação — onboarding guiado de caixas

- Data: 2026-08-30
- Alvo: Odoo 16 de teste, SERVIDOR05
- Produção: não acessada nem alterada

## Resultado

O fluxo administrativo de criação de caixa foi substituído por um assistente guiado e
provider-neutral. A referência visual do EvoAPI Manager/discuss-hub foi usada para a
clareza do pareamento, sem acoplar o core à WuzAPI nem copiar a arquitetura do addon.

Fluxo entregue:

1. Atendimento: nome, equipe, provider e preferências operacionais.
2. Provedor: configuração específica injetada pelo addon selecionado.
3. Conectar: provisionamento/verificação assíncrona e QR quando necessário.
4. Revisão: provas locais de sessão, identidade, HMAC e callback.
5. Pronta: promoção explícita da conexão e abertura da Central de Atendimento.

## Divisão de responsabilidades

- `contact_center_base`: wizard, navegação, conta/caixa, equipe, preferências, estados,
  preflight de ativação, buffer e replay do primeiro ingress.
- `contact_center_wuzapi`: servidor gerenciado, criação ou vínculo da instância, tokens,
  HMAC, callback, QR, health e provas de identidade.
- Outros providers podem herdar os mesmos hooks sem adicionar condicionais ao core.

## Invariantes de segurança e concorrência

- Conexão guiada nasce em `migration`, com inbound e outbound desligados.
- O composer e o webhook não promovem a conexão implicitamente.
- Callbacks anteriores à ativação ficam no ledger com
  `blocked_reason=onboarding_not_activated` e são liberados em lotes pelo OCA
  `queue_job` somente após o cutover.
- Health remoto usa o instante de início da leitura; um lifecycle posterior não pode ser
  sobrescrito por uma resposta antiga.
- `onboarding_ingress_revision` cria uma fence MVCC: ativação concorrente com o primeiro
  callback enxerga o hold ou recebe `SerializationFailure` e repete em snapshot novo.
- Eventos de desconexão mais novos que a última prova de health bloqueiam a ativação até
  nova verificação.
- A ativação repete o preflight sob os locks canônicos da topologia.

## Correções encontradas na validação pelo navegador

- Funções CSS `clamp()` são emitidas literalmente para compatibilidade com o LibSass do
  Odoo 16; o bundle deixou de falhar por mistura de `%`, `px`, `rem` e `vw`.
- A disponibilidade do provider passou a nascer como default persistido no transient;
  antes o formulário novo podia exibir WuzAPI e, simultaneamente, o estado vazio.
- Campos WuzAPI são validados no servidor somente em “Criar configuração segura”. Isso
  mantém os erros específicos sem bloquear “Voltar” quando a etapa ainda está vazia.
- Rótulos, etapas e menus do onboarding foram traduzidos para pt_BR.
- `_rec_name = inbox_name` removeu o nome técnico do transient do título e breadcrumb.
- Layout conferido em 1440×900 e 390×844. Console final: 0 erros e 0 warnings.

## Auditoria final independente

- O token de uma instância existente exige pelo menos 32 caracteres e nunca volta ao
  navegador depois de persistido.
- A URL da WuzAPI representa somente a origem canônica (`scheme://host[:port]`); paths
  equivalentes não conseguem mais produzir fingerprints distintos para a mesma sessão.
- A prova de health do onboarding registra estado e métricas sem limpar o UUID de um
  health job concorrente. Somente o job proprietário conclui a fila, preservando uma
  observação restritiva posterior.
- Um setup WuzAPI em erro pode voltar à configuração e corrigir nome/token reutilizando
  caixa, conexão e referência idempotente; servidor e modo permanecem imutáveis.
- Estado vazio sem provider, botões provider-neutral, contraste e traduções de erros
  administrativos foram corrigidos.

Capturas locais:

- `output/playwright/onboarding-final-desktop.png`
- `output/playwright/onboarding-final-provider-desktop.png`
- `output/playwright/onboarding-final-mobile.png`
- `output/playwright/onboarding-final-provider-mobile.png`

## Gate automatizado final

- Base: **355/355**, 0 falhas, 0 erros.
- Integrado (base + WuzAPI + Meta + UI): **699/699**, 0 falhas, 0 erros.
- Bancos temporários removidos ao final.
- Árvore local/remota idêntica:
  `eacd92b4df2d07e87a05c6cf7c4bb7d94dd007fd358d3dd80590d1d7b0b7aea4`.
- Evidência canônica:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260830T094552949222Z`.
- Release atômico do laboratório sem backup, conforme política de desenvolvimento:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260830T094454051481Z`.

## Versões instaladas

- `contact_center_base`: `16.0.1.28.5`
- `contact_center_wuzapi`: `16.0.1.26.4`
- `contact_center_meta`: `16.0.1.9.0`
- `contact_center_ui`: `16.0.1.18.3`
- `queue_job`: `16.0.3.0.2`
