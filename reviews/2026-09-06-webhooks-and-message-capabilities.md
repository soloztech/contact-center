# Webhooks e formatos de mensagem — auditoria de cobertura

Este documento registra o diagnóstico anterior às implementações desta data. As matrizes
abaixo são históricas. Consulte os resultados posteriores de
[mensagens ricas](2026-09-06-rich-messaging.md),
[mídia Meta](2026-09-06-rich-messaging-meta.md) e
[capacidades por canal](2026-09-06-channel-capabilities.md) para a cobertura candidata.

Data: 2026-09-06. Repositórios: Contact Center e Marketing Center, árvore local com as
correções da auditoria de 2026-09-05. Estudo com inspeção de código, contratos dos
provedores e leitura do laboratório SERVIDOR05. Não houve envio real, replay
administrativo, mudança no laboratório ou implantação.

## Conclusão

Há recursos faltantes, mas a contagem `unsupported` não mede diretamente mensagens
perdidas. Ela mistura eventos técnicos, conversas fora do escopo e conteúdo ainda não
implementado. Também existem mensagens `done` que preservam apenas um resumo, sem os
dados necessários para o atendente ou uma automação.

As maiores lacunas práticas são: envio de vários arquivos; envio de mídia Meta; botões e
listas com resposta estruturada; cartões de contato utilizáveis; contexto visual de
compartilhamentos; e visibilidade de citações cujo original está ausente. Álbum nativo
exige confirmação e suporte específico do transporte.

## Estado observado no laboratório

Consulta somente leitura por ORM, com validação da identidade do banco. A coleta
agregada usou XML-RPC. A inspeção de formatos usou o mesmo ORM via JSON-RPC porque o
XML-RPC do Odoo não serializa valores JSON `null`. Foram armazenados contadores, nomes
de campos e evidências técnicas, sem texto de clientes ou URLs privadas.

O instante e os filtros exatos estão em `lab-webhook-analysis.json`. No snapshot:

| Estado        | Histórico | Últimas 24 horas |
| ------------- | --------: | ---------------: |
| `done`        |    33.900 |              658 |
| `unsupported` |    27.584 |              848 |
| `dead`        |     1.455 |               43 |
| `blocked`     |         1 |                0 |

Os 848 `unsupported` recentes se dividem em:

| Motivo                               | Quantidade | Interpretação                                                                               |
| ------------------------------------ | ---------: | ------------------------------------------------------------------------------------------- |
| Conversas broadcast/status           |        518 | Fora do contrato de conversa atual; não significa falha em DM.                              |
| Mensagem somente de protocolo        |        229 | Sem conteúdo humano para criar uma bolha.                                                   |
| Leitura do próprio aparelho em grupo |         82 | Não comprova leitura por outro participante.                                                |
| Estado de recibo sem mapeamento      |         12 | Exige classificação específica do estado; não habilitar como entregue/lido por aproximação. |
| Recibo sem IDs de mensagens          |          6 | Falta evidência para correlacionar.                                                         |
| Envelope pai de álbum                |          1 | Coordena os filhos; não contém uma imagem isolada.                                          |

Nos últimos sete dias, os 14 casos de conteúdo não implementado incluíram cinco
`groupStatusMentionMessage`, quatro `pinInChatMessage`, três `extendedTextMessage` e
dois `messageHistoryNotice`. O nome do campo sozinho não prova que havia texto válido
nos três `extendedTextMessage`. Houve ainda 13 edições de mídia não implementadas e 54
envelopes de coordenação de álbum.

Os 43 `dead` recentes eram correlações ausentes: 18 mutações, 12 respostas, 11 recibos
de grupo e dois recibos diretos. Nos sete dias, 87 respostas terminaram com original
ausente; 76 eram inbound com texto e/ou mídia. Em 86 dos 87 casos não havia binding do
original nem no momento da inspeção. A pesquisa do binding foi restrita à conexão, sem
concluir que IDs de conversas distintas são equivalentes.

O laboratório informa Contact Center `16.0.1.0.0`, Marketing Meta `16.0.2.1.3` e shared
webhook `16.0.1.4.2`. Essas contagens descrevem o ambiente instalado; não provam que ele
executa os mesmos arquivos da árvore local auditada.

## Matriz funcional

