# Verificação — divisão do serviço de aplicação (base 1.24.1)

- Data: 2026-08-29
- Revisor: Claude (auditor)
- Escopo: 5 commits sobre `16.0.1.24.0-lab` (`7b549e6`…`adca48b`); só
  `contact_center_base` mudou (`meta`/`wuzapi`/`ui` sem diff).

## Veredito

**Refactor correto e comprovadamente puro.** Comparação independente por AST entre
`application.py@16.0.1.24.0-lab` e a união de `application.py` +
`application_outbound.py`

- `ui_api.py`: **121 métodos antes e depois, 0 removidos, 0 novos, 0 corpos alterados**
  (normalizados por espaço). Corrobora a alegação "57/57 e 62/62 AST-equivalentes".
  Ordem de import (`application` → `application_outbound` → `ui_api`) garante a
  composição do registry antes dos consumidores. Black/isort/flake8 limpos; `py_compile`
  ok; 566 testes contados.

Tamanhos: `application.py` 5 306 → **2 302** linhas (44 métodos); `ui_api.py` 2 118
(57); `application_outbound.py` 908 (20). É a recomendação da revisão de qualidade
executada; o próximo corte natural é extrair os serializers (`_serialize_*`, ainda em
`ui_api.py`, com as duas maiores funções do repositório).

## Estado no lab

Ao contrário do que a review `2026-08-29-application-service-refactor.md` diz
("homologação remota pendente"), a evidência mostra o gate **cumprido**:
`scans/raw/20260829-odoo16-contact-center-application-refactor/` tem `release-apply`
`applied_and_validated` (tree `27f632b9…`), `tests-apply` **296/296 + 566/566**,
`validate-installed` e `browser-smoke`; o lab reporta `contact_center_base 16.0.1.24.1`.
Faltam: atualizar o documento e criar a tag `16.0.1.24.1-lab` (última tag é 1.24.0).

## Pendências da verificação anterior — inalteradas

Nenhum dos defeitos novos da remediação foi tocado (sem diff em `contact_center_meta`
/`contact_center_wuzapi`):

- **N1 (alto)** `meta/models/health.py:559` ainda só `except ProviderPausedError` → rate
  limit vira `authentication_required` e fecha o outbound Meta.
- N2 `normalizer.py:481-484` ainda levanta `AdapterError` com `attachments: []` →
  `dead`.
- N3 `queue.py:498` ainda não captura `UserError` → 12 retries + traceback.
- N4 papel de admin do roster stale continua terminal.
- N5–N7, M2 (log/GET), M4 (contrato `error_code` sem teste; 429 fora do outbox).
- Conflito **#1263** segue `blocked` desde 22/08.

Lab (29/08 17:18 BRT): 7 conexões saudáveis (Meta `degraded` por pendências externas),
dead +25 desde 29/08 — só recibos órfãos, classe aceita; 6 `pending` recentes.

## Observação sobre o teste de contrato

`test_application_service_contract.py` verifica apenas **existência/callable** dos
métodos no registry (10 de aplicação + 19 RPCs). Não fixa assinaturas nem formatos de
resposta; a proteção real contra regressão do split são os 566 testes existentes, que
passaram. Adequado como sentinela de composição; não substitui testes de contrato de
DTO.
