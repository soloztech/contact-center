# Auditoria — Fases 2.1 (papéis/escopo) e 3 (mídia, reply, reaction, edit, delete)

- Data: 2026-08-22 (madrugada)
- Revisor: Claude (auditor), com 6 leitores independentes por frente + 1 refutador por
  achado de severidade alta/média (23 de 24 refutados/confirmados; 1 não verificado)
- Versões no lab SERVIDOR05: base `16.0.1.5.0`, wuzapi `16.0.1.3.0`, ui `16.0.1.4.1`,
  queue_job `16.0.3.0.2`; WuzAPI `v1.0.8` com `--skipmedia=true`
- Evidência: incident
  `2026-08-21-odoo16-contact-center-phase2-permissions-phase3-media.md`,
  `scans/raw/…phase2-permissions-phase3-media/` (52/52 base, 89/89 integrado, 3
  rodadas), leitura JSON-RPC do lab, fonte do Odoo 16 e do OCA `queue_job`.

## Veredito

**Fase 2.1 e Fase 3 entregues, arquitetura íntegra, sem bug de perda de dados no caminho
feliz.** A fronteira "binário nunca entra em DTO/inbox/outbox/bus/log" está implementada
(sanitizer antes de persistir, locator admin-only, `ir.attachment` privado em
`mail.message`, rota de conteúdo com ACL por membership e Range 206/416). Permissões: 27
regras, todas por grupo, nenhuma global sobre `mail.channel`/`mail.message`; nenhum
caminho de API deixa um agente da equipe A alcançar dados da equipe B. Mutações:
idempotência em três camadas, tombstone real, corpo original preservado, projeção só
após sucesso do provider.

**Não está pronto para piloto com mídia** por 4 defeitos médios que se somam no dia a
dia (download morre em ~70 s sem recuperação na UI; receipts/mutações órfãos morrem em
~2 min; conexão não se recupera sozinha; `text/html` servido inline sem CSP) e pelas
pendências de processo que persistem desde a Fase 1 (git, incident da Fase 1, PII real
no lab).

## Achados confirmados (severidade após refutação)

### Médios — corrigir antes do piloto

| #   | Achado                                                                                                                                                                                                                                                                                                                                                                        | Onde                                                              | Correção                                                                                                                                                         |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| M1  | **Download de mídia inbound morre em ~70 s**: `RetryableJobError(seconds=10)` fixo em `media.py:379-389` ignora o `retry_pattern` exponencial do `queue_job` (só usado quando `seconds` é falsy); teto `attempt >= 8`. Uma reinicialização de 2 min da WuzAPI perde a foto.                                                                                                   | `models/media.py:376-389`                                         | Passar `seconds=None` (ou `retry_after` do provider) para o pattern valer; subir o teto.                                                                         |
| M2  | **Mídia `failed` não tem recuperação**: `action_retry_download` existe mas não há view, botão nem menu para `contact.center.media.binding`.                                                                                                                                                                                                                                   | `models/media.py:272`                                             | View/lista para supervisor com "Retry download" + cron que reenfileira `failed` recentes.                                                                        |
| M3  | **Receipts e mutações órfãos morrem em ~110 s**: `retry_after_seconds = 10` fixo (`application.py:396-399` e `439-443`) × 12 tentativas. Lab: 8 `dead`. Na Fase 3 isso passou a atingir reaction/edit/delete que chegam antes da mensagem.                                                                                                                                    | `models/application.py:398`                                       | Backoff escalonado (deixar o pattern agir) e estado terminal próprio (`orphan`/`blocked`) quando o alvo é conhecido como `unsupported`.                          |
| M4  | **Conexão não se recupera sozinha** (gap aberto desde a Fase 1): `state` só muda pelo botão `action_check_health`; `Connected/Disconnected/LoggedOut` → `unsupported`; o ramo `connection.updated` em `application.py:97-101` é código morto. Sessão cai silenciosamente → `state` continua `connected` → envios entram numa sessão morta.                                    | `models/account.py:681`; `wuzapi/services/adapter.py:681-687`     | Cron de health por conexão ativa + normalizar eventos de sessão da WuzAPI em `connection.updated` + requeue dos comandos `pending` ao voltar a `connected`.      |
| M5  | **Documento `text/html` servido inline sem CSP = XSS armazenado**: `_MIME_BY_KIND["document"] = None` (sem allow-list), sem sniffing no inbound, rota monta a resposta à mão sem o `default-src 'none'` que o Odoo usa em `/web/content`. Atenuante: a UI abre documentos com `?download=1`; o risco é o agente colar a URL sem o parâmetro.                                  | `controllers/main.py:243-256`; `wuzapi/services/adapter.py:63-68` | Forçar `attachment` fora de image/audio/video; `Content-Security-Policy: default-src 'none'; sandbox`; sniffing (`guess_mimetype`) como já faz a rota de upload. |
| M6  | **`mediaKey` sem restrição em `inbox.event`**: `raw_envelope_json`/`normalized_dto_json` não têm `groups=`, enquanto `media.binding.remote_locator_json` é admin-only. Um supervisor com acesso ao ledger da conta pode ler a chave de descriptografia de mídia de conversas das quais não é membro (precisa também de acesso ao CDN; exposição real é menor que a descrita). | `models/queue.py:72-74`                                           | `groups=…group_contact_center_admin` nos dois campos JSON (ou remover `mediaKey` do envelope persistido após criar o `media.binding`).                           |
| M7  | **Mutação sem guarda de ordem**: `_apply_projection` só checa o próprio `state`; edit E1 (18:12:01) processado depois de E2 (18:12:04) reverte o corpo para o texto antigo; reaction idem.                                                                                                                                                                                    | `models/mutation.py:122-181`                                      | Monotonicidade por `occurred_at` (como `_contact_center_apply_delivery`); marcar superadas como `applied`.                                                       |
| M8  | **Eco `from_me` de reaction resolve ator errado**: usa o autor da mensagem-alvo (`target.message_id.author_id or technical_author`), mas o outbound da UI usa o partner do agente → reação duplicada e substituição quebrada quando o operador reage pelo celular.                                                                                                            | `models/application.py:468-471`                                   | Ator do lado "próprio" sempre = autor técnico da conta (ou mapear para o partner do agente quando o alvo for outbound dele).                                     |
| M9  | **Admin sem `ir.rule` em inbox/outbox**: só há regra para supervisor (escopo de equipe); admin herda supervisor, mas um admin fora do roster da equipe não vê os ledgers, e `action_requeue` (admin-only) fica inalcançável.                                                                                                                                                  | `security/contact_center_security.xml:229-247`                    | Regras `*_admin_company` por empresa, como já existe para team/account/media/mutation.                                                                           |
| M10 | **Envio de mídia materializa ~4-5 cópias em RAM** (`datas` base64 → bytes → base64 → str → JSON): pico ≈ 450 MB para o teto de 50 MB, no worker do `queue_job`.                                                                                                                                                                                                               | `wuzapi/services/adapter.py:931-940, 1037-1038`                   | Ler `raw`/`_full_path` em vez de `datas`; encodar uma vez; reduzir teto de vídeo/documento até haver streaming.                                                  |
| M11 | **Sincronização em tempo real trunca a timeline em 100**: `scheduleSynchronization` refaz `get_timeline` (clamp 100) com `reset: true`; agente que carregou 150 mensagens perde o histórico e o scroll ao chegar qualquer evento de bus.                                                                                                                                      | `ui/static/src/js/contact_center_store.esm.js:1000-1006`          | Merge incremental em vez de reset; manter `loadedFloorMessageId`.                                                                                                |

