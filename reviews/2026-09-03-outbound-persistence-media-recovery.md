# Persistência outbound e mídia JPEG-documento — validação R6

- Data: 2026-09-03 (release concluído em 2026-09-04 UTC)
- Ambiente: SERVIDOR05, `odoo16-teste.soloz.com.br`
- Produção: **não acessada e não alterada**
- Estado: **corrigido, implantado e registros afetados recuperados**

## Veredito

Os dois sintomas apresentados na interface tinham causas independentes e foram
corrigidos na origem:

1. onze mensagens amarelas haviam sido aceitas pela WuzAPI, mas a confirmação local
   falhou com `SerializationFailure` depois do limite externo; e
2. duas imagens JPEG chegaram como `documentMessage`. A WuzAPI recusava a derivação via
   `/chat/downloaddocument` com `invalid media hmac`, embora os mesmos metadados fossem
   válidos via `/chat/downloadimage`.

Nenhuma mensagem foi reenviada durante a recuperação. Ao final, as onze outboxes estão
`done`, as onze projeções estão `sent`, todas possuem ID externo e identidade de
participante do grupo. As duas mídias estão `ready`, possuem anexo privado e suas rotas
autenticadas retornam `200`, `image/jpeg` e o tamanho exato esperado.

## Correções

### Confirmação de envio sem duplicidade

- Envio, `mark_seen` e `mark_read` deixaram de incrementar a revisão de topologia da
  conta em operações rotineiras. O fence operacional usa `account FOR SHARE` e
  `connection FOR UPDATE`, sem reescrever a linha quente da conta.
- A finalização posterior ao provider readquire a ordem canônica
  `account -> connection -> outbox` antes das projeções de grupo, binding e evento. Isso
  elimina a inversão `connection -> account FK` encontrada na revisão adversarial.
- Quando o provider já retornou sucesso e somente a persistência local falha por
  `SerializationFailure` ou `DeadlockDetected`, o core pode reconciliar localmente em
  até três transações novas. Esse caminho nunca instancia nem chama o adapter.
- A evidência precisa comprovar resposta positiva, ID externo válido, request/DTO,
  binding e escopo compatíveis, além de `queue_job_uuid == dispatch_job_uuid`. Evidência
  ausente ou conflito de ID preserva `uncertain`.
- A ação administrativa é idempotente e foi usada uma vez por registro histórico. O
  operador R6 fixa URL LAN, UUID do banco, UID, versões e IDs antes de qualquer write.

### JPEG classificado como documento

- O fallback só existe para `document` com MIME original JPEG ou PNG.
- Ele só é ativado por HTTP `500` com envelope WuzAPI estrito e causa terminal exata
  `invalid media hmac`.
- Há no máximo uma segunda chamada, exclusivamente a `/chat/downloadimage`.
- A resposta é validada como imagem, com teto de 16 MiB, MIME igual ao original, tamanho
  exato e SHA-256 exato. PDF, vídeo, erro genérico, MIME divergente, conteúdo excessivo
  ou hash divergente continuam falhando fechados.
- O resultado permanece um `MediaDownloadResult` do documento canônico; não há troca do
  tipo de domínio nem duplicação de mensagem.

## Release e evidência

Versões publicadas:

| Addon                   |        Versão |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.1` |
| `contact_center_wuzapi` | `16.0.1.29.2` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.1` |
| `contact_center_ui`     | `16.0.1.26.4` |

Release atômico:
`scans/raw/20260903-odoo16-contact-center-delivery-media-recovery-atomic-release-r6/release/20260904T011425050493Z/summary.json`.

| Gate                        |                                                          Resultado |
| --------------------------- | -----------------------------------------------------------------: |
| Árvore implantada           | `0bde1bec9975bbd80ebbca5affceba4def1c45a5960cb3e7d45d3b6a3e8a3fde` |
| Arquivos verificados        |                                                                298 |
| Base isolado                |                                                        **497/497** |
| WuzAPI isolado              |                                                        **212/212** |
| Integração                  |                                                        **884/884** |
| QUnit UI minificado         |                                         **144/144**, **1248/1248** |
| QUnit UI `debug=assets`     |                                         **144/144**, **1248/1248** |
| Viewer base, ambos os modos |                                                 **4/4**, **15/15** |
| Upgrade offline             |                                                         status `0` |
| HTTP interno/público        |                                                      `200` / `200` |
| Rota Traefik                |                                             restaurada byte a byte |

Recuperação aplicada:
`scans/raw/20260903-odoo16-contact-center-delivery-media-recovery-r6/20260904T012129255038Z/summary.json`.

Prova de idempotência posterior:
`scans/raw/20260903-odoo16-contact-center-delivery-media-recovery-r6/20260904T012606584234Z/summary.json`.

O UiDTO mostrou os onze itens com `delivery_state=sent` e sem incerteza. Nas duas
mensagens do contato de laboratório, mostrou mídia `ready` e `content_url`; as rotas
autenticadas retornaram os JPEGs com 316.023 e 343.642 bytes.

## Limites conscientes

Há dezoito falhas históricas de mídia em outras conversas, não relacionadas a estes dois
registros. Nove excedem o teto explícito de 50 MiB, seis falham a integridade de
tamanho, duas registram colisão de anexo e uma é um HTTP 500 antigo. Elas não foram
alteradas nem mascaradas por esta correção e devem ser avaliadas separadamente por
política de tamanho, deduplicação ou nova evidência do provider.
