# Mídia e compartilhamentos Meta — implementação local

Data: 2026-09-06. Escopo: Messenger Page e Instagram profissional vinculado a uma
Facebook Page, com Facebook Login. Não houve mensagem real, upload para a Meta,
alteração de permissões do aplicativo nem implantação.

## Contrato externo verificado

O envio usa a Page proprietária do Page Access Token nos dois transportes. O ID da conta
profissional Instagram permanece como identidade do asset/webhook; ele não substitui o
Page ID na rota deste adaptador. O destino continua PSID no Messenger e IGSID no
Instagram. Não foi habilitada a modalidade Instagram Login.

O fluxo de arquivos do Messenger aceita upload prévio por `message_attachments`,
multipart `filedata` e envio posterior com `attachment_id`. O limite publicado para
arquivos locais é 25 MB; o limite de imagem enviada por URL é diferente e não é
utilizado pelo adaptador.
[Fonte: Meta, Saving Assets](https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages/saving-assets).

Para Instagram Page-linked, o upload utiliza Page ID, Page Access Token e
`platform=instagram`. A documentação lista imagem de até 8 MB, áudio/vídeo/PDF de até 25
MB e restrição a nomes ASCII. A implementação segue o guia de envio para referenciar um
anexo por tipo de mídia e `payload.attachment_id`. O guia de upload contém exemplos e
texto divergentes sobre `MEDIA_SHARE`; isso não foi interpretado como autorização para
compartilhar qualquer publicação por ID.
[Fontes: Meta, Attachment Upload](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/attachment-upload),
[Send Message](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message).

