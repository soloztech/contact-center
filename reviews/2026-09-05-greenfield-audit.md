# Auditoria greenfield — Contact Center

Data: 2026-09-05. Fonte inicial: `324fec1`, branch `16.0`, sem alterações locais.

## Parecer de arquitetura

A separação entre núcleo, adaptadores, interface, Kanban e CRM é adequada ao produto.
Foram revisados os seis addons em conjunto com os dezoito addons do Marketing Center: o
grafo contém 38 dependências internas e nenhum ciclo. `mail.channel`/`mail.message`,
ORM, record rules e OCA `queue_job` continuam sendo os mecanismos canônicos. Não foi
identificado benefício suficiente para criar novas camadas, substituir a fila ou fundir
os addons opcionais.

A auditoria encontrou defeitos concretos apesar da cobertura anterior. As correções
preservam a arquitetura e atacam perda de eventos, atomicidade, autorização, retomada de
transações e classificação de erros. Não foi adicionada tabela, serviço de
infraestrutura ou dependência Python ao produto.

## Achados corrigidos

| Prioridade | Problema e efeito                                                                                                                            | Correção e localização                                                                                                                                             |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Alta       | Arquivamento nativo via `mail.channel.active` ocultava conversas e interrompia recebimento, coexistindo com o estado operacional `archived`. | `models/channel.py` do Base bloqueia essa rota pública; a conversa permanece no ciclo `contact_center_state`. Regressões preservam a semântica dos canais nativos. |
| Alta       | Contexto RPC `contact_center_crm_origin_case_id` podia suprimir sincronização de estágio entre lead e caso.                                  | `contact_center_crm/models/crm_lead.py` só reconhece a origem quando acompanha o token interno não serializável. Teste verifica convergência e revisão do caso.    |
| Alta       | Erro esperado após criar anexo/upload podia retornar HTTP 400 e confirmar parcialmente a transação.                                          | Savepoint do agregado no controller de upload; teste HTTP injeta erro após criação, confirma ausência de resíduos e repete o mesmo UUID.                           |
| Média      | `opened_at` não estava protegido no `write`; defaults RPC podiam forjar `active`/`is_default` na criação do caso.                            | Fronteira de metadados em `contact_center_kanban/models/pipeline.py` aplicada em criação e edição.                                                                 |
| Média      | Uma atualização de dados iniciada na reconexão podia terminar após nova desconexão e marcar a interface como online.                         | O store deixa a conectividade exclusivamente sob controle dos eventos do bus. Timer respeita destruição do componente. Regressão QUnit determinística.             |
| Média      | WuzAPI mantinha exceções externas no traceback, incluindo possíveis URLs assinadas/valores privados.                                         | Erros estáveis na fronteira dos serviços `adapter`, `group` e `onboarding`; causas externas não são propagadas.                                                    |
| Média      | JSON excessivamente aninhado e URLs opcionais malformadas escapavam dos contratos de erro do adaptador.                                      | Tratamento de `RecursionError` e validação de URL nos leitores existentes, preservando fechamento de streams.                                                      |
| Média      | HTTP 408/425 era considerado permanente. Em envio, isso poderia sugerir reenvio de uma operação cujo resultado é desconhecido.               | WuzAPI classifica envio como incerto e consultas/mídia como temporárias. Downloads Meta seguem a classificação temporária.                                         |
| Baixa      | O Kanban tentava chamar um accessor inexistente antes do accessor obrigatório atual.                                                         | Fallback removido; acesso usa diretamente `_contact_center_effective_users`.                                                                                       |

## Concorrência sob carga

O complemento do operador apontou a execução `20260905T024938642834Z`. A evidência
confirma 4.800 webhooks: 4.786 respostas 202 e 14 respostas 400, em 97,623 segundos. Os
14 casos têm `SERIALIZATION_FAILURE` no mesmo worker antes do 400, na admissão que
bloqueia a conta com `FOR KEY SHARE`.

O controller WuzAPI lia o corpo com `get_data(cache=False)`. O retry nativo do Odoo
reexecutava o controller sobre a mesma requisição, mas o stream já estava consumido. A
segunda leitura retornava vazio e produzia `empty_payload`/400. Uma reprodução com o
request real do Werkzeug confirmou 43 bytes na primeira leitura e zero na segunda. O
webhook compartilhado Meta tinha a mesma falha.

A correção preserva o corpo limitado no cache da própria requisição, permitindo que o
retry transacional refaça autenticação, normalização e deduplicação sobre os mesmos
bytes. Não cria cache global, não enfraquece HMAC e não substitui o retry do Odoo. Os
controllers Marketing já seguiam esse contrato e já possuíam regressões correspondentes.

