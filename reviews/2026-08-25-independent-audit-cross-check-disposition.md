# Disposição do cross-check da auditoria independente — 2026-08-25

Fonte analisada: `reviews/2026-08-25-independent-audit-cross-check.md`.

## Veredito

O cross-check encontrou defeitos reais. Os dois P1, as três regressões e as duas
inversões de lock foram corrigidos. As recomendações não foram aplicadas literalmente
quando isso preservaria a perda de mensagens ou criaria uma política de produto ainda
não definida.

| Achado | Veredito                      | Disposição                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------ | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| XC-01  | Confirmado                    | Depois de `unique_violation`, re-search vazio sob `REPEATABLE READ` agora gera erro retentável; conflito terminal só é concluído quando o alias concorrente está visível e pertence a outro participante. O comentário enganoso sobre renovação do snapshot foi corrigido.                                                                                                                                                                                                |
| XC-02  | Confirmado, solução revisada  | `referral` é telemetria opcional: campos inválidos são descartados sem rejeitar o webhook. O normalizador também degrada atribuição inválida para vazia, mesmo se receber envelope não saneado. Uma falha de domínio/DTO na captura do ledger não descarta a mensagem, fica registrada em `metadata_json.attribution_capture_failure` e o DTO normalizado continua disponível para recuperação. Falhas de banco ou programação continuam retentáveis e não são ocultadas. |
| XC-03  | Confirmado                    | A captura inicial não grava FKs da projeção. `channel_binding_id`, canal, mensagem e identidade são vinculados somente em `_link_projection`, depois da ordem canônica de locks.                                                                                                                                                                                                                                                                                          |
| XC-04  | Confirmado                    | Recibos de grupo passam a bloquear e revalidar o canal antes de enriquecer participante/alias e tocar o perfil de grupo.                                                                                                                                                                                                                                                                                                                                                  |
| XC-05  | Confirmado                    | Prefetch sem perfil usa recordset vazio; uma inconsistência histórica degrada o item, sem derrubar `list_conversations`.                                                                                                                                                                                                                                                                                                                                                  |
| XC-06  | Confirmado                    | A conversa selecionada reanexada não realimenta a paginação. Refresh em tempo real tem teto de 200 itens e conserva o limite já carregado.                                                                                                                                                                                                                                                                                                                                |
| XC-07  | Confirmado e corrigido        | A lane de mídia ganhou o mesmo preflight fail-closed de saúde usado pelas leituras de grupo/avatar. Quando conexão, conta, sessão, identidade ou observação não estão saudáveis, o job é apenas adiado e nenhuma chamada é feita ao provider. O backoff global não foi alterado porque isso prejudicaria lanes cujo pause é apenas local.                                                                                                                                 |
| XC-08  | Crítica aceita                | A refutação anterior respondeu ao escopo errado. Não haverá expurgo nesta etapa: retenção foi explicitamente retirada do escopo do produto. O próximo passo técnico é medir volume por ledger antes de definir compactação. `raw_envelope_json` pode manter `{}` como sentinela; não é obrigatório tornar o campo opcional.                                                                                                                                               |
| XC-09  | Parcialmente aberto           | Consulta negativa continua indisponível e reenvio automático permanece proibido. É necessária uma resolução administrativa explícita para comandos `uncertain`, com carência e auditoria, sem redispatch implícito.                                                                                                                                                                                                                                                       |
| XC-10  | Factual, fora do escopo atual | Retenção/apagamento não foi implementado por decisão expressa desta fase. Não bloqueia o laboratório, mas deve ser tratado antes de uma política formal de produção.                                                                                                                                                                                                                                                                                                      |

Também foram corrigidas três lacunas Meta de baixo custo detectadas no cross-check:

- `source_type` de anúncio passa a ser canonicamente `ad` em todos os providers;
- identificadores enriquecidos usam o `occurred_at` da nova evidência;
- `_link_projection` escreve o ledger em ambiente elevado controlado.

`META-10` (evento `referral` sem mensagem) e `META-16` (escopo da chave canônica de
mensagem) permanecem no backlog da Fase 6, porque exigem fixture real e definição do
contrato do provider.

## Validação

- Suíte base: **255/255**, sem falhas ou erros.
- Suíte integrada: **421/421**, sem falhas ou erros.
- QUnit autenticado do modelo UI: **75/75** testes, **625/625** asserções, zero erro ou
  warning no console.
- Upgrade e validação canônica no SERVIDOR05 concluídos com base `16.0.1.20.2`, WuzAPI
  `16.0.1.17.0`, UI `16.0.1.17.1`, Meta `16.0.1.1.1` e OCA `queue_job 16.0.3.0.2`.
- Árvore funcional implantada:
  `b49e5b05bf95ab6f46b600d80246533635baa7ac90a739f8acd92e9a881e4985`.
- Evidências canônicas em
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/`: deploy final
  `20260825T191507851523Z`, testes `20260825T190927936757Z`, upgrade
  `20260825T191239218564Z`, validate `20260825T191603538591Z` e validação da árvore
  implantada `20260825T191630422482Z`.

O teste de XC-01 cobre de forma determinística a decisão após `IntegrityError`. Um
ensaio com dois cursores concorrentes continua útil como teste de integração adicional,
mas a classificação terminal incorreta e o diagnóstico falso já foram removidos do
caminho de produção.

## Pendências que não devem ser confundidas com regressão desta rodada

1. Saída administrativa auditável de `uncertain`, sem reenvio automático (XC-09).
2. Métrica de crescimento dos ledgers; política de retenção somente quando o produto a
   definir.
3. Commit/remote e os ensaios operacionais do piloto: reconexão real, canário de timeout
   ambíguo e carga do JobRunner.