Os documentos públicos foram obtidos por GET nos endereços antigos, que redirecionaram
para os endereços canônicos acima e retornaram HTTP 200. O cliente de busca recebeu HTTP
429 para alguns desses endereços. A coleção oficial também confirma upload na Page e uso
de `payload.is_reusable`:
[Meta, coleção Messenger Platform](https://github.com/fbsamples/messenger-platform-samples/blob/main/postman/messenger-platform-api.postman_collection.json).

## Envio implementado

1. O usuário faz upload privado pelo endpoint já existente da base. ACL, pertencimento à
   conversa e consumo único da referência continuam na base.
2. O preflight da fila revalida rota, conversa direta, janela de 24 horas e o anexo
   efetivamente vinculado à mensagem outbound. Confere conta, conexão, DTO persistido,
   estado, arquivo privado, modelo/res_id, MIME, tamanho e SHA-256.
3. A fila registra uma intenção de upload contendo identificador interno, MIME, tamanho,
   hash e nome ASCII determinístico. Não registra binário/base64, URL pública do anexo
   ou credenciais.
4. Depois da fronteira durável da fila, o adaptador realiza dois HTTP POST: primeiro
   `/{PAGE_ID}/message_attachments` com multipart; depois `/{PAGE_ID}/messages` contendo
   o `attachment_id` retornado. Bearer token e `appsecret_proof` são acrescentados
   apenas pelo transporte Graph, em memória.
5. A janela de 24 horas e a disponibilidade da rota são verificadas novamente depois do
   upload, antes do POST que entrega a mensagem. Replies mantêm `reply_to.mid`; sucesso
   exige correlação exata de destinatário e ID da mensagem.

Evidência: `contact_center_meta/services/outbound.py`,
`contact_center_meta/services/outbound_media.py`,
`contact_center_meta/services/adapter.py` e
`marketing-center/meta_api_base/services/graph.py`.

### Falhas e repetição

Timeout, resposta ambígua ou ausência de ID utilizável no upload podem levar a novo
upload. Isso pode deixar um objeto sem mensagem associada na Meta. Não duplica uma
mensagem no destinatário porque o endpoint de upload não realiza o envio.

O POST final mantém a classificação conservadora da base: resultado ambíguo fica
`uncertain`, sem redispatch automático. Uma janela que fecha durante o upload impede o
envio final, podendo igualmente deixar o upload sem uso. Não foi criado cache
persistente de attachment IDs nem rotina de exclusão remota: ambos ampliariam o estado e
o contrato necessários sem ganho essencial para este fluxo.

### Capabilities e limites efetivos

Os limites abaixo são o menor valor entre o provedor e a base; MB usa 1.000.000 bytes e
MiB usa 1.048.576 bytes.

| Conteúdo           | Messenger Page                      | Instagram Page-linked           |
| ------------------ | ----------------------------------- | ------------------------------- |
| Imagem             | JPEG, PNG, GIF; 16 MiB              | JPEG, PNG; 8 MB                 |
| Áudio como arquivo | AAC, M4A/MP4, MP3, OGG, WAV; 16 MiB | AAC, M4A/MP4, WAV; 16 MiB       |
| Vídeo              | MP4, OGG, AVI, MOV, WebM; 25 MB     | MP4, OGG, AVI, MOV, WebM; 25 MB |
| Documento          | PDF; 25 MB                          | PDF; 25 MB                      |

Cada comando envia exatamente um anexo. Legenda junto ao anexo e gravação como voice
note não são anunciadas. A interface/base pode coordenar vários comandos; isso não é um
álbum atômico do provedor. O adaptador mantém a janela padrão de resposta e não introduz
tags promocionais ou exceções à política.

A autorização externa continua dependente de App Review, Page Token válido e permissões
realmente concedidas. O guia de upload Instagram lista `instagram_basic`,
`instagram_manage_comments`, `instagram_manage_messages` e `pages_messaging`, além da
tarefa MESSAGING na Page. A configuração/grant da App não foi alterada nesta tarefa;
rejeições de autorização passam pelo tratamento existente de provedor indisponível. Os
testes locais não homologam esses grants.

## Recebimento de compartilhamentos

`share`, `ig_post`, `ig_reel`, `reel` e `story_mention` geram conteúdo neutro
`structured_content.type=shared`, com até dez itens, título limitado a 200 caracteres e
kind `post`, `reel` ou `story`. A base persiste esse conteúdo e o expõe à UI.

Só caminhos públicos reconhecíveis de Instagram/Facebook são transformados em links,
removendo query e fragmento. URL assinada de CDN nunca vira campo público. Quando uma
URL privada em domínio permitido declara extensão de imagem/vídeo, a mensagem recebe um
MediaDTO e reutiliza download protegido e vault existentes. Conteúdo misto com anexos
normais preserva seus índices originais; o slot rejeitado não desloca referências
válidas. Sem permalink ou tipo de mídia comprovável, o cartão conserva o rótulo do
compartilhamento.

Replies a stories preservam o texto e o cartão de contexto. Quando existe mídia privada
identificável, o slot `story` agora é resolvido pelo mesmo vínculo estrito de
delivery/item/inbox/conexão que protege os anexos. URLs expiradas ou indisponíveis
seguem a falha de mídia existente, mantendo o conteúdo textual. Um contexto opcional
rejeitado não elimina texto válido; contradições de reply/scope continuam rejeitadas.
Não foi habilitada edição Instagram nem alterada a assinatura de subscriptions.

Evidência: `contact_center_meta/services/messaging.py`,
`contact_center_meta/services/normalizer.py` e
`contact_center_meta/models/media_locator.py`.

## Validação local

Black, isort, flake8 e `git diff --check` passaram nos arquivos alterados antes do
handoff. A instalação limpa integrada dos 24 addons foi iniciada pelo agente principal;
o resultado dessa execução deve ser registrado no relatório geral da release. Não se
presume aprovação a partir de testes ainda em andamento.

Cobertura adicionada:

- `TestMetaGraphClient`: multipart limitado, autenticação/proof em memória,
  filename/bytes/MIME/método/JSON incompatíveis rejeitados antes de HTTP.
- `TestMetaPhase64Outbound`: upload privado da UI até fila e HTTP simulado em Messenger
  e Instagram, reply, filename ASCII, snapshot sem conteúdo, correlação, caption/tamper,
  janela fechando durante upload, upload retryable e fila que não repete um envio final
  ambíguo.
- `TestMetaWebhookConsumer`: ingresso compartilhado completo com permalink público, CDN
  privada, mídia mista e slots inválidos; persistência/UI/replay; contexto de story com
  download protegido; URLs não públicas sem vazamento.

Os cenários não fazem chamadas reais ao provedor. Homologação com a App dedicada e seus
grants continua necessária para validar aceitação externa dos formatos antes de uma
publicação de produção.