“Resumo” significa que há mensagem visível, mas não representação completa do objeto.
“Não” significa ausência no fluxo do produto, mesmo se o provedor oferecer endpoint.

| Recurso                                  | Recebimento/projeção atual                                                                      | Envio atual                                   | Trabalho restante                                                                       |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------------------------------------------------- |
| Texto e resposta citada                  | WuzAPI e Meta; original ausente não impede conteúdo novo após tolerância                        | Ambos, com limites por canal                  | Mostrar citação indisponível; vínculo tardio automático ainda não implementado.         |
| Imagem, áudio, vídeo e documento         | WuzAPI e Meta                                                                                   | WuzAPI: um arquivo; Meta: não                 | Transporte de mídia Meta e seleção/envio de vários arquivos.                            |
| Várias mídias em um webhook Meta         | Até 10; renderer já percorre a lista                                                            | Não                                           | Não confundir o renderer com suporte outbound.                                          |
| Álbum WhatsApp                           | Envelope pai técnico; filhos viram mídias independentes; associação é preservada como evidência | Não                                           | Agrupamento visual por associação explícita; álbum nativo requer suporte de transporte. |
| Botões/listas recebidos                  | Resumo textual; IDs/opções não formam contrato operacional                                      | Não                                           | Modelo pequeno para opções/ID/tipo e renderer próprio.                                  |
| Resposta a botão/lista                   | WuzAPI: texto selecionado quando disponível; Meta: quick reply/postback normalizados            | Não há criação de botões/listas               | Preservar seleção estruturada para ações/automação sem executar payload recebido.       |
| Respostas rápidas do atendente           | Texto salvo inserido no composer                                                                | Sim                                           | São atalhos do atendente, distintos de botões enviados ao cliente.                      |
| Contato/vCard                            | WuzAPI: resumo com nome; número/cartão não é projetado de forma utilizável                      | Não                                           | Extração limitada de campos de contato e cartão; vínculo ao CRM sempre explícito.       |
| Localização                              | WuzAPI: texto/coordenadas; atualização ao vivo não é rastreamento contínuo                      | Não                                           | Cartão/link seguro e envio estruturado, se necessário ao atendimento.                   |
| Sticker                                  | WuzAPI WebP recebido como mídia; Meta recebido quando fornecido pelo canal                      | Não há envio nativo                           | Transporte específico e formatos aceitos, sem tratar JPEG como sticker.                 |
| Enquete                                  | Pergunta/opções em resumo; votos não são projetados                                             | Não                                           | IDs, apuração e respostas; prioridade menor que atendimento básico.                     |
| Compartilhamento de post/reel/story Meta | Reconhecido; vários casos viram só rótulo textual                                               | Não                                           | Expor conteúdo/contexto útil sem publicar URL privada do provedor.                      |
| Mensagem composta Meta                   | Algumas combinações degradam para texto; outras ficam `unsupported`                             | Não                                           | Suportar combinações reais com testes, preservando cada parte válida.                   |
| Reações                                  | WuzAPI e Meta                                                                                   | WuzAPI                                        | Meta outbound e seletor ampliado, se houver demanda.                                    |
| Edição textual e exclusão                | WuzAPI; Meta conforme carrier/modalidade                                                        | WuzAPI, somente texto na edição após correção | Edição de legenda não está implementada; não usar endpoint textual para isso.           |
| Mensagem fixada no WhatsApp              | `pinInChatMessage` não projetado                                                                | Não                                           | É diferente de fixar a conversa na lista do Contact Center.                             |
| Chamadas                                 | Cartões de chamada WuzAPI para eventos suportados                                               | Não faz/atende chamadas                       | Telefonia é outro escopo; não confundir cartão com ligação integrada.                   |
| Digitando/presença                       | Eventos fora da assinatura padrão                                                               | Não                                           | Estado efêmero opcional, com expiração e controle de volume.                            |
| Histórico anterior                       | Não há importador `HistorySync`                                                                 | Não aplicável                                 | Importação limitada, deduplicação e política de origem/cronologia.                      |
| Templates oficiais WhatsApp              | Não há adaptador Cloud API                                                                      | Não                                           | Adaptador oficial, catálogo/status/variáveis e políticas do canal.                      |

## Contratos dos provedores

Outras lacunas de recepção verificadas no contrato fixado:

