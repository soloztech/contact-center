# Prévia do anúncio de origem na conversa

Data: 2026-09-12. Estado: estudo de implementação; nenhuma funcionalidade de anúncio foi
implementada ou ativada.

O resultado desejado é mostrar, junto à mensagem inicial, o título do anúncio, um trecho
do texto, um link público para a origem e uma miniatura. A frase enviada pelo contato —
por exemplo, “Hello Can I get more info” — continua sendo a mensagem. “Solicite um
orçamento gratuito!” seria o título do anúncio, quando realmente recebido, e não um
título inferido da conversa.

A base analisada foi o worktree `contact-center-transcription`, derivado de
`fc6a76f207a11f1080ae845982b3c13c9c444ca6`, mais o código local do Marketing Center.
Foram lidos código, testes e pesquisa já versionada; nenhum payload de cliente, anexo
real, banco ou host foi consultado para este estudo. Os números de linha abaixo
referem-se aos arquivos observados nessa leitura.

## Resultado

É viável reproduzir o resultado visual. O Contact Center já guarda a evidência de
origem, seus identificadores, a conversa e, no WuzAPI, a URL de origem. Faltam a captura
específica da apresentação do anúncio, o download privado da imagem e a projeção para a
interface. Trocar apenas o template não basta.

O primeiro incremento recomendado é capturar os dados do anúncio que chegam no próprio
webhook WuzAPI e mostrar um cartão associado à mensagem. O enriquecimento via Marketing
API deve ser uma segunda origem de dados, opcional, com credenciais e escopo próprios.
Não há necessidade de depender de Marketing Center para mostrar um anúncio cujo conteúdo
já veio no webhook.

## O que já existe e onde a informação se perde

| Informação                       | WuzAPI hoje                                                                                                                | Meta Messenger/Instagram hoje                                 | Trabalho necessário                                                                   |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Texto enviado pelo contato       | Projetado normalmente como mensagem                                                                                        | Projetado normalmente como mensagem                           | Manter intacto; não concatenar o anúncio ao corpo                                     |
| Identificação da origem          | `externalAdReply.sourceID`, `ctwaClid`, sinais de entrada e UTM viram touchpoints                                          | `referral.ad_id`, `ref`, `source` e `type` viram touchpoints  | Reutilizar ledger e seus namespaces                                                   |
| URL de origem                    | `sourceURL/sourceUrl` entra em `AttributionDTO.source_url` e no touchpoint                                                 | Não é capturada pelo contrato de referral atual               | Criar URL pública validada de apresentação; não expor automaticamente o valor técnico |
| Título e texto do anúncio        | `title` e `body` podem sobreviver no envelope sanitizado, se fornecidos, mas são excluídos pelo normalizador de atribuição | Campos extras de referral são removidos antes da normalização | Capturar snapshot limitado e explícito antes da perda                                 |
| Saudação sugerida                | `greetingMessageBody` pode permanecer no envelope, mas não no DTO de atribuição                                            | Sem contrato de apresentação atual                            | Opcional; não confundir com texto que o contato efetivamente enviou                   |
| Tipo da peça                     | `creative.media_type` e `creative_media_type` persistidos                                                                  | Sem preenchimento criativo no normalizador de referral        | Reutilizar tipo conhecido; preservar “desconhecido” quando faltar                     |
| URL de miniatura e bytes         | Toda chave contendo `thumbnail` é descartada pelo sanitizador; inclui `thumbnailUrl`                                       | Nenhuma miniatura de referral é preservada                    | Extrair locator privado antes da sanitização; baixar em fila                          |
| Outras URLs de imagem            | `mediaURL` e `originalImageURL` podem sobreviver no envelope, mas não entram no DTO                                        | Contrato de referral não preserva essas informações           | Validar cada campo; página HTML não é arquivo de imagem                               |
| Conteúdo enriquecido do catálogo | Marketing Center resolve identidades e sincroniza entidades                                                                | Mesmo catálogo pode ser usado após resolução autorizada       | Catálogo atual não traz copy/thumbnail suficientes; ampliar leitura específica        |

### WuzAPI