A repetição local aceitou as 4.800 mensagens e os 200 controles. Ao avançar para a
corrida de deduplicação, revelou outra causa: 81 perdedores de pares concorrentes
capturavam a violação de unicidade, mas procuravam o vencedor no mesmo snapshot
`REPEATABLE READ`, onde ele ainda não era visível. A violação escapava como HTTP 400
HTML. WuzAPI e Meta agora sinalizam `40001` especificamente para essa constraint quando
o vencedor permanece invisível, permitindo nova transação e resposta idempotente.

Na fila, conflitos de serialização/deadlock recebiam o backoff de falha do adaptador e
podiam atingir o teto de tentativas. O tratamento usa `RetryableJobError` da própria OCA
com `ignore_retry=True` e espera de cinco segundos. A regressão executa 26 conflitos
através de `Job.perform`, mantém o contador em zero e comprova que a falha real seguinte
consome apenas a primeira tentativa. Não há contador ou mecanismo de retry adicional.

O upload multipart foi verificado separadamente: o Odoo já rebobina `FileStorage` no
retry. Um teste HTTP força o conflito depois da leitura do arquivo e comprova upload
único com o conteúdo intacto; não foi adicionado rewind redundante ao controller.

Ao alcançar a drenagem, o fixture revelou uma configuração incompleta: simulava 100
mensagens `IsFromMe` enviadas por outro aparelho sem configurar `technical_author_id`. O
produto corretamente manteve essas mensagens como não suportadas, com os recibos
aguardando seus alvos. O fixture agora configura um autor sintético nas 20 contas e
valida essa pré-condição. O procedimento de implantação explicita o mesmo requisito para
esse fluxo. A autoria opcional e a recusa de atribuir mensagens a um operador que não as
enviou foram preservadas.

O ensaio anterior aprovou isolamento de rede, 20 probes negativos de HMAC e saúde das 20
conexões. Parou na carga; controles, corrida com operações de interface,
indisponibilidade, retomada, 503, timeout e recuperação de órfãos não tinham sido
aprovados naquela execução. A repetição desta auditoria é registrada abaixo.

## Funcionalidades citadas no complemento

| Item                       | Evidência no código e nos testes                                                                                                                                              |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Kanban opcional separado   | Manifest depende apenas do Base; CRM declara a dependência de casos. `test_two_cases_move_independently_from_operational_state` verifica independência do estado operacional. |
| Primeira mensagem não lida | `_first_unread_message_id`, anchor do store e posicionamento do timeline; `test_anchor_page_starts_at_first_unread_without_advancing_seen` e QUnit de paginação ancorada.     |
| Arquivamento               | Usa `contact_center_state=archived`, mantendo canal ativo. `test_new_inbound_keeps_archived_conversation_archived` preserva o recebimento e o arquivo lógico.                 |
| Fixar                      | Preferência pessoal, `pinned_at` e paginação estável; `test_pinned_conversations_precede_activity_and_cursor_is_stable`.                                                      |
| Silenciar                  | Suprime atenção pessoal sem interromper eventos de realtime; `test_muting_suppresses_attention_but_not_realtime_delivery` e QUnit correspondente.                             |
| Reabertura automática      | Mensagem nova reabre conversa resolvida após deduplicação; testes de reabertura e de duplicata que não reabre.                                                                |

A inspeção da primeira fixação/silenciamento simultâneos encontrou o mesmo problema de
snapshot invisível na criação da preferência. A admissão agora pede retry transacional
para a constraint pessoal específica; preferências continuam isoladas por usuário e
conversa.

## Limpeza e decisões de manutenção

