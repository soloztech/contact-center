# Contact Center release readiness — 2026-08-28

## Resultado

A árvore consolidada foi implantada e homologada somente no laboratório descartável
SERVIDOR05. Produção não foi acessada. O banco de teste não recebeu backup, conforme a
orientação operacional vigente.

Versões do release:

- OCA `queue_job 16.0.3.0.2`;
- `contact_center_base 16.0.1.22.5`;
- `contact_center_wuzapi 16.0.1.17.1`;
- `contact_center_meta 16.0.1.5.3`;
- `contact_center_ui 16.0.1.17.3`.

O release atômico isolou apenas `odoo16-teste.soloz.com.br`, realizou o upgrade offline,
restaurou a rota Traefik byte a byte e terminou com HTTP 200, container `running`, zero
operação de módulo pendente e aproximadamente 2,3 GB livres.

## Validação automatizada

- suíte base: **288/288**, zero falha/erro;
- suíte integrada: **540/540**, zero falha/erro;
- bancos temporários removidos;
- QUnit UI minificado e `debug=assets`: **76/76**, **630/630** asserções;
- QUnit viewer minificado e `debug=assets`: **4/4**, **15/15** asserções;
- Black, isort, Flake8 com complexidade 16, pylint obrigatório, OCA checks, XML,
  JavaScript, ESLint, Prettier e `git diff --check` sem pendência.

Evidência das suítes:
`scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260828T200905903282Z`.

Evidência da validação do estado instalado:
`scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260828T201208650027Z`.

Evidência da release atômica funcional:
`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260828T200813883329Z`.

A árvore operacional final contém 212 arquivos e hash
`5394634c9e9859729ffb55cd722d8488e3cfc2ec9642b1c57d2d444765d3a65d`.

Uma execução anterior ao source switch ainda usava a árvore antiga do servidor e foi
descartada como evidência. Na primeira execução contra o candidato, dois testes novos
usavam `assertRaises` do Odoo, cujo savepoint desfez a persistência simulada pelo
`flush`; a captura foi trocada por `try/except`, como no teste legado da mesma
fronteira. Não houve alteração no fluxo de produção por esse diagnóstico. A repetição
acima, já contra o hash final, passou integralmente.

## WuzAPI

As cinco conexões estavam `connected/healthy`, sem identity mismatch, cooldown ou nova
falha pós-release. O read-back confirmou URL correta e 16/16 eventos em todas as caixas.
Um canário real na instância Lucas provou webhook assinado, replay idempotente, guest e
aliases canônicos, outbox concluída em uma tentativa, correlação externa e sessão ainda
conectada. Mídia nova observada após o release ficou íntegra e `ready`.

Não há blocker de código WuzAPI conhecido. O rollout para aproximadamente vinte caixas
ainda deve medir JobRunner/disco e ensaiar queda/reconexão e timeout ambíguo com
resolução administrativa; são gates operacionais da topologia produtiva.

O release adiciona pacing compartilhado e durável por conexão. WuzAPI reserva um
intervalo mínimo de um segundo antes de cada fronteira externa; um 429 compartilha seu
`Retry-After` com as mensagens irmãs e usa fallback de 60 segundos quando o header não é
válido. O comando barrado permanece `pending`, sem consumir tentativa ou persistir
snapshot. Um teste com dois cursores independentes comprovou que apenas um job atravessa
o adapter e que nenhum lock permanece durante a chamada HTTP. O inventário read-only
pós-release confirmou as cinco sessões conectadas e sem alteração planejada.

## Meta 6.4 e 6.5

O outbound direto de texto está implementado pela outbox canônica, com janela de 24
horas, `appsecret_proof`, destino escopado, retry 429/613 compartilhado por conexão com
fallback de 60 segundos sem header e timeout mutante terminal em `uncertain`. Health e
subscriptions usam jobs OCA revisionados, read-back e gates fail-closed. Um teste
dedicado prova que a falha de uma autorização/Page não bloqueia a outra.

O cliente `debug_token` usa o GET documentado. Falhas de health persistem apenas uma
classe técnica e estágio allow-listed. O probe real chegou até `page_identity` e
confirmou que o token atual não possui `pages_read_engagement`; nenhum token, URL de
request ou payload foi persistido na evidência.

Meta ainda não está liberada para produção. Restam ações externas: reautorizar o scope,
validar novamente Page tasks e Instagram vinculado, usar credencial gerenciada por
System User, colocar o App em Live com aprovações aplicáveis e testar um remetente sem
App role. Até isso ocorrer, as duas conexões devem permanecer `degraded` e com outbound
desligado.

## Interface

O navegador autenticado exibiu 473 conversas, as sete caixas configuradas, realtime
ativo, WuzAPI 5/5 conectada e as duas caixas Meta visíveis e fail-closed. O console
terminou com zero erro e zero warning.
