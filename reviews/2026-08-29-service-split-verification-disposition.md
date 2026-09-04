# Disposição — verificação da divisão do serviço

- Data: 2026-08-29
- Entrada: `reviews/2026-08-29-service-split-verification.md`
- Escopo: confrontar cada afirmação com o código atual, corrigir defeitos reais e
  homologar somente no SERVIDOR05.

## Veredito

A conclusão sobre o refactor procede: a separação de `application.py`,
`application_outbound.py` e `ui_api.py` conservou a fachada e o comportamento. Os
problemas N1–N7 e M4 apontados fora do split também eram reais, com qualificações
importantes em N2, N3, N4, M2 e M4. A interpretação do conflito legado #1263 como
possível merge de identidade não procede para o registro concreto do laboratório.

## Disposição dos achados

| Item                                        | Disposição                                | Resultado                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pureza do service split                     | Confirmado                                | A fachada Odoo e os testes comportamentais permanecem canônicos. Nenhuma mudança adicional no split foi necessária.                                                                                                                                                                                                        |
| Documento/tag 1.24.1 ausentes               | Refutado por estado mais novo             | O plano já registrava a homologação e a tag `16.0.1.24.1-lab` já existia.                                                                                                                                                                                                                                                  |
| N1 — rate limit no health Meta              | Aceito e corrigido                        | `ProviderRateLimitError` agora é tratado antes de `ProviderPausedError`; preserva a autorização válida e retorna à lane de retry sem marcar `authentication_required`.                                                                                                                                                     |
| N2 — anexo Meta removido pelo sanitizer     | Aceito com escopo corrigido               | Uma lista vazia vira `unsupported` somente quando existe evidência de rejeição pelo sanitizer. Entrada malformada direta continua erro permanente, sem mascarar corrupção de contrato.                                                                                                                                     |
| N3 — `UserError` em mutation de grupo       | Aceito com captura local                  | Apenas a falha conhecida de participante próprio ainda não resolvido vira erro transitório. Não foi adicionada captura genérica de `UserError` na fila.                                                                                                                                                                    |
| N4 — roster antigo em delete administrativo | Aceito; solução proposta era insuficiente | A autorização exige roster completo, aplicado e cuja solicitação começou depois do recebimento. O evento aguarda de forma durável um refresh e é reenfileirado após snapshot válido. Isso cobre promoção, rebaixamento e participante ausente.                                                                             |
| N5 — nome presente somente na mutation      | Aceito e corrigido                        | Nome observado na mutation é persistido com proveniência antes do fallback por telefone e ordenado pelo `occurred_at` do provider, não pela chegada tardia do inbox.                                                                                                                                                       |
| N6 — alias direto em mensagem de grupo      | Aceito e corrigido                        | O tipo da conversa do binding alvo é a fonte canônica e deve coincidir com o evento antes de enriquecer aliases.                                                                                                                                                                                                           |
| N7 — `target_from_me` obrigatório           | Aceito para inbound                       | Tornou-se evidência opcional no DTO inbound. Quando presente continua estritamente booleano. O comando outbound mantém o campo obrigatório.                                                                                                                                                                                |
| M2 — logging do webhook Meta                | Parcialmente aceito                       | Rejeições POST agora têm teste de log limitado a status, motivo, App ID, SHA-256 e tamanho, sem corpo ou segredo. Exigir warning para o GET de verificação foi refutado: ele não é uma entrega assinada rejeitada. Também foi refutada a ideia de aceitar/quarentenar `entry` sem campos estruturais de roteamento/dedupe. |
| M4 — 429 fora do outbox                     | Aceito com contrato preservado            | Leituras WuzAPI de mídia, grupo e identidade agora elevam `ProviderRateLimitError` e respeitam `Retry-After` sem consumir o teto comum. No outbox, o contrato existente permanece `transient/http_429`; alterá-lo para `paused/rate_limited` quebraria a classificação do core.                                            |
| Contrato do service split                   | Observação correta, sem defeito           | O teste de registry é intencionalmente uma sentinela de composição. Assinaturas, DTOs e respostas são protegidos pela suíte comportamental, não por duplicação de introspecção.                                                                                                                                            |
| Extrair serializers de `ui_api.py`          | Backlog válido                            | É uma melhoria de manutenibilidade, não uma correção necessária desta rodada. Deve ser outro refactor puro e isolado.                                                                                                                                                                                                      |

### Por que N4 não usa somente `RetryableJobError`

No
[runner do OCA queue_job 16](https://github.com/OCA/queue/blob/16.0/queue_job/controllers/main.py),
uma exceção do job faz rollback do cursor de negócio antes de o estado de retry ser
gravado pelo runner. Portanto, agendar o refresh e em seguida levantar
`RetryableJobError` desfaria justamente o refresh criado na mesma transação. A
implementação persiste a espera, agenda/coalesce o snapshot, finaliza o job atual e
libera o inbox somente depois de um roster autoritativo.

A revisão final encontrou uma corrida adicional: a hora de conclusão da resposta não
prova que o pull começou depois do delete. Por isso, os quatro predicados de liberação
também exigem `sync_requested_at >= waiting_group_roster_after`. O teste reproduz um
pull iniciado antes e concluído depois do evento. Outra regressão de ordem foi fechada
com um teste de mutation antiga entregue depois de um nome mais novo.

O payload atual não traz uma prova histórica do papel do participante no instante do
delete. Em um replay muito tardio, a decisão usa o primeiro roster autoritativo obtido
depois do recebimento local. Isso é uma limitação conhecida do provider, não algo que o
core deva inventar.

## Caso legado #1263

O evento é um `ReadReceipt` WuzAPI antigo, `from_me=true`. A versão antiga tentou
enriquecer aliases antes de classificá-lo como receipt próprio sem suporte, criando o
conflito entre as identidades 2 e 8. Não havia evidência para mesclá-las.

No SERVIDOR05, o conflito #1 foi resolvido com nota auditável e o evento #1263 foi
reenfileirado. Com o código atual convergiu imediatamente de `blocked` para
`unsupported`, sem merge de identidades.

## Validação e homologação

- Qualidade estática: `git diff --check`, compilação Python, Black, isort e flake8 com
  `max-complexity=16` passaram.
- Testes focados cobriram todos os ramos corrigidos, inclusive a literal Graph code 4,
  sanitizer, retry de participante próprio, freshness do roster e 429 tardio.
- Suíte canônica: **304/304** no base e **577/577** integrada, zero falha/erro, em
  bancos temporários removidos ao final.
- Versões instaladas: base `16.0.1.24.4`, WuzAPI `16.0.1.19.1`, Meta `16.0.1.6.1`, UI
  `16.0.1.17.4` e OCA queue_job `16.0.3.0.2`.
- A candidata intermediária `1.24.3` foi rejeitada pela suíte antes do fechamento; uma
  referência SQL não qualificada foi corrigida na `1.24.4` e todos os testes foram
  repetidos desde o início.
- Árvore funcional implantada:
  `f72f13f1c6a46a6ed1420d6bf60d200c6c20c110f02450a91aeb882ac35e4771`.
- Evidências: `scans/raw/20260829-odoo16-contact-center-service-split-remediation/`.
- O deploy atômico e a validação instalada foram feitos somente no SERVIDOR05. A
  produção não foi acessada ou alterada.