### Baixos (confirmados; 20+ itens, os mais relevantes)

- Sanitizer não remove `streamingSidecar`/`scansSidecar` (base64 de ~12-17 KB por vídeo)
  e `data:` URI sem `;base64` — deny-list; converter em allow-list de chaves do locator
  (`webhook.py:44-72`).
- Rota de conteúdo decodifica o anexo inteiro em memória a cada Range request
  (`main.py:213`) — usar `ir.binary`/`Stream.from_attachment`.
- Edit recebido após delete → `ValidationError` → `dead`, em vez de no-op
  (`mutation.py:153-155`).
- Lookup de alvo de mutação inbound ignora `client_message_id` (só
  `external_message_id`), diferente do caminho de receipts (`application.py:427-437`).
- Reações nativas do Discuss em mensagens CC não são bloqueadas (`_message_add_reaction`
  não passa por `write`); cria reação local sem ledger/outbox e emite
  `mail.message/insert`. O próprio teste do módulo assume esse comportamento
  (`test_contact_center.py:661-665`) — decidir e documentar.
- Preview da lista mostra o token `audio`/`image`/`text` em vez de rótulo
  (`conversation_list.xml:129`).
- Sem i18n na UI: pt-BR fixo sobrescreve rótulos traduzidos do servidor (`_("Open")`
  vira código morto) — `contact_center_model.esm.js:132`.
- Capability `create_contact` computada de `res.partner.check_access_rights('create')`,
  que o backend contorna com sudo → agente comum nunca vê "Criar contato"
  (`application.py:1831`) — **não verificado adversarialmente**.
- Tombstone mantém descritores de mídia e URL de conteúdo funcionando
  (`application.py:1530`).
- Upload: `client_upload_id` global único × dedupe por usuário+conversa → retry vira
  HTTP 500; cron de limpeza pode apagar anexo que um envio concorrente acabou de
  consumir; caption sem limite no caminho de mídia; PTT (nota de voz) inalcançável e sem
  transcodificação.
- Transação aberta por até 11 min por tentativa de download (timeout 660 s).
- Timeline força scroll ao fundo a cada mensagem nova; `aria-live` na lista e na
  timeline inteiras; upload não cancela de fato.
- Migração 1.4.0 reescreve só 12 das 27 regras `noupdate`; migração wuzapi 1.3.0 pula
  conexões arquivadas.
