# Pente-fino greenfield — adapters WuzAPI/Meta e interface

- Data: 2026-09-03
- Escopo exclusivo: `contact_center_wuzapi`, `contact_center_meta` e `contact_center_ui`
- Fora do escopo: `contact_center_base`, CRM, Marketing Center, Integration Core,
  Website, scripts de infraestrutura e deploy
- Estado analisado: worktree compartilhado e concorrente; esta revisão preservou as
  alterações já existentes, inclusive o fence de onboarding WuzAPI por `identity_key`
- Resultado: **aprovado para a próxima integração no laboratório**, condicionado à
  execução das suítes Odoo/QUnit pelo coordenador do release

## Veredito

A separação arquitetural está correta e não há razão para criar outra camada ou dividir
arquivos apenas por tamanho:

1. `contact_center_wuzapi` contém somente configuração, onboarding, normalização e I/O
   próprios do transporte WuzAPI. O domínio recebe DTOs do core; não conhece o envelope
   WuzAPI.
2. `contact_center_meta` é um adaptador fino. App, credenciais, Graph runtime,
   assinatura, endpoint, subscriptions, delivery ledger e fan-out pertencem a
   `meta_api_base`/`meta_webhook_base`. O addon não mantém uma segunda infraestrutura
   Meta.
3. `contact_center_ui` consome apenas o contrato provider-neutral do Contact Center. Não
   há import, leitura de envelope ou desvio específico para WuzAPI/Meta nos assets da
   interface.

O pente-fino confirmou cinco melhorias concretas. Quatro afetam comportamento em runtime
e receberam testes de regressão; a quinta corrige uma promessa de segurança imprecisa na
documentação do journal do browser. Não foi encontrada justificativa para migration:
nenhuma coluna, constraint ou dado persistido mudou.

## Correções aplicadas

| ID         | Severidade | Achado confirmado                                                                                                                                                                                                         | Correção                                                                                                                                                                                                                                   | Regressão adicionada                                                                                 |
| ---------- | ---------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| GF-WUZ-01  |      Média | `json.loads()` aceita `NaN`/`Infinity` por extensão Python e uma árvore extremamente profunda pode levantar `RecursionError`. Na rota pública isso podia atravessar JSON não padrão ou produzir HTTP 500 antes do ledger. | `parse_constant` agora rejeita números não JSON; `ValueError` e `RecursionError` retornam `400 invalid_json`, antes de qualquer persistência/job.                                                                                          | Webhook assinado com `NaN` e payload com 12 mil níveis; ambos falham fechados e não criam inbox/job. |
| GF-WUZ-02  |      Média | O provedor controlava integralmente `Retry-After`; um `429` com valor enorme podia imobilizar o job muito além da política da fila.                                                                                       | Valor numérico e HTTP-date são limitados a 3.600 segundos.                                                                                                                                                                                 | `Retry-After: 7200` resulta em retry de 3.600 segundos.                                              |
| GF-META-01 |      Média | `TransientAdapterError` de download do avatar é subtipo de `AdapterError`; o `except AdapterError` amplo o transformava em avatar permanentemente indisponível.                                                           | Falhas transitórias são relançadas com o mesmo `retry_after_seconds`; apenas falhas permanentes projetam `unavailable`.                                                                                                                    | O teste prova identidade da exceção e preservação do retry de 30 segundos.                           |
| GF-UI-01   |      Média | Depois de preparar uma mídia, o draft mantinha o objeto `File`; em anexos grandes também mantinha o Blob URL, retendo o binário no heap mesmo quando o servidor já era o dono da referência imutável.                     | Após upload confirmado, limpa `File`, ID e metadata de upload. Previews acima de 8 MiB são revogados/limpos; previews pequenos permanecem para boa UX. As transformações foram extraídas para manter complexidade abaixo do limite ESLint. | Casos QUnit para vídeo de 50 MB (revoga/libera) e imagem de 2 MB (mantém preview, libera `File`).    |
| GF-UI-02   |      Baixa | O comentário do fingerprint do journal podia ser lido como garantia criptográfica, embora o algoritmo seja apenas um identificador determinístico de intenção.                                                            | Comentário passa a descrevê-lo como fingerprint opaco de idempotência e afirma expressamente que não é fronteira de confidencialidade.                                                                                                     | Não aplicável: correção documental, sem mudança de wire/storage.                                     |