O sanitizador em
[`controllers/webhook.py:147`](../contact_center_wuzapi/controllers/webhook.py#L147)
remove thumbnails, base64, data URIs, cópias redundantes e material criptográfico que
não pertence à mídia principal. O envelope resultante é persistido em
`contact.center.inbox.event`; portanto, “raw” significa envelope já sanitizado, não
cópia integral recuperável do webhook.

O normalizador faz outra seleção:
[`services/adapter.py:285`](../contact_center_wuzapi/services/adapter.py#L285) define os
campos de `externalAdReply` que podem atravessar `_attribution_context()`. Título,
corpo, saudação e URLs de preview não estão nessa lista. Em `:1193`,
`_attribution_values()` reconhece a origem; em `:1299` monta apenas
`creative.media_type`; em `:1323` conserva `source_url`.

A pesquisa anterior já identificava esses campos de apresentação e registrava a perda
deliberada de thumbnails para o caso de atribuição, que não precisava de imagem:
[`meta-click-to-whatsapp-attribution.md`](meta-click-to-whatsapp-attribution.md). Isso
documenta capacidade histórica dos payloads analisados naquela pesquisa; não prova que
todos os novos anúncios fornecerão todos os campos.

Não promover `meta.source_id` automaticamente a `meta.ad_id`. O contrato atual preserva
a ambiguidade do identificador recebido. Um bloco com apenas título ou thumbnail também
não deve passar a ser classificado como clique pago: `_attribution_values()` exige
evidência de origem e ignora apresentações sem evidência suficiente.

### Meta

[`services/messaging.py:62`](../contact_center_meta/services/messaging.py#L62),
`_sanitize_referral()`, conserva somente `ref`, `source`, `type` e `ad_id`. Isso vale
para referral associado à mensagem, a postback ou a evento independente. Qualquer
contexto de anúncio adicional é descartado nessa fronteira.

[`services/normalizer.py:141`](../contact_center_meta/services/normalizer.py#L141)
transforma os quatro campos em evidência. O evento independente de `:357` produz
`attribution.observed`; ele pode anteceder a primeira mensagem e não inventa uma
mensagem. O futuro cartão deve respeitar essa diferença: vincular à mensagem quando
existe vínculo real; quando existe apenas um touchpoint de conversa, mostrar no painel
Origem ou como contexto separado da conversa.

O módulo `contact_center_meta` analisado atende Messenger/Instagram. O objeto
`messages[].referral` da WhatsApp Cloud API não pode ser tratado como uma estrutura já
implementada nesse adapter. Campos adicionais como `ads_context_data`, seus textos e
URLs são candidatos a suportar quando houver fixture autorizada e contrato confirmado
para cada transporte; este estudo não confirma que estão presentes em uma conta
conectada.

### Contrato e projeção comuns

[`services/dto.py:66`](../contact_center_base/services/dto.py#L66) permite somente
`media_type` em `creative`.
[`models/attribution.py:432`](../contact_center_base/models/attribution.py#L432) já
exclui copy e URLs de preview do fingerprint canônico, mas isso não significa que o DTO
atual aceite esses campos. São decisões separadas.

O ledger guarda `source_url`, UTM, tipo de mídia, identificadores e vínculos. A projeção
em [`models/attribution.py:940`](../contact_center_base/models/attribution.py#L940)
oferece apenas classificação, plataforma, rótulos UTM, tipo e data.
[`get_attribution()`](../contact_center_base/models/ui_api.py#L2292) autoriza a conversa
antes dessa projeção. O frontend também possui whitelist em
[`normalizeAttributionItem()`](../contact_center_ui/static/src/js/contact_center_model.esm.js#L646);
adicionar campos só no backend não os fará aparecer.

Hoje a seção
[`AttributionTouchpoints`](../contact_center_ui/static/src/xml/attribution_touchpoints.xml)
está no painel de detalhes, sob Origem. Não há um cartão de anúncio na bolha inicial.

## Comparação com Chatwoot

Na implementação pública da integração Evolution → Chatwoot, `getAdsMessage()` extrai
título, corpo, URL de thumbnail e URL de origem. O fluxo baixa a imagem, adapta-a para
320 × 180 e envia um anexo acompanhado do texto inicial, título, trecho e link. Isso
explica uma aparência como a descrita, mas não identifica a versão ou integração da
captura fornecida. Fonte:
[Evolution, código consultado](https://github.com/evolution-foundation/evolution-api/blob/fa09d37892cdbb1d65a250155d293d92230c5b30/src/api/integrations/chatbot/chatwoot/services/chatwoot.service.ts#L1724).

O Chatwoot consultado também tem um componente específico de referral com título, corpo,
link, imagem e estado de falha. Isso confirma o padrão visual; o código desse componente
usa URLs externas de mídia, comportamento que não atende ao requisito de miniaturas
privadas deste projeto. Fonte:
[WhatsappReferral.vue](https://github.com/chatwoot/chatwoot/blob/2f1ed80f894ed9a3eba636ebab90ca61deec110a/app/javascript/dashboard/components-next/message/bubbles/Text/WhatsappReferral.vue).

A implementação proposta preserva a mensagem original e associa o cartão como metadado
visual. Não copia código nem transforma conteúdo do anúncio em palavras do cliente. Os
exemplos deste documento são ilustrativos, fornecidos no pedido, e não evidência de um
evento real.

## Proposta de dados e interface

Criar uma apresentação derivada do touchpoint, com ciclo de vida próprio, em vez de
sobrecarregar os identificadores de atribuição. Uma forma possível é
`contact.center.attribution.preview`, ligada ao touchpoint e à companhia; nenhuma nova
FK obrigatória para `mail.message` é necessária. O touchpoint já possui o vínculo
transitório com mensagem/conversa.

| Campo proposto              | Finalidade e limite inicial proposto                                                |
| --------------------------- | ----------------------------------------------------------------------------------- |
| `touchpoint_id`             | Referência à evidência já deduplicada; única por variante escolhida de apresentação |
| `title`, `body`             | Texto simples, até 256 e 2.000 caracteres; UI mostra trecho menor e expansão        |
| `source_public_url`         | URL HTTPS pública validada; separada do `source_url` técnico preservado             |
| `media_type`                | Imagem/vídeo/desconhecido; vídeo usa miniatura e link, sem reprodução automática    |
| `thumbnail_attachment_id`   | Imagem privada local, sem URL assinada na projeção                                  |
| `presentation_source`       | `provider_snapshot` ou `marketing_catalog`; origem explícita do conteúdo            |
| `observed_at`, `fetched_at` | Distinguir o que veio na interação do que foi consultado depois                     |
| `state`, `error_code`       | Pendente/pronto/sem imagem/indisponível; mensagem continua utilizável               |
| `content_hash`, `revision`  | Evitar downloads repetidos e representar alteração do criativo sem novo clique      |
| `private_locator_ref`       | Referência opaca de uso interno, com expiração; nunca URL no DTO público            |

Os limites são decisões propostas para o produto, não limites documentados dos
provedores. A apresentação não deve integrar `canonical_key`, fingerprint de aquisição
ou revisão de Marketing. Mudança de thumbnail ou título não é novo contato com anúncio.
Se o catálogo atual divergir do snapshot da interação, preservar o snapshot e
identificar o enriquecimento como informação consultada posteriormente.

Fluxo recomendado:

1. Após autenticar e rotear o webhook, extrair apenas apresentação válida do contexto
   ativo; ignorar anúncios contidos em mensagens citadas/encaminhadas que não provem a
   origem desta interação.
2. Capturar a evidência pelo fluxo existente e ligar o snapshot ao touchpoint
   correspondente.
3. Enfileirar o download da miniatura sem bloquear a mensagem ou a resposta do webhook.
4. Notificar `message_updated` quando houver mensagem vinculada e `attribution_updated`
   para atualizar o painel Origem.
5. Projetar um objeto limitado, como `ad_origin_preview`, com texto, UUID público e rota
   local de thumbnail.

Na timeline, acrescentar o cartão em `MessagePayload`, próximo ao texto original,
mantendo ações de resposta/cópia/edição vinculadas à mensagem. Para a experiência
descrita: texto inicial, cartão com imagem, título, trecho e “Ver anúncio no Instagram”.
Abaixo do cartão pode haver uma identificação discreta “Anúncio de origem”. Não usar o
título da campanha como título da peça quando forem campos diferentes.

O painel Origem reutiliza a mesma apresentação para revisitar cliques anteriores. Não
repetir o anúncio em toda mensagem da conversa. Em falha de imagem, mostrar texto e
link; em anúncio indisponível, manter a evidência de origem sem inventar o criativo.

## Download privado da thumbnail

O browser deve receber apenas uma rota autenticada do Odoo, por exemplo
`/contact_center/attribution/<public_ref>/thumbnail`. A rota resolve o touchpoint,
verifica companhia, acesso à conversa e política de visualização antes de servir a
imagem. UUID opaco não substitui autorização; não usar attachment público, token
permanente ou URL assinada da Meta no HTML, bus, logs ou resposta RPC.

Reaproveitar o desenho do vault de
[`contact_center_meta/models/media_locator.py:22`](../contact_center_meta/models/media_locator.py#L22),
que guarda URLs privadas por curto período, sem ACL pública, com token interno e
referência opaca. Contudo, ele atualmente valida somente slots `attachment:*` e `story`
(`:264` e validações seguintes); uma thumbnail de anúncio exige contrato novo de slot e
verificação do item correspondente. Não forjar uma mídia de mensagem para contornar essa
checagem. No WuzAPI, a extração da thumbnail deve ocorrer antes de
`_sanitize_webhook_value()` descartá-la, depois de autenticação e validação do contexto.

O download deve ter fila própria ou canal limitado, idempotência por apresentação/hash,
timeout, limite de bytes e tentativas finitas. Proposta inicial: até 2 MiB,
JPEG/PNG/WebP decodificável, teto de pixels e conversão para derivado de até 640 × 360;
são limites ajustáveis em homologação. Preferir thumb fornecida; só tratar
`mediaURL/originalImageURL` como imagem após validação real. Não carregar iframe, HTML
retornado por página Instagram, SVG arbitrário ou vídeo integral como se fosse
thumbnail.

Validar HTTPS, hostname permitido do provedor, porta, ausência de credenciais embutidas,
destino DNS e cada redirecionamento. Rejeitar rede privada/local/link-local; não
encaminhar token Graph para CDN/redirecionamento. A base possui validações úteis em
[`contact_center_meta/services/media.py:57`](../contact_center_meta/services/media.py#L57),
mas uma nova implementação deve verificar também resolução/endereço de destino e não
assumir que uma URL arbitrária do webhook é segura.

Conservar a imagem como `ir.attachment` privado de propriedade da apresentação, com
dimensões conhecidas e cache privado. Expirar/apagar locators assinados após consumo ou
prazo; permitir falha definitiva 403/404/410 sem interromper o atendimento. Falhas/logs
guardam códigos, não query strings privadas. No frontend, usar carregamento lazy
existente e dimensões reservadas para evitar mudança do layout.

O link “Ver anúncio” é um caso diferente: pode abrir um permalink público validado, com
`noopener noreferrer`. O helper `_public_social_url()` em
[`messaging.py:890`](../contact_center_meta/services/messaging.py#L890) é uma referência
local útil, mas sua whitelist não cobre todo link CTWA, como `fb.me`. Ampliar
deliberadamente os tipos aceitos; não apenas remover parâmetros de uma URL cujo
funcionamento dependa deles. Manter o `source_url` técnico sob a política atual.

## Enriquecimento opcional pelo Marketing Center

No repositório vizinho Marketing Center,
`marketing_center_contact_center/models/attribution_bridge.py:240` já envia os
touchpoints ao serviço de Marketing com escopo de companhia e preserva a referência de
evidência. O mapper transporta os identificadores, URL sanitizada e tipo de mídia; não
fornece copy/thumbnail ao Contact Center.

O catálogo Meta já lê a associação anúncio → criativo e IDs de imagem/vídeo/post, mas o
spec de creative em `marketing_center_meta/services/catalog.py:106` não solicita
`title`, `body`, `thumbnail_url`, `image_url` ou conteúdo suficiente de
`object_story_spec`. Portanto, instalar o bridge sozinho não completa o cartão.

Uma segunda etapa pode resolver o identificador no catálogo autorizado e buscar os
campos de apresentação do criativo. O SDK oficial Meta contém campos como título, corpo,
imagem, thumbnail e especificações da peça; a coleção oficial também exemplifica leitura
de detalhes de criativo. A existência dos campos não garante preenchimento em todo
formato de anúncio. Fontes:
[SDK oficial Meta](https://github.com/facebook/facebook-python-business-sdk/blob/5286888addfe3ba3718db65fbf132bd66de3ddfe/facebook_business/adobjects/adcreative.py),
[coleção Meta no Postman](https://www.postman.com/meta/facebook-marketing-api/request/8kvi2rw/getcreativedetails).

Usar o perfil `ads_reader` e a capacidade `read_entities` já condicionada a `ads_read`
em `marketing_center_meta/services/adapter.py:88`. Acesso de mensagens não deve ser
considerado autorização de anúncios. Validar a fonte/conta de anúncios da companhia,
acesso real do token ao ativo e ausência de ambiguidade antes da consulta; não testar o
mesmo ID em contas aleatórias. O resolver existente tem estados explícitos
`unresolved/ambiguous` e tratamento polimórfico para `meta.source_id` em
`models/attribution_resolution_service.py:251`.

Sem perfil apto, ID resolvido ou criativo acessível, continuar com snapshot do webhook
ou cartão incompleto. Nenhuma solicitação de novo scope, conexão de conta ou chamada a
anúncio real foi executada neste estudo. As páginas oficiais de documentação de webhooks
Meta não renderizaram nesta consulta; detalhes adicionais de referral, campos por
formato e permissões de transportes específicos devem ser confirmados com
documentação/fixtures antes da implementação desse enriquecimento.

## Permissões e retenção

Hoje `account.attribution_ui_enabled` controla a visualização pelos agentes;
administradores continuam podendo visualizar. A ajuda do campo em
[`account.py:904`](../contact_center_base/models/account.py#L904) declara que URLs
técnicas e evidência permanecem restritas. A apresentação pública é ampliação desse
contrato: documentar quais campos os agentes passam a receber e manter a checagem do
ator real de `:1511`, companhia ativa, membership/equipe e conta exata. Sugestão: cartão
habilitado junto da política de Origem, com configuração explícita caso a empresa queira
separar as duas visualizações.

Os identificadores de anúncio não devem desaparecer com a limpeza do histórico. A
retenção atual preserva os touchpoints e seus identificadores, desligando
`message_binding_id` antes de excluir mensagens em
[`retention_dependencies.py:37`](../contact_center_base/models/retention_dependencies.py#L37).
A expiração automática por prazo hoje é opt-in e limitada a grupos WhatsApp via WuzAPI
(`retention.py:193`); não presumir que esteja ativa para conversas diretas ou Meta. A
exclusão integral da conversa também desliga o vínculo da conversa em
[`attribution.py:322`](../contact_center_base/models/attribution.py#L322). O Marketing
bridge continua apontando a evidência preservada; não criar revisão nova apenas por
limpeza do chat.

Para a apresentação, adotar inicialmente cache com prazo e expiração junto ao conteúdo
de origem, sem preservar cópia da frase do cliente. Antes de desligar os vínculos,
limpar snapshots/attachments correspondentes, cancelar jobs pendentes e impedir jobs
tardios de restaurar imagem ou texto. Isso preserva referências de anúncio e fatos de
aquisição, sem fazer o cache visual impedir a expiração da conversa. Se futuramente o
produto quiser manter o criativo histórico para auditoria de Marketing, essa retenção
deve ser uma política separada do cache do chat.

Uma apresentação ligada somente ao touchpoint não será detectada automaticamente pelo
guard de consumidores, que procura FKs de terceiros para mensagens/bindings. Portanto,
implementar explicitamente os hooks de retenção/exclusão e a coleta dos jobs; não
confiar em cascata. Revalidar a elegibilidade antes do fetch e antes de gravar seu
resultado. Referrals sem mensagem vinculada precisam de TTL próprio para a apresentação;
o touchpoint pode continuar existindo.

## Histórico, entrega e aceitação

Não prometer reconstrução completa dos anúncios antigos. Para WuzAPI, um backfill
limitado pode extrair título/corpo de envelopes sanitizados ainda presentes, usando o
mesmo contexto ativo e sem reprocessar a mensagem. Thumbnails já descartadas não são
recuperáveis desses envelopes; URLs restantes podem ter expirado. Para Meta, o
sanitizador atual já eliminou os campos extras: enriquecimento autorizado por catálogo
pode produzir uma apresentação consultada hoje, não necessariamente a versão vista no
clique original. Não reabrir conteúdo que a retenção já apagou.

Entrega sugerida:

1. Contrato de apresentação + captura WuzAPI + cartão de texto/link, com fixtures
   sintéticas de anúncio completo, incompleto e contexto citado.
2. Vault/worker de thumbnail + rota privada + UI com fallback, testados em homologação
   com mídia sintética; nenhuma dependência de ativo real para CI.
3. Captura adicional Meta por transporte e integração opcional com catálogo, mantendo
   origem e data da apresentação.
4. Backfill opt-in, por lote limitado, somente depois de demonstrar idempotência e
   elegibilidade de retenção.

Aceitação mínima: mensagem original intacta; clique repetido não duplica
touchpoint/card; ausência de título/thumb não bloqueia mensagem; contexto citado não
gera origem falsa; anúncios diferentes na mesma conversa permanecem distintos; texto
renderizado com escape; thumbnail servida somente pela rota local autorizada; troca de
companhia/conversa invalida acesso; nenhuma URL assinada aparece no browser;
falha/expiração tem fallback; exclusão concorrente não ressuscita apresentação; limpeza
conserva IDs e referências de atribuição; QUnit cobre cartão completo, parcial, erro e
troca de conversa.

O estudo deixa uma implementação delimitada: aproveitar a atribuição já existente,
acrescentar apresentação privada e mostrar o cartão junto ao conteúdo que originou o
atendimento. Nenhum arquivo funcional do Contact Center ou do Marketing Center foi
modificado como parte desta entrega.