- Zero testes para o job de download, a máquina de estados de mídia, a rota de conteúdo,
  a rota de upload e o cron; QUnit sem cobertura de componente/DOM.

### Refutados (não são defeitos)

- "Teto de download usa o máximo por tipo em vez do `fileLength`" — confirmado no
  código, mas o tamanho e o hash são validados logo depois; impacto desprezível.
- "Nada verifica `--skipmedia=true`" — **falso no nível de infra**:
  `infra/scripts/wuzapi_servidor04_compose_deploy.py` exige exatamente uma ocorrência da
  flag (linhas 305-308, 355-356, 414, 571, 622) e
  `infra/deployments/servidor04/wuzapi/docker-compose.yml:51` a fixa. Corrige o gap (h)
  apontado na verificação anterior; falta só o `phase1_configure.py` refletir isso.

## O que foi confirmado correto (amostra)

- Sanitização antes de persistir; hash de dedupe sobre os bytes crus; `RawMessage`
  removido; `_validate_json_locator` rejeita `data:` e strings > 8 KB.
- Locator admin-only no ledger de mídia; `mediaKey` nunca chega ao UiDTO, bus ou log.
- Download com `stream=True`, `allow_redirects=False`, teto incremental, SHA-256 e
  `fileLength` validados em três lugares; job reentrante e idempotente.
- Anexo privado (`public=False`, sem `access_token`) em `res_model='mail.message'` — por
  isso o lab mostra 0 anexos em `mail.channel`.
- Upload: `auth='user'`, CSRF, teto lido dos bytes reais, MIME sniffado, dono e conversa
  checados duas vezes, consumo atômico com mensagem/binding/outbox sob `FOR UPDATE`, 1
  anexo por mensagem em três camadas, eco `from_me` não duplica mídia, retry reusa o
  mesmo `Id` e revalida hash.
- 27 `ir.rule`, todas por grupo; portal/share excluído em todo caminho; roster com lock
  e limpeza de responsável inválido; `search_partners`/`link_partner` não cruzam
  empresa; `create_and_link_partner` com allow-list.
- UI sem import privado do Discuss, sem `t-raw`, `safeLocalUrl` para `href/src`, menu de
  ações navegável por teclado, confirmação de delete, contratos de bus coerentes.
- Guest preservado após promoção agora tem teste; contagens de testes batem com o código
  (52/89/20).

## Status dos gaps anteriores

| Gap                               | Status                                                                             |
| --------------------------------- | ---------------------------------------------------------------------------------- |
| (a) reconexão automática          | **Aberto** (M4)                                                                    |
| (b) receipts órfãos → dead        | **Aberto e ampliado** para mutações (M3)                                           |
| (c) throttle/jitter por conexão   | Aberto                                                                             |
| (d) raw payload integral          | **Corrigido para binários**; PII textual continua (288 eventos de grupo com texto) |
| (e) inbox sem `FOR UPDATE`        | Aberto (mitigado por unicidade + teste de convergência)                            |
| (f) teto de tentativas hard-coded | Aberto (`12` em 4 pontos, `8` na mídia)                                            |
| (g) testes faltantes              | Guest pós-promoção **fechado**; bus capture ainda aberto                           |
| (h) `--skipmedia` não verificado  | **Corrigido** — gate no deploy da WuzAPI (SERVIDOR04)                              |
| (i) incident da Fase 1            | Aberto                                                                             |
| (j) código sem git                | **Aberto** — 14,8k linhas Python untracked; sem ponto de rollback                  |

## Pendências de processo

1. **Git**: criar o repo `soloztech/contact-center` (branch `16.0`) e commitar antes de
   qualquer outra iteração — hoje não há diff, review nem rollback de código.
2. **Incident da Fase 1** continua ausente.
3. **PII real no lab** cresceu (288 eventos de grupo/broadcast com texto e JIDs de
   terceiros, 3 identidades reais, mídia); o upgrade foi feito sem backup por decisão —
   ok para lab, mas o dump/expurgo antes de qualquer cópia precisa virar regra.
4. **QUnit** (20 testes/97 asserções) e o **smoke real de mídia** não têm arquivo de
   evidência em `scans/raw` — só texto no incident. O outbox do lab (6 send, 2 delete, 1
   edit, 1 react, todos `done`) corrobora o smoke.
5. **Mídia inbound real** ainda não foi exercitada (só fixtures) — fazer um envio real
   de foto/áudio/documento antes do piloto, observando M1/M2.

## Ordem sugerida

1. Git + incident F1 (meio dia).
2. M1+M2+M3 (backoff e recuperação de órfãos/falhas) — mesma raiz: `seconds` fixo
   ignorando o `retry_pattern`.
3. M4 (cron de health + eventos de sessão + requeue).
4. M5+M6 (CSP/disposition/sniffing; `groups=` nos JSON do inbox).
5. M7+M8 (ordem das mutações; ator do eco).
6. M9, M10, M11 e os baixos de UI.