- Os seis addons mantêm `16.0.1.0.0`; os dezoito do Marketing foram alinhados.
- CI aponta para revisões publicadas imutáveis com o grafo de dependências necessário.
  Marketing instala Kanban antes do bridge CRM. O input opcional `peer_ref` aceita
  somente SHA completo para validar o par final de commits via `workflow_dispatch`, sem
  alterar o workflow a cada candidato. A CI usa PostgreSQL 16, como o runtime desta
  auditoria, substituindo PostgreSQL 12, cujo suporte terminou em 2024 conforme a
  [política oficial](https://www.postgresql.org/support/versioning/).
- O README foi reduzido para descrever o produto atual e apontar para evidências
  históricas sem tratá-las como prova da árvore atual.
- O baseline de tentativas de jobs foi mantido: jobs novos que retomam trabalho após
  espera de roster precisam das tentativas anteriores. O nome/comentário que sugeria
  compatibilidade legada foi corrigido.
- A ferramenta `release_migrations/kanban_extraction.py` é uma operação de laboratório
  fora dos addons instaláveis; não é migração implícita de produção. Histórico e recibos
  antigos não foram apagados nem reescritos.
- Fixtures de concorrência agora limpam as projeções opcionais Marketing antes dos
  registros CC da própria fixture. A mudança é exclusiva dos testes e não permite
  exclusão dos ledgers no produto.

## Validação e limites

Evidência privada central: `scans/raw/20260905-centers-greenfield-audit/` no repositório
de infraestrutura.

| Verificação                    | Resultado                                                                                                                                                     |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Base isolado                   | 463 testes da suíte e quatro regressões adicionais de fila, upload e preferências; zero falhas/erros.                                                         |
| QUnit Base                     | 4 testes/15 assertions por modo.                                                                                                                              |
| QUnit UI                       | 152 testes/1.302 assertions por modo.                                                                                                                         |
| Assets                         | Minificados e debug passaram; zero testes ignorados.                                                                                                          |
| Navegador                      | Contact Center desktop 1440×1000 e mobile 390×844; nenhum erro de página e nenhuma rolagem horizontal mobile. Smoke de renderização sem conversas de negócio. |
| Pre-commit                     | Hooks obrigatórios completos passaram.                                                                                                                        |
| Instalação limpa dos 24 addons | 1.679 testes, zero falhas e zero erros.                                                                                                                       |
| Replay de atualização          | Os 24 addons atualizados separadamente dos testes, todos instalados em `16.0.1.0.0`.                                                                          |
| Ensaio operacional             | Completo, exit 0, em `20260905T173155804033Z`; detalhes abaixo.                                                                                               |
| Scripts de operação            | 180 testes, zero falhas.                                                                                                                                      |
| Regressões adicionadas         | 37 testes Python no conjunto e três JavaScript; demais testes relevantes foram reforçados.                                                                    |

O runtime local usa a fonte OCB do laboratório, Python 3.10, PostgreSQL 16 descartável e
a pilha TLS fixada do Marketing. Algumas bibliotecas binárias usam versões compatíveis
para ARM64; o inventário exato está na evidência. A execução não representa uma medição
de capacidade do hardware de produção.

Executar `at_install` durante `-u` sobre um banco completo expõe colunas de addons ainda
ausentes no registry parcial (por exemplo, `account.fiscalyear_last_day`). Isso foi
separado de defeitos do produto: não foram adicionadas dependências falsas às fixtures
para contornar a ordem de carga. A instalação limpa final e o replay de upgrade separado
passaram; as evidências estão em `release-validation.json`.

## Resultado do ensaio operacional

Execução definitiva: `local-operational/20260905T173155804033Z/summary.json`, na pasta
central de evidência. O fixture, o harness e os dois conjuntos de addons estão
identificados por SHA-256. Resultado agregado e limpeza: exit 0.

- 20 contas/conexões, 100 conversas e isolamento entre contas comprovado.
- 4.800 mensagens aceitas com HTTP 202, sem erro; 200 controles aceitos.
- 100 pares concorrentes produziram 100 eventos novos e 100 respostas de duplicata.
- 5.161 eventos concluídos, 100 mídias prontas, 100 reações e 100 recibos projetados.
- Os 20 comandos preservados durante indisponibilidade concluíram após retomada das 20
  conexões. O registro do provedor comprova uma chamada por comando recuperado.
- HTTP 429 teve uma recusa e um sucesso; 503 após aceite, timeout e reset após aceite
  tiveram uma chamada cada, permaneceram `uncertain` e não foram reenviados.
- Estado final: 21 comandos concluídos, três incertos esperados, zero jobs ativos, zero
  jobs falhos e zero registros órfãos. O órfão sintético foi recuperado.
- Banco e filestore do ensaio removidos; nenhum processo do ensaio ficou ativo.

Os cenários posteriores à carga não haviam sido executados na evidência original. Ao
exercitá-los, também foram corrigidas premissas antigas do fixture: UUIDs e consultas
correspondentes, autor técnico, domínio de mídia, estados recuperáveis durante pausa,
contadores transitórios, precisão de datas do órfão e comprovação de health por probe
fresco concluído. Os 23 domínios ORM foram compilados no registry final antes da última
repetição. A validação exige os efeitos finais e a contagem real de chamadas do
provedor. As tentativas incompletas permanecem registradas com seus motivos e provas de
limpeza.

A carga local levou 36,369 segundos, com p95 de 344 ms. Isso é telemetria do ensaio
funcional em WSL ARM64, com rede isolada, quatro workers, PostgreSQL próprio e provedor
simulado. Não substitui o gate de capacidade/memória no hardware alvo, nem comprova uma
melhoria de desempenho em comparação com o laboratório original. Não foram acionados
provedores ou destinatários reais.

## Disposição de release

Nenhuma publicação, implantação, alteração de produção ou mensagem a clientes foi
executada. Os dois GitHubs ainda apontavam para o prerelease `16.0.20260904.3-rc1`,
criado antes destas correções.

A janela 2026-09-05 07:00–10:00 BRT foi cancelada pelo próprio critério do
[procedimento de implantação](../operations/production-cutover-rollback.md). A promoção
exige os commits finais dos dois repositórios, CI desse conjunto, evidência operacional
no ambiente alvo e nova janela registrada. O rate limit permanece adiado conforme
decisão do operador.
