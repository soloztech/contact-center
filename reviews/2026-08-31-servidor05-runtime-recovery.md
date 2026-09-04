# Recuperação e estabilização do Odoo 16 no servidor05 — 2026-08-31

## Resultado

O laboratório `odoo16-teste.soloz.com.br` foi recuperado sem alteração da produção, da
base PostgreSQL ou do filestore. O runtime final usa dois workers, um canal raiz do
`queue_job`, limites de memória/PIDs e backend dedicado para websocket/longpolling.

## Causa do incidente

O container antigo operava com `workers=0`, fila `root:4`, logs em `debug` e sem limites
Docker de memória/PIDs. Após um restart sob tráfego público, o processo chegou a
aproximadamente 1.180 threads/PIDs e falhou com `MemoryError` e
`can't start new thread`.

Durante a estabilização surgiu um segundo problema independente: o worker gevent do Odoo
16 não iniciava. A imagem combinava `gevent==21.8.0` com `zope.interface==8.5` e
`zope.event==6.2`; o carregador legado do `pkg_resources` não resolvia os nomes de
distribuição PEP 420. Foram fixadas as versões compatíveis `zope.interface==5.5.2` e
`zope.event==4.5.0`.

## Runtime aplicado

- Odoo: 2 workers e 1 thread de cron.
- `queue_job`: `root:1`.
- PostgreSQL por processo: `db_maxconn=16`.
- Memória Odoo soft/hard: 512/768 MiB.
- Limite do container: 1,5 GiB e 256 PIDs.
- HTTP: servidor05 `8169 -> 8069`.
- Tempo real: servidor05 `8172 -> 8072`.
- Traefik: rotas separadas para HTTP e websocket/longpolling, healthcheck e limites de
  requisições em voo.
- Log: `info`.

O Dockerfile canônico remoto inclui os pins e um teste de `gevent.monkey.patch_all()`.
Como um rebuild completo excedeu o espaço livre, foi criada uma camada derivada mínima e
reproduzível sobre a imagem anterior. A imagem antiga permanece com a tag de rollback
`odoo16-local:pre-gevent-dependency-fix-20260831`.

## Proteções adicionadas ao controlador

- dry-run por padrão e token de confirmação explícito;
- locks exclusivos nos servidores04 e 05;
- isolamento e restauração fail-closed da rota exata de teste;
- reconhecimento do estado intermediário `runtime_recreate_recovery`;
- validação nominal de cada gate do runtime;
- prova do listener interno 8072 e do caminho servidor04 -> servidor05:8172;
- confirmação independente do hash/backup quando o canal SSH encerra após o marcador de
  commit;
- rollback automático de compose e `.env` em falhas anteriores ao commit.

## Evidência final

- Controlador: `status=already_hardened_and_validated`.
- HTTP público: 8/8 probes com status 200, entre 58 e 75 ms.
- Websocket público: HTTP 101 (`SWITCHING PROTOCOLS`).
- Docker: `running` e `healthy`, imagem corrigida, 11 PIDs.
- Fila: runner pronto; nenhum job novo em `failed` desde o restart.
- Contact Center: 7/7 conexões em `connected`, sem degradação, desconexão, autenticação
  pendente ou trava de identidade.
- Logs do runtime: zero `MemoryError`, `can't start new thread`, `CRITICAL`, worker
  morto ou `Traceback` desde o restart.
- Módulos: 4/4 módulos do Contact Center instalados e sem estado pendente; `queue_job`
  16.0.3.0.2 instalado.
- Produção: não tocada.

Diretório da evidência canônica:

`scans/raw/20260831-odoo16-lab-runtime-hardening/20260831T190951557083Z`

## Pendência operacional

O cache descartável da tentativa de rebuild (3,647 GiB) foi removido sem tocar em
imagens ativas, volumes, banco ou filestore. O servidor ficou com cerca de 4 GiB livres
(94% de uso). Isso é suficiente para o runtime, mas um novo rebuild completo requer
expansão do volume ou uma limpeza planejada de artefatos antigos.
