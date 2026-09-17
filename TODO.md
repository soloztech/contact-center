# TODO — Contact Center

Pendências abertas deste repositório, com a evidência que as originou. Cada item sai
daqui quando for fechado por um commit ou por uma decisão registrada em `operations/`,
`reviews/` ou no incidente correspondente do ambiente. Registre item novo com sintoma,
evidência e ação sugerida; sem evidência, não é item.

Última revisão: 2026-09-17.

## Concorrência e resiliência

### 1. Erro de concorrência engolido vira cascata e alerta ao usuário

`models/application.py::_notify_ui` e `models/media.py::_finalize_provider_locator`
capturam `Exception` genérica e seguem trabalhando quando a falha é
`psycopg2.errors.SerializationFailure`. A transação já está abortada, todo comando
seguinte devolve "current transaction is aborted" e o queue_job notifica o usuário,
mesmo quando a retentativa conclui o trabalho sem perda.

Evidência: job `bdee51ad-e544-4056-a929-14dbfa7ef915` ("Contact Center media 12691"),
produção em 17/09/2026 19:40:34 UTC. O erro de origem foi
`could not serialize access due to concurrent update` num lock `FOR KEY SHARE` sobre
`contact_center_provider_connection`, durante a gravação dos campos de transcrição do
`contact_center_media_binding`. A retentativa às 19:48:37 concluiu e a mídia ficou com
`transcription_state = done`. O caminho saudável do mesmo tipo de conflito aparece às
19:02 e às 22:03 do mesmo dia: `OperationalError, postponed`, sem ruído.

Ação sugerida: reerguer os erros de concorrência nesses dois tratadores, deixando o
queue_job adiar o job, e manter a supressão apenas para falhas realmente acessórias.

### 2. Conflitos de serialização recorrentes no fluxo de mídia e transcrição

Em 17/09/2026 a produção registrou entre 1 e 15 conflitos por hora, quase todos
absorvidos por retentativa. Vale revisar o escopo de transação entre download de mídia,
enfileiramento de transcrição e escrita na conexão do provedor, para reduzir a disputa
na mesma linha de `contact_center_provider_connection`.

## Invariantes de conversa

### 3. Mudança de escopo da caixa ainda remove o responsável de conversa resolvida

`models/account.py::_contact_center_reconcile_channels` limpa
`contact_center_responsible_id` quando o agente sai do escopo da caixa, inclusive em
conversas já resolvidas. Isso reabre a brecha fechada em 17/09/2026 pela regra de que
conversa resolvida sempre tem responsável. Não foi alterado por falta de decisão sobre o
destino dessas conversas: manter o histórico, reabrir ou exigir transferência antes de
tirar o agente da caixa.

### 4. Conversa arquivada sem responsável

A regra pedida cobre apenas o estado `resolved`. A produção tem uma conversa arquivada
sem responsável. Decidir se o estado arquivado entra na mesma regra.

## Integração contínua da `16.0`

### 5. Teste do WuzAPI falhando

`contact_center_wuzapi/tests/test_webhook.py::TestWuzapiWebhook::test_unique_collision_retries_http_and_acknowledges_existing_inbox`
falha com `AssertionError: Lists differ: [True, True] != [True]`. Reproduzido fora do CI
no commit `4ca2d59` e no `fa0800f`, com HTTP habilitado: é anterior às entregas de 17/09
e deixa a `16.0` vermelha.

### 6. `pre-commit` vermelho em arquivos antigos

Prettier reformata `AGENTS.md`, `operations/ad-origin-preview-validation.md` e
`operations/audio-transcription.md`; `isort`, `flake8` e `pylint` reclamam de
`operations/ad_origin_preview_qa.py`,
`contact_center_base/models/transcription_provider.py`,
`contact_center_base/services/transcription.py` e `contact_center_meta/`. São pendências
das entregas de transcrição e de prévia de anúncio.

### 7. Suítes QUnit não rodam no CI

O repositório tem dez suítes em `contact_center_ui/static/tests/*.esm.js`, e nenhum
teste Python as executa por `browser_js`. O CI roda `oca_run_tests` e cobre só a camada
Python; as validações QUnit continuam manuais, registradas em `reviews/`.

## Higiene de repositório e ambiente

### 8. Atalhos técnicos estão na `16.0` e não estão em produção

Os atalhos técnicos de mensagem e canal (`view_technical_message` e
`view_technical_channel` em `models/ui_api.py`, mais JS, XML e teste de permissão) foram
integrados à `16.0` em 17/09/2026, junto com a reorganização da documentação, no merge
`80741cd`. A produção continua servindo a release `resolve-owner-fa0800f0e5e4`, anterior
a eles: publicar exige nova release, reinício dos dois serviços e autorização própria.
Validação até aqui: 760 testes Python de `contact_center_base` e `contact_center_ui` sem
falhas, com HTTP habilitado. As suítes QUnit continuam fora do CI, conforme o item 7.

### 9. Descoberta automática de addons no laboratório

O entrypoint do SERVIDOR05 varre `/mnt/outros` inclusive em diretórios ocultos. Uma
fonte antiga renomeada para `.algo` dentro dessa pasta ganha precedência alfabética e
passa a servir os módulos. Ao trocar a fonte do laboratório, mover a anterior para fora
do diretório de addons e usar o `--addons-path` fixado em
`/etc/odoo/website-canonical-addons-path.txt`.
