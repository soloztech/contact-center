# Resposta à verificação da disposição — N1–N7 / M2 / M4

- Data: 2026-08-29
- Entrada: `reviews/2026-08-29-service-split-disposition-verification.md`
- Base analisada: commit `7698dea`, tag `16.0.1.24.4-lab`
- Escopo operacional: código local e laboratório descartável SERVIDOR05; produção fora
  do escopo.

## Veredito

A verificação encontrou lacunas reais em **R1, R2, R4, R5, R6 e R7**; todas foram
aceitas e corrigidas. **R3 foi aceita somente quanto à falta de limite e de
observabilidade do evento**, mas as propostas de remover o reset de tentativas do
profile e de matar a espera apenas por idade foram rejeitadas por misturarem dois ciclos
de vida diferentes.

## Correções aceitas

| Item | Disposição          | Implementação                                                                                                                                                                                                                       |
| ---- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1   | Aceito              | 429 no health Meta usa o jitter/cooldown compartilhado e `ignore_retry=True`; a autorização válida e o orçamento de retries anteriores são preservados.                                                                             |
| R2   | Aceito              | O recovery encerra como `unsupported`, sob lock e em lote limitado, waiters cujo profile, binding, canal, conexão ou conta deixaram de compor um escopo válido. Uma conexão ativa apenas pausada/desconectada continua recuperável. |
| R3   | Parcialmente aceito | Cada nova decisão que ainda exige roster incrementa métricas duráveis e o evento converge para `dead` ao alcançar o teto de 12 tentativas. O primeiro instante e a contagem ficam visíveis ao administrador.                        |
| R4   | Aceito              | Ausência temporária do participante próprio continua transitória; DTO persistido inválido, papel inválido ou alias não único permanecem `ValidationError` permanente.                                                               |
| R5   | Aceito              | A liberação de waiters roda em savepoint best-effort; sua falha não rebaixa um snapshot de roster válido. O cron é o fallback durável.                                                                                              |
| R6   | Aceito              | 429 na configuração do webhook WuzAPI agora é `ProviderRateLimitError`; o consumidor respeita `Retry-After`, aplica jitter e não consome o teto. 5xx continua transitório e limitado.                                               |
| R7   | Aceito              | Somente rejeições dos slots `attachments`/`attachment:*` justificam tratar a lista vazia como mídia quarentenada; rejeição de `quick_reply` não contamina essa decisão.                                                             |

Também foram acrescentados testes para `extra_where`, coalescing `skip_if_active`,
múltiplos waiters, isolamento entre profiles, escopos arquivados, provider desconectado,
teto da espera, integridade do participante próprio, falha da liberação e preservação do
contador do `queue_job` em 429.

## Pontos refutados ou qualificados

### 1. `health_pending` durante 429 crônico

Não deve virar erro apenas pelo número de respostas 429. Rate limit informa capacidade
temporária, não credencial inválida. O mesmo job cercado por revisão permanece pendente,
com UUID e estado observáveis, enquanto `ignore_retry=True` impede que throttling gaste
o orçamento reservado a falhas transitórias reais. Um alerta por idade pode ser incluído
como observabilidade operacional, mas não como transição automática para erro.

### 2. Reset de `group.profile.attempts` em `force=True`

Não foi removido. Esse contador pertence à **revisão do pull de metadata** e deve zerar
quando `sync_revision` avança; carregá-lo entre revisões faria um pull novo herdar
falhas de um request já supersedido. O orçamento da mutation aguardando roster pertence
ao inbox e agora é limitado independentemente por `inbox.attempts`, sem ser zerado pela
liberação do waiter.

### 3. TTL temporal que mata waiters

Não foi implementado. Tempo decorrido sozinho não prova impossibilidade: uma caixa pode
ficar desconectada ou pausada por horas e depois recuperar o roster correto. O desenho
agora distingue:

- escopo estruturalmente impossível: `unsupported`;
- ciclos de processamento sem decisão: `dead` no teto de 12;
- provider ativo, porém temporariamente indisponível: espera durável e visível.

Isso evita descartar uma mutation válida apenas por uma indisponibilidade externa.

### 4. Ator que apaga e depois sai do grupo

A afirmação precisa distinguir autoria de privilégio. Delete feito pelo próprio autor
continua autorizado pela correlação imutável entre ator e mensagem-alvo, mesmo que ele
tenha saído depois. Delete administrativo depende do papel atual confirmado em roster
posterior ao evento. Se o ator já saiu, não existe prova histórica de que era admin no
instante do comando; fabricar essa autorização ou criar uma lápide seria inseguro. O
fail-closed sem projeção local é, portanto, deliberado.

### 5. Latência do coalescing

O valor de “até ~10 minutos” não é um limite garantido. O `retry_pattern` do job de
metadata chega a **3.600 segundos**, além do tempo de fila. `skip_if_active` evita jobs
duplicados; a liberação após snapshot e o cron de recovery garantem retomada, mas a
telemetria deve assumir que uma falha já em backoff pode adicionar aproximadamente uma
hora.

### 6. Evidência histórica exata do laboratório

Os artefatos canônicos comprovam a release e as suítes, mas o retrato pontual citado
para 22:58 (`7 conexões`, variação de `dead`) não possui artefato independente
suficiente para reprodução posterior. Ele não foi usado como premissa das correções.

## Validação

- `git diff --check`, `py_compile`, Black 22.8, isort, flake8 C901≤16 e parse XML:
  aprovados;
- release atômico e upgrade no SERVIDOR05: `applied_and_validated`, HTTP 200 e rota
  Traefik restaurada byte a byte;
- suíte base: **312/312**, zero falha/erro;
- suíte integrada: **588/588**, zero falha/erro;
- bancos temporários removidos e estado instalado revalidado;
- versões: base `16.0.1.24.5`, WuzAPI `16.0.1.19.2`, Meta `16.0.1.6.2`, UI `16.0.1.17.4`
  e OCA queue_job `16.0.3.0.2`;
- árvore funcional final:
  `23bb787a9d6fbd8b408633dd04ad7a67819e2a0865f87319fb2b44d0e37749c7`;
- evidências: `scans/raw/20260829-odoo16-contact-center-disposition-followup/`.

A primeira execução da nova sentinela do `extra_where` expôs uma lacuna adicional:
campos de prontidão escritos pelo ORM ainda podiam não estar descarregados quando o
recovery os consultava por SQL no mesmo worker. O recovery agora faz flush explícito do
profile antes da query e invalida o cache antes da segunda guarda ORM. As suítes finais
foram repetidas integralmente depois dessa correção. Nenhuma ação foi executada no
SERVIDOR02.
