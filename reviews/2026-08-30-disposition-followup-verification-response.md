# Resposta ao follow-up da disposição — D1–D5 / R3

- Data: 2026-08-30
- Entrada: `reviews/2026-08-30-disposition-followup-verification.md`
- Base analisada: commit `3c70ba9`, tag `16.0.1.24.5-lab`
- Escopo operacional: código local e laboratório descartável SERVIDOR05; produção fora
  do escopo.

## Veredito

**D1 e D2 procedem**, com qualificações importantes sobre severidade e classificação. A
lacuna de cobertura de R3 também procede e foi fechada. **D3 descreve uma janela real,
mas a segunda leitura ORM sugerida não a corrige no isolamento do Odoo; D4 não procede
no fluxo canônico; D5 é telemetria já separada por ciclo de vida, não um defeito de
orçamento.**

## Correções aceitas

### D1 — profile `failed` e waiter sem convergência

O relatório está correto quanto à falta de convergência finita sob falha recorrente, mas
`failed` não ficava literalmente sem qualquer retry: o cron do profile agenda uma nova
revisão após `next_sync_at` (24 h). Ainda assim, o contador do inbox permanecia
congelado entre essas revisões e o evento podia ficar `pending` indefinidamente.

A correção não adiciona `failed` ao predicado que produz `unsupported`. A classificação
agora é:

- escopo/capability inexistente ou arquivado: `unsupported`;
- leitura temporariamente pausada, desconectada ou rate-limited: `pending`;
- DTO/adapter/validação permanente ou teto do pull de metadata: `dead`.

O profile continua administrativamente retryable e mantém seu agendamento de saúde. O
inbox morto pode ser requeued depois da correção, sem ficar invisivelmente pendurado.
Antes de limpar a dependência, o ledger congela evidência sanitizada com profile, classe
do erro, revisão, tentativa e instante; não copia mensagem do provider nem payload.

O fallback do cron não decide sobre um snapshot obsoleto: ele trava primeiro
`mail.channel`, depois `group.profile`, e somente então o inbox. Essa é a mesma ordem da
recuperação do profile. Uma recuperação concorrente é pulada ou produz retry de
serialização, nunca um `dead` baseado em estado antigo.

### D2 — flush anterior à criação do savepoint

Confirmado diretamente no OCB 16: `_FlushingSavepoint.__init__()` chama `cr.flush()`
antes de emitir o `SAVEPOINT`. O risco prático era baixo porque o snapshot anterior já
tinha sido descarregado, mas capturar a construção inteira podia engolir uma falha de
entrada e deixar o cursor abortado.

Os blocos de release e finalização best-effort agora abrem o savepoint fora do `try`
interno. Apenas o corpo opcional é capturado; em falha ele executa `rollback()`
explícito, que limpa o ambiente ORM e volta ao savepoint. Falhas ao abrir, descarregar
na saída ou fechar a fronteira propagam. Um teste executa escrita parcial seguida de
`SELECT 1 / 0`, confirma o rollback da escrita e executa outro SQL no mesmo cursor.

### R3 — contadores e ciclo real

O teto em `attempts` permanece correto. Ele é o orçamento total do inbox e, sem requeue
administrativo, satisfaz `attempts >= group_roster_wait_count`; usar o contador
específico como segundo teto seria redundante. Foram adicionadas provas de:

- ciclo `defer -> release -> defer` acumulando `attempts` e `wait_count`;
- preservação de `first_group_roster_wait_at`;
- release sem zerar tentativas;
- erro `UnsupportedEventError` antigo limpo antes de gravar um waiter novo;
- estados `paused` e `disconnected` permanecendo recuperáveis.

## Pontos refutados ou qualificados

### D3 — segunda guarda ORM após o lock do ledger

A janela descrita existe, mas a correção sugerida não entrega a garantia afirmada. O
Odoo usa PostgreSQL `REPEATABLE READ`: reler por ORM na mesma transação continua vendo o
mesmo snapshot. Travar profile/binding depois de já travar o ledger também inverteria a
ordem usada pelo sync (`scope -> profile -> ledger`) e criaria risco de deadlock.

Não foi adicionada uma guarda cosmética. Para escopos estruturais, arquivar/desativar é
uma decisão terminal válida no instante observado; uma reativação posterior pode usar o
requeue administrativo. Uma garantia estrita contra reativação simultânea exigiria a
refatoração em duas fases com todos os locks canônicos antes do ledger. A nova lane
`profile.failed`, onde recuperação concorrente é operação normal, já usa exatamente essa
ordem forte.

### D4 — `UnsupportedEventError` pegajoso

Não procede. `_request_sync()` grava a nova revisão com `last_error_class=False` e
`last_error_message=False`. `_defer_for_group_roster()` chama esse método antes de
persistir o waiter, na mesma transação. Antes do commit o cron não vê o waiter; depois
do commit já vê o erro limpo. O teste de ciclo parte explicitamente de um erro
Unsupported antigo e confirma que o terminalizador estrutural não mata o waiter novo.

### D5 — reset do orçamento do profile

O reset é deliberado: `profile.attempts` pertence a uma revisão específica do pull e
deve zerar quando `sync_revision` avança. O orçamento do evento é `inbox.attempts`; a
telemetria específica é `group_roster_wait_count`, `first_group_roster_wait_at` e a
revisão registrada no metadata. Acumular tentativas do profile entre revisões faria um
request novo herdar falhas de um request supersedido.

## Validação

- `git diff --check`, `py_compile`, Black 22.8, isort nos arquivos alterados, flake8
  C901<=16 e checks OCA: aprovados;
- release/upgrade e deploy final no SERVIDOR05: HTTP 200, módulos instalados sem estado
  pendente e rota de teste preservada/restaurada;
- suíte base: **314/314**, zero falha/erro;
- suíte integrada: **590/590**, zero falha/erro;
- bancos temporários removidos e estado instalado revalidado;
- versões: base `16.0.1.24.6`, WuzAPI `16.0.1.19.2`, Meta `16.0.1.6.2`, UI `16.0.1.17.4`
  e OCA queue_job `16.0.3.0.2`;
- árvore funcional final:
  `8048beb8044ec74c973c46bf6840e1143718bbeb60b000068c60b970bc170e8f`;
- evidências: `scans/raw/20260830-odoo16-contact-center-disposition-followup-2/`,
  especialmente `source-deploy-final`, `tests-final-2` e `validate-final`.

Nenhuma ação foi executada no SERVIDOR02.
