# Carrossel WuzAPI — extensão experimental

Implementação isolada de transporte para o WuzAPI **v1.0.8 / 9487eca9**. Não é um addon
instalado pelo Contact Center e não habilita uma opção no compositor.

A versão instalada e o upstream consultado não oferecem envio de carrossel. A extensão
acrescenta dois endpoints à mesma autenticação das rotas de mensagens:

- `GET /chat/capabilities`: identidade, limites e `experimental: true`.
- `POST /chat/send/carousel`: imagens, textos e botões em uma mensagem nativa.

O patch altera somente o registro dessas rotas. O handler e seus testes ficam em
arquivos adicionais; não há cópia permanente do upstream ou atualização da dependência
whatsmeow. A preparação exige o SHA-256 do arquivo upstream inspecionado.

## Contrato do protótipo

Enviar `Content-Type: application/json` e a credencial de sessão no cabeçalho `token`.
Não colocar credenciais em URLs ou exemplos versionados.

```json
{
  "Phone": "5511999999999@s.whatsapp.net",
  "Id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "Body": "Conheça as opções",
  "Cards": [
    {
      "Body": "Opção A",
      "Image": "data:image/jpeg;base64,<imagem A completa>",
      "Buttons": [{"type": "reply", "title": "Quero esta", "id": "opcao-a"}]
    },
    {
      "Body": "Opção B",
      "Image": "data:image/png;base64,<imagem B completa>",
      "Buttons": [
        {"type": "cta_url", "title": "Ver detalhes", "url": "https://example.com"}
      ]
    }
  ]
}
```

Os dados acima são ilustrativos; não usar esse número ou ID em um envio real. Cada
tentativa lógica deve ter um ID gerado e registrado antes do despacho.

| Campo             | Limite local proposto                                                                  |
| ----------------- | -------------------------------------------------------------------------------------- |
| Destinatário      | JID direto canônico PN ou LID; sem grupos, aliases numéricos ou broadcast              |
| ID da mensagem    | 32 caracteres hexadecimais maiúsculos, obrigatório                                     |
| Texto principal   | 1–1.024 caracteres Unicode                                                             |
| Cartões           | 2–10, sem omissão ou truncamento silencioso                                            |
| Texto do cartão   | 1–160 caracteres Unicode                                                               |
| Imagem do cartão  | JPEG/PNG em base64; até 5 MiB, 4.096 px por dimensão e 16 MP                           |
| Total de imagens  | Até 10 MiB de bytes decodificados                                                      |
| Corpo HTTP        | Até 15 MiB                                                                             |
| Botões por cartão | 1–3; rótulos de até 20 caracteres                                                      |
| Resposta          | `type: reply`, `id` de até 200 caracteres, único entre todos os cartões                |
| Link              | `type: cta_url`, `url` HTTPS, sem credenciais/espaços, porta 443, até 2.048 caracteres |
| Telefone          | `type: cta_call`, `phone_number` E.164 com `+` e até 15 dígitos                        |

Esses limites são escolhas conservadoras do protótipo. Não representam suporte
homologado do WhatsApp. O handler rejeita campos desconhecidos, chaves duplicadas, ações
misturadas e imagem corrompida. Não aceita URLs de imagem, caminhos locais, contexto de
reply ou protobuf arbitrário.

## Envio e recuperação

Todas as entradas e imagens são validadas antes de qualquer upload. Após os uploads, o
handler constrói um `InteractiveMessage.CarouselMessage` com cartões
`NativeFlowMessage`, acrescenta os nós `biz` explícitos e chama `SendMessage` uma vez. O
evento e o histórico de sucesso usam o mesmo ID.

| Resultado                                   | Interpretação                                                                        |
| ------------------------------------------- | ------------------------------------------------------------------------------------ |
| `success: true`, `delivery_state: accepted` | ACK correlacionado pelo transporte; não comprova exibição no destinatário            |
| `delivery_state: not_sent`                  | A mensagem final não foi enviada; podem ter ocorrido uploads de imagem               |
| `delivery_state: unknown`                   | O despacho começou, mas não houve confirmação confiável; não repetir automaticamente |
| Falha/timeout sem corpo confiável           | Tratar como incerto; não inferir ausência de envio pelo HTTP                         |

O ID não cria deduplicação durável na API. Um segundo POST pode repetir o envio.
Idempotência e decisão sobre repetição continuam sendo responsabilidades da outbox que
venha a consumir a extensão.

Existe uma pendência adicional: o retry interno da versão whatsmeow fixada não preserva
os `AdditionalNodes` fornecidos no envio original. Não aprovar recuperação do carrossel
sem ensaio real desse caminho. Nenhum fork adicional ou alteração de protocolo foi
introduzido para contornar essa limitação sem evidência.

## Preparar e testar

Requisitos: Python 3.10+, Git, Go compatível com o `go.mod` fixado e uma pasta de
trabalho vazia fora dos checkouts de runtime. O comando abaixo apenas prepara fontes e
evidência; não instala, inicia, conecta ou envia nada.

```bash
python3 operations/wuzapi-carousel/prepare.py \
  --download \
  --archive /tmp/carousel-lab/upstream.tar.gz \
  --output /tmp/carousel-lab/source

cd /tmp/carousel-lab/source
go test -count=1 -run Carousel ./...
go test -count=1 ./...
go build -trimpath -o ../wuzapi .
```

Os testes de carrossel usam imagens sintéticas e transporte simulado. Não pareiam conta,
enviam mensagens ou fazem upload de mídia no WhatsApp. A preparação recusa árvore já
existente e arquivo upstream com hash divergente. Para uma nova revisão, preparar outra
pasta e guardar o novo `*.prepared.json`.

O [procedimento de homologação](acceptance.md) define a instância separada, o pareamento
humano e as provas necessárias antes de integrar a opção ao Contact Center. Não executar
o binário sobre o banco/volume de uma instância compartilhada.

## Fontes inspecionadas

- [Rotas WuzAPI fixadas](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/routes.go).
- [Handler existente de botões](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go#L2139).
- [Comparação com o upstream consultado](https://github.com/asternic/wuzapi/compare/9487eca9a40f292d19953a44983979c85d91ccce...919c72c9750b2a1eedf0fcf9c9592f05fe46f61c).
- [Representação protobuf do carrossel](https://github.com/tulir/whatsmeow/blob/b572e5bcb92bbc285b68cb6d6540da3093330e04/proto/waE2E/WAWebProtobufsE2E.proto#L361).
- [Caminho de retransmissão](https://github.com/tulir/whatsmeow/blob/b572e5bcb92bbc285b68cb6d6540da3093330e04/retry.go#L379).

Os arquivos Go desta extensão usam licença MIT, compatível com o upstream. Não
distribuir uma versão experimental como substituta de uma release homologada.