### Arquivos diretamente alterados nesta revisão

- `contact_center_wuzapi/controllers/webhook.py`
- `contact_center_wuzapi/services/adapter.py`
- `contact_center_wuzapi/tests/test_webhook.py`
- `contact_center_wuzapi/tests/test_adapter.py`
- `contact_center_wuzapi/__manifest__.py`
- `contact_center_meta/services/profile.py`
- `contact_center_meta/tests/test_profile.py`
- `contact_center_meta/models/shared_webhook_consumer.py` (comentário de fronteira e
  supressão localizada de falso positivo do pylint)
- `contact_center_meta/__manifest__.py`
- `contact_center_ui/static/src/js/message_composer.esm.js`
- `contact_center_ui/static/src/js/contact_center_store.esm.js`
- `contact_center_ui/static/tests/contact_center_model_tests.esm.js`
- `contact_center_ui/__manifest__.py`

## Arquitetura e invariantes verificadas

### WuzAPI → DTO → core

- Contrato externo fixado em WuzAPI `v1.0.8`, commit `9487eca`; endpoints e formatos
  continuam compatíveis com essa revisão.
- O webhook público usa chave opaca indexada e HMAC SHA-256 sobre os bytes exatos, com
  `hmac.compare_digest` e revalidação depois do lock de admissão.
- Há limite duro de 1 MiB tanto com `Content-Length` quanto em body chunked; MIME, JSON,
  formato do envelope e profundidade são validados antes do ledger.
- Sanitização ocorre antes de `raw_envelope_json`; segredos, base64 e sidecars sensíveis
  não são projetados em DTO, bus ou UI.
- Dedupe no banco e savepoint tratam corridas; admissão, rotação de HMAC, mudança de
  primário e ativação de onboarding são linearizadas por locks/revision fences.
- O job de onboarding já corrigido procura o registro pela identidade estável, não por
  um recordset potencialmente obsoleto. Esta revisão não o alterou nem reverteu.
- Todas as chamadas HTTP têm timeouts explícitos, leitura de resposta limitada,
  `stream=True` e redirects automáticos desabilitados.
- A URL do serviço WuzAPI pode ser HTTP/RFC1918 deliberadamente: é um endpoint de
  infraestrutura definido por administrador na LAN, não uma URL recebida de usuário
  público. Forçá-la a HTTPS público quebraria instalações válidas sem eliminar uma
  fronteira SSRF real. URLs remotas de avatar, por outro lado, usam validação mais
  restrita e redirects desabilitados.
- Envio de áudio/imagem/documento/vídeo continua codificando uma vez para base64 no
  despacho. Isso é exigência do protocolo pinado; a implementação lê o `raw` do
  filestore para não criar o ciclo `datas` base64 → decode → encode.