- Vídeo circular `ptvMessage` existe no protocolo, mas não é selecionado como mídia pelo
  adaptador nem pela lista de chaves de download permitidas.
- Respostas Native Flow em `paramsJSON` não são interpretadas; um corpo ausente pode
  aparecer apenas como conteúdo interativo genérico.
- A WuzAPI já entrega `pollVote.selectedOptions` quando consegue resolver um voto. O
  módulo não usa essa informação. Aproveitá-la exige correlação e idempotência, mas não
  exige implementar criptografia de enquetes dentro do Odoo.
- Wrappers de visualização única/temporária são abertos como conteúdo comum. Não existe
  semântica equivalente de visualização única ou expiração no produto. Representação e
  retenção precisam de contrato explícito antes de prometer essa equivalência.

Fontes primárias:
[eventos WuzAPI](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/wmiau.go),
[protobuf fixado](https://github.com/tulir/whatsmeow/blob/b572e5bcb92b/proto/waE2E/WAWebProtobufsE2E.proto).

O adaptador fixa WuzAPI `v1.0.8`, commit `9487eca9a40f292d19953a44983979c85d91ccce`. A
consulta do upstream também congelou o `main` em
`919c72c9750b2a1eedf0fcf9c9592f05fe46f61c`, sem atualizar o produto.

No commit fixado, existem rotas ativas para botões, listas, enquetes, contato,
localização e sticker. Não foi encontrada rota de envio de álbum. A rota
`/chat/send/template` está comentada, apesar de aparecer em `API.md`; documentação
isolada não comprova disponibilidade. O handler de botões constrói Native Flow e o de
edição constrói `ExtendedTextMessage`. A existência de handler ainda requer homologação
de renderização nos clientes antes de anunciar a capacidade.
[Rotas fixadas](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/routes.go),
[handlers fixados](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go).

O Messenger oferece envio de múltiplas imagens e quick replies. Nosso adaptador Meta
anuncia outbound textual e rejeita mídia; essa é uma lacuna do produto. Não estender
automaticamente limites/regras do Messenger ao Instagram.
[Coleção oficial Meta: envio](https://www.postman.com/meta/messenger-platform-api/folder/7cc3gd2/send-api),
[quick replies](https://www.postman.com/meta/messenger-platform-api/folder/hnhvmum/quick-replies).

O normalizador interno aceita edição Instagram, mas o ingresso compartilhado Page-linked
não a mapeia. A coleção oficial consultada demonstra `message_edit` na modalidade
Instagram Login; isso não homologa o contrato Facebook Login usado pelo adaptador.
Validar modalidade, campo de assinatura e callback real antes de habilitar.
[Coleção oficial Instagram](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api).

## Defeitos corrigidos nesta inspeção

1. **Texto de resposta de botão WuzAPI:** o JSON Go real guarda o texto no oneof
   `Response.SelectedDisplayText`; o fixture anterior o colocava na raiz. Normalização e
   fixtures foram corrigidos para o contrato fixado, com limite de texto e sem usar o ID
   opaco do botão como conteúdo humano.
2. **Anexo Meta inválido antes de mídia válida:** a compactação do array mudava índices
   enquanto o cofre mantinha o índice original. Agora os índices são preservados e
   somente slots explicitamente rejeitados são ignorados. Texto e mídias válidas
   continuam entrando, com as referências privadas corretas.
3. **Story inválida descartando texto:** contexto opcional vazio acompanhado da rejeição
   explícita do sanitizer deixa de impedir a mensagem. IDs de resposta e contradições de
   escopo continuam validados.
4. **Edição oferecida para mídia:** uma regra única restringe a ação, a admissão RPC e o
   preflight da fila a texto sem mídia. Comandos incompatíveis são rejeitados antes de
   chamar o provedor.
5. **Citação de original não capturado:** o recebimento tenta a correlação nas duas
   primeiras execuções, com intervalos solicitados de 5 e 10 segundos. Se o original
   continuar ausente, preserva texto/mídia/autor sem inventar `parent_id`. A referência
   permanece na evidência. O mesmo ID em outra conversa não é usado como original.
   Recibos, mutações e ecos contraditórios mantêm suas proteções.

A quinta correção não recompõe automaticamente uma citação cujo original chegue depois
dessa tolerância. Esse vínculo tardio exigiria reconciliação adicional; preservar o
conteúdo humano já resolve o caso predominante observado sem novo schema, cron ou
importador de histórico. Nenhum evento histórico foi reprocessado no laboratório durante
esta tarefa.

Referências: `contact_center_wuzapi/services/adapter.py::_interactive_content`;
`contact_center_meta/services/messaging.py::_sanitize_attachments`;
`contact_center_meta/services/normalizer.py::_attachment_rows` e `_reply_values`;
`contact_center_base/models/application_outbound.py::_outbound_text_edit_supported`;
`contact_center_base/models/application.py::_event_reply_binding`.

## Marketing Center e observabilidade

`changes.leadgen` é consumido pelo Marketing: hint durável, busca autenticada do Lead
Ads, submissão/atribuição e CRM pelo bridge. Não deve criar mensagem fictícia de chat.
Referral isolado também pode gerar atribuição sem bolha de conversa.

Os ledgers guardam envelopes sanitizados, não cópia integral do corpo recebido. Itens
desconhecidos podem conservar apenas classificação técnica. Replay não reconstrói dados
removidos; locators privados de mídia também expiram. Para ampliar um formato, adicionar
captura limitada dos campos necessários e fixtures reais. Não é necessário guardar
indiscriminadamente todo payload bruto.

Parte da degradação de conteúdo está no DTO técnico e não chega à timeline. Uma melhoria
pequena e útil é mostrar ao atendente que chegou conteúdo complementar não exibido,
preservando a distinção entre mensagem humana e evento de protocolo. O painel já tem
contagem recente de `unsupported`; falta separar motivos úteis para operação, em vez de
tratar todos como defeitos de mensagem.

## Ordem recomendada de implementação

1. Sinalizar conteúdo humano degradado e citação indisponível na interface. A
   preservação do corpo quando o original não foi capturado foi corrigida nesta
   auditoria; sua referência técnica ainda não é um indicador visual de citação.
2. Seleção de vários arquivos com preview, remoção e estado individual. Reusar upload,
   UUID e outbox por arquivo; repetir somente os não enviados. Agrupar visualmente
   filhos recebidos quando houver associação explícita, sem inferir álbum apenas pela
   proximidade dos horários.
3. Habilitar mídia outbound Meta usando armazenamento/outbox existentes.
4. Botões e listas WuzAPI: contrato limitado, formulário simples, IDs estáveis, resposta
   estruturada e teste ponta a ponta. Homologar o Native Flow real.
5. Contatos/localização e contexto visual de compartilhamentos. Avaliar enquetes,
   stickers enviados, presença e importação histórica conforme uso.
6. Álbum nativo e templates oficiais somente após resolver suporte do provedor. Não
   criar motor genérico de blocos ou workflow visual para esses incrementos.

Cada novo formato deve ter teste do webhook assinado até a mensagem apresentada, teste
do comando até o payload, replay sem duplicação e comportamento de envio incerto
preservado. Mock de normalizador sozinho não valida assinatura, sanitizer, fanout,
locator e consumer — os defeitos encontrados demonstram essa diferença.

## Validação e evidências

Instalação limpa dos cinco módulos afetados e dependências em banco local sintético:
**909 testes, zero falhas e zero erros**. Dez regressões novas cobrem os cinco defeitos.
Não houve mudança em JavaScript; a disponibilidade de edição foi testada na API que
fornece as ações à interface. Nenhum envio foi feito a provedor real.

Evidências em `scans/raw/20260906-centers-webhook-capabilities/`:

- `lab-inventory.json`, `lab-webhook-analysis.json`, `lab-shape-analysis.json`;
- `upstream-refs.json` e fontes congeladas em `upstream/`;
- `approved-test-results.json`, `odoo-approved-tests.log`, `run_approved_tests.py`;
- `approved-source-hashes.json` e logs `precommit-*.log`;
- scripts das consultas sanitizadas e registro operacional no incidente da data.

A primeira suíte dos quatro ajustes passou com 906 testes. Uma rodada intermediária da
correção de citações detectou um fixture que não persistia `job.retry` e um replay de
grupo sem referência local; ambos foram corrigidos e a rodada final de 909 testes
passou. Os arquivos de produto/teste permaneceram idênticos aos hashes congelados. A
revisão independente verificou os contratos WuzAPI e os guards/replays do core.
