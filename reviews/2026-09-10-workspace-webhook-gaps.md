# Atendimento: webhooks e cartão Pix

## Resultado da inspeção

Consulta somente leitura na produção em 10/09/2026. O envio Pix não é um webhook
ausente: três envios da caixa Alice no dia 09/09 chegaram como `Message`, foram
processados (`done`) e preservados como conteúdo `interactive`. Falta a projeção visual
específica do botão `payment_info`. Nenhum evento foi reprocessado e nenhuma mensagem
foi enviada durante a análise.

O payload recebido usa:

```
Message.interactiveMessage.InteractiveMessage.NativeFlowMessage
  buttons[].name = payment_info
  buttons[].buttonParamsJSON
    currency
    total_amount.{value,offset}
    payment_settings[].pix_static_code.{key,key_type,merchant_name}
    order / reference_id
```

Não foram copiados chaves Pix, dados do destinatário ou valores para este documento. O
parser atual em `contact_center_wuzapi/services/structured_content.py` admite
`quick_reply`, `cta_url` e `cta_call`; ignora `payment_info`. O fallback mantém o texto
da mensagem, mas não o cartão de pagamento. Uma resposta que cita o cartão também contém
o formato no `quotedMessage`.

## Implementação proposta — ainda não aplicada

1. Acrescentar um DTO de apresentação `payment_info`, separado de botões comuns. Validar
   JSON limitado, moeda, escala inteira positiva (`value / offset`), campos e
   comprimentos; nunca executar conteúdo ou URLs do payload.
2. Projetar apenas recebedor, chave, tipo da chave e valor quando disponíveis. Mostrar
   cartão compacto “Pix” com ação explícita “Copiar chave Pix”. Chave estática não deve
   ser rotulada como código “copia e cola”/BR Code.
3. Não marcar cobrança como paga nem criar pagamento/fatura a partir do texto, do clique
   ou de um estado informado pelo remetente. Conciliação bancária é outro fluxo, fora
   deste suporte visual.
4. Manter o acesso da conversa e a política de conteúdo apagado. Cobrir payload
   incompleto, escala inválida, tipos desconhecidos, citações e cartão oculto.
5. Após homologar, propor uma reprojeção restrita dos três registros existentes, com
   deduplicação e sem reenvio/outbox. Isso é uma operação separada, não executada nesta
   entrega.

## Outras classes observadas

A coleta de falhas dos últimos sete dias é móvel; os números são um retrato, não uma
estimativa de mensagens perdidas.

| Classe                                  |                                      Retrato observado | Tratamento sugerido                                                                                                     |
| --------------------------------------- | -----------------------------------------------------: | ----------------------------------------------------------------------------------------------------------------------- |
| Status do WhatsApp (`status@broadcast`) |                                          966 mensagens | Manter fora da conversa; distinguir “fora do escopo” de erro de integração.                                             |
| Recibos `Read`/`Delivered`              | 1.042 não suportados, 59 falhas transitórias esgotadas | Os estados já são implementados. Investigar destino/referência do recibo antes de adicionar suporte ou repetir eventos. |
| `ReadSelf`                              |                                      15 não suportados | Estudar semântica de leitura em outro dispositivo, sem assumir leitura por cada agente Odoo.                            |
| Apenas `messageContextInfo`             |                                                    165 | Não inventar mensagem humana; classificar como metadados.                                                               |
| `protocolMessage`                       |                 30 não suportados, 1 falha transitória | Separar sincronização/controle de edição e exclusão; estas últimas já têm fluxo próprio.                                |
| `albumMessage`                          |                                                      3 | Estudar agrupamento com mensagens-filhas já suportadas, sem duplicar imagens/vídeos.                                    |
| `IdentityChange`                        |                                    3 falhas de adapter | Validar contrato efetivamente recebido; a classe já tem normalização parcial.                                           |

Prioridade: cartão Pix, classificação operacional das exclusões esperadas e investigação
dos recibos transitórios; depois álbuns e leitura em outro dispositivo. Não é indicado
habilitar ou reprocessar todas as classes indiscriminadamente.

## Fontes

- Código local do adapter e payloads efetivamente recebidos (evidência privada
  `scans/raw/20260909-contact-center-workspace/webhook-scan.json`).
- [WuzAPI no commit de compatibilidade 9487eca](https://github.com/asternic/wuzapi/tree/9487eca),
  usado pelo adapter, incluindo suas dependências em
  [go.mod](https://github.com/asternic/wuzapi/blob/9487eca/go.mod).
- [Contrato protobuf do Whatsmeow](https://github.com/tulir/whatsmeow/blob/main/proto/waE2E/WAWebProtobufsE2E.proto):
  `NativeFlowButton` possui nome e parâmetros JSON; o conteúdo Pix específico acima foi
  confirmado no payload real, não inferido do protobuf genérico.