Referência de protocolo:
[WuzAPI API.md no commit pinado](https://github.com/asternic/wuzapi/blob/9487eca/API.md).

### Meta compartilhado → Attribution/Message DTO → core

- Manifesto depende de `meta_api_base` e `meta_webhook_base`; não existem controller
  webhook, modelos App/Authorization/Delivery próprios, cron de health duplicado, queue
  channel próprio ou ACL paralela no addon.
- `test_architecture_contract.py` transforma essa decisão em contrato: falha se rotas,
  modelos, campos ou caminhos aposentados reaparecerem.
- A conexão guarda somente o binding ao asset compartilhado. Page/App, modo de
  transporte e destino são projeções readonly; coerência de empresa, plataforma e asset
  é validada.
- A autenticação/sanitização/limites do callback pertencem ao core compartilhado. O
  consumidor Contact Center só recebe delivery/item sanitizado e usa capability interna,
  freshness, subscription revision e route fence antes de criar seu inbox.
- O inbox mantém dedupe próprio e referência técnica ao ledger compartilhado, sem copiar
  segredo ou URL assinada para DTO/UI.
- URLs CDN assinadas ficam num vault privado sem ACL de usuário, acessível somente com
  token interno e correlação delivery/item/connection. São purgadas por retenção curta.
- Downloads exigem HTTPS, porta padrão, ausência de userinfo/fragment e hostname Meta
  allowlisted; cada redirect é revalidado, com máximo de três. Resposta é streamada com
  timeout, limite por tipo e validação de MIME.
- O outbound continua limitado às capabilities implementadas. A janela de 24 horas,
  destinatário, Page/IG asset e correlação do ID retornado são rechecados antes e depois
  do I/O conforme o caminho.
- O health de mensageria não usa `pages_read_engagement` como proxy de conexão.

Referências de protocolo:

- [Messenger Platform — Instagram Send API](https://developers.facebook.com/docs/messenger-platform/instagram/features/send-message)
- [Messenger Platform — Send Messages](https://developers.facebook.com/docs/messenger-platform/send-messages)

### UI operacional

- O bus é a fonte primária de invalidação; o ciclo de 30 segundos é reconciliação de
  consistência e também tenta recuperar o worker, não substituição silenciosa por
  polling.
- Listas e timeline usam cursor e limites explícitos. A timeline tenta fechar lacuna em
  até 20 páginas e faz reanchor honesto quando não pode provar continuidade.
- Respostas obsoletas são descartadas por request generation/channel fence; timers,
  listeners, audio, object URLs e uploads abortáveis têm cleanup.
- Drafts são deliberadamente por banco+usuário+conversa, limitados a 100 entradas e
  orçamento total. Corpo não é colocado no operation journal.
- O journal de operações mantém somente fingerprint/UUID, por banco+usuário, com máximo
  de 200 entradas e TTL de 24 horas. Ele recupera intenção ambígua sem prometer sigilo
  criptográfico.
- Não há `t-raw` em produção. URLs locais são validadas, conteúdo dinâmico usa
  escaping/QWeb e não há leitura direta de snapshot de provider.
- Upload, clipboard/drop, gravação de voz e troca de conversa possuem fences contra
  respostas tardias. A correção GF-UI-01 elimina a retenção desnecessária de blobs
  grandes depois da preparação.
- A confirmação destrutiva tem semântica de grupo, foco inicial, Escape e restauração do
  botão de origem. Um focus trap completo e i18n integral continuam acabamento, não
  falha de integridade.

## Refutações e decisões de não mudança

| Hipótese                                                          | Disposição                                                                                                                                                                                                                               |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contact_center_meta` duplicou a infraestrutura Meta              | **Refutada.** O addon estende os dois cores compartilhados e o contrato arquitetural proíbe a volta da infraestrutura local removida. O vault de URL de mídia é estado privado e específico do adaptador, não um segundo webhook ledger. |
| A UI depende de comportamento WuzAPI/Meta                         | **Refutada.** Não há token/import/provider snapshot desses addons em `contact_center_ui/static/src`; diferenças chegam por capabilities e DTOs do core.                                                                                  |
| O realtime é apenas polling                                       | **Refutada.** Bus é primário; o timer de 30 s é reparo limitado. Removê-lo pioraria recuperação após perda de notificação.                                                                                                               |
| A timeline sempre perde histórico ao atualizar                    | **Refutada.** Há merge por cursor, recuperação progressiva e reanchor apenas quando a continuidade não é demonstrável.                                                                                                                   |
| Toda URL HTTP privada é SSRF                                      | **Refutada para `wuzapi_base_url`.** É configuração administrativa de infraestrutura e redirects estão desligados. URLs originadas de payload/Meta seguem política bem mais restrita.                                                    |
| As migrations WuzAPI são “legado de runtime” e devem ser apagadas | **Refutada.** São a cadeia necessária para bancos de laboratório já atualizados. Não são chamadas por runtime normal. Meta, que foi reconstruído sobre os cores compartilhados, não mantém migration/compatibilidade aposentada.         |
| Arquivos grandes devem ser divididos por LOC                      | **Refutada como regra.** Black/flake8/pylint/ESLint passam após a extração motivada por complexidade real. Um split sem fronteira de domínio aumentaria acoplamento e risco de merge.                                                    |
| Devemos remover base64 do envio WuzAPI                            | **Refutada no protocolo atual.** A API pinada exige data URI/base64 para mídia. Trocar isso requer revalidar/alterar o provider, não uma refatoração interna.                                                                            |

## Riscos residuais conscientes

1. **Pico de memória no outbound WuzAPI:** mesmo com uma única codificação, um vídeo de
   50 MiB ainda exige `raw + base64 + JSON` no worker porque o protocolo pinado não
   oferece upload streaming. Antes de carga produtiva, medir RSS e definir teto/pool
   coerente; reduzir o limite é mitigação operacional possível.
2. **Rate limiting da rota pública:** o controller falha cedo em chave/HMAC/tamanho, mas
   limitação por IP/conexão deve ficar no reverse proxy/perímetro. Persistir contadores
   por requisição inválida no Odoo criaria um vetor de contenção no banco.
3. **Lista muito profunda na UI:** o refresh automático é limitado a 200 conversas e
   pode reancorar uma lista manualmente carregada além disso. É um trade-off explícito
   para evitar avalanche de RPC; observar no piloto antes de elevar o limite.
4. **i18n e acessibilidade:** existem rótulos pt-BR hardcoded e melhorias possíveis de
   focus trap/aria-live. Não afetam a integridade do transporte, mas devem entrar em um
   lote próprio antes de internacionalização.
5. **Runtime integrado:** esta sessão não instalou addons nem executou Odoo/QUnit,
   conforme o escopo sem deploy. A régua estática e o inventário passaram; o gate de
   release ainda deve executar as suítes no laboratório com o worktree congelado.

## Validação executada nesta revisão

| Verificação                                                |                                    Resultado |
| ---------------------------------------------------------- | -------------------------------------------: |
| Black 22.8 (`--check`)                                     |                    68 arquivos Python, limpo |
| isort 5.12 (`--check-only`, exclusão OCA de `__init__.py`) |                                        Limpo |
| flake8 + bugbear, complexidade máxima 16                   |                                        Limpo |
| `compileall`                                               | Limpo; caches gerados foram removidos depois |
| pylint-odoo opcional                                       |                                        Limpo |
| pylint-odoo obrigatório                                    |                                        Limpo |
| OCA module checks                                          |                        Limpo nos três addons |
| Prettier 2.7.1 + plugin XML (`--check`)                    |                                        Limpo |
| ESLint 8.24, complexidade máxima 15                        |                         Limpo, zero warnings |
| `node --check` em todos os JS                              |                                        Limpo |
| XML via `xml.etree.ElementTree`                            |                          19 arquivos válidos |
| `git diff --check` no escopo                               |                                        Limpo |
| Marcadores de conflito `<<<<<<<`/`>>>>>>>`                 |                                     Ausentes |
| Deploy/commit                                              |                            **Não realizado** |

### Inventário de testes (AST/declaração; não equivale a execução)

| Addon                   |           Arquivos de teste |     Casos declarados |
| ----------------------- | --------------------------: | -------------------: |
| `contact_center_wuzapi` |  7 Python, todos importados | 210 métodos `test_*` |
| `contact_center_meta`   | 12 Python, todos importados | 100 métodos `test_*` |
| `contact_center_ui`     |  1 asset QUnit no manifesto |     134 `QUnit.test` |
| **Total**               |                      **20** |              **444** |

As contagens são obtidas por AST para Python e por declaração QUnit para JS. Elas
demonstram descoberta/inventário, não sucesso em runtime; não são apresentadas como “444
testes passaram”.

## Versões finais do worktree

| Addon                   |        Versão | Migration deste lote |
| ----------------------- | ------------: | -------------------: |
| `contact_center_wuzapi` | `16.0.1.29.1` |                  Não |
| `contact_center_meta`   |  `16.0.2.1.1` |                  Não |
| `contact_center_ui`     | `16.0.1.26.1` |                  Não |

## Gate recomendado ao coordenador

1. Congelar/identificar o worktree compartilhado para não misturar nova alteração
   durante a execução.
2. Rodar as suítes Odoo do base, WuzAPI, Meta e integração sem reutilizar resultado de
   release anterior.
3. Rodar o asset QUnit atualizado (134 casos) e guardar log/artefato.
4. Fazer smoke autenticado mínimo: webhook Wuz válido/inválido, mensagem Meta, upload
   grande/pequeno, alternância de conversa e recuperação por bus.
5. Só então consolidar pins/deploy. Nenhuma pendência encontrada nesta revisão exige
   outro remendo estrutural antes desse gate.
