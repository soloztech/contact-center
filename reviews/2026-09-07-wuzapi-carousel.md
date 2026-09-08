# Carrossel WuzAPI — análise e protótipo de transporte

## Estado

**Protótipo compilado e validado localmente; transporte real e integração no Contact
Center pendentes.** Nenhuma opção de carrossel foi habilitada no compositor. Não há
alteração nos addons instaláveis, migração de banco, publicação de imagem, deploy remoto
ou envio de mensagem nesta etapa.

O pedido é um carrossel horizontal nativo com imagens, textos e botões. O envio
existente de vários anexos gera mensagens separadas e não atende esse requisito.

## Constatações

O WuzAPI em uso no SERVIDOR04 é `asternic/wuzapi` v1.0.8, commit
`9487eca9a40f292d19953a44983979c85d91ccce`. As caixas Lucas e Comercial04 usam o mesmo
processo. A inspeção encontrou também sessões conectadas sem callback de laboratório,
portanto a instância inteira não foi tratada como descartável.

As fontes completas desse commit e de `main` `919c72c9750b2a1eedf0fcf9c9592f05fe46f61c`
não implementam endpoint de carrossel ou envio de protobuf arbitrário. O handler de
botões recebe um cartão; enviar um campo `Cards` adicional a esse endpoint não produz um
carrossel. Atualizar somente para o upstream consultado não acrescenta o recurso.

A dependência whatsmeow contém `InteractiveMessage.CarouselMessage`, mas não acrescenta
automaticamente os nós de transporte `biz` necessários ao caminho interativo usado pelo
WuzAPI. Além disso, o retry interno não preserva os `AdditionalNodes` do envio original.
A representação protobuf é evidência de viabilidade para um protótipo, não de
entrega/renderização ou recuperação homologadas.

## Implementação

A pasta [operations/wuzapi-carousel](../operations/wuzapi-carousel/README.md) contém um
acréscimo isolado ao upstream fixado: um handler Go, seus testes, duas rotas
autenticadas e preparação verificável por hash. Não há dependência Go adicionada ao Odoo
nem fork integral incorporado ao repositório.

O endpoint admite 2–10 cartões JPEG/PNG com texto e ações de resposta, URL ou telefone,
conforme os limites conservadores documentados. Valida a mensagem inteira antes de
upload, recusa URLs de mídia e envia uma única mensagem final. Mantém identidade de
correlação e classifica falhas após despacho como incertas. Não promete deduplicação
durável ou repetição segura do POST.

## Evidências locais

Diretório de evidência operacional: `scans/raw/20260907-contact-carousel/`, na raiz do
repositório infra-ai-ops. Pesquisa primária:
`scans/raw/20260907-wuzapi-carousel-research/`.

| Verificação                         | Resultado                                                                     |
| ----------------------------------- | ----------------------------------------------------------------------------- |
| Preparação                          | Hash upstream fixado; arquivo adulterado e pasta existente recusados          |
| Suíte carrossel                     | 7 funções de teste e 27 subtestes aprovados                                   |
| Suíte completa WuzAPI com extensão  | 63 funções e 71 subtestes aprovados, sem falhas ou skips                      |
| Rede durante testes e build         | Namespace privado com apenas loopback                                         |
| Build                               | Go 1.25.14, Linux ARM64, sem alteração da árvore durante compilação           |
| Binário SHA-256                     | `38e2af6194bc150266e7df61cf15e01f35295b0e3f0f793cc4a1f530e8a77772`            |
| Inicialização HTTP                  | SQLite novo, saudável, zero usuários/sessões/conexões; encerramento conferido |
| Autenticação                        | Ambos os endpoints recusaram acesso anônimo com HTTP 401                      |
| Pre-commit                          | Aprovado para os arquivos da extensão                                         |
| Recebimento e navegação no WhatsApp | Pendente de pareamento e ensaio no destinatário                               |
| Retorno dos botões e retransmissão  | Pendente de ensaio real                                                       |
| Integração no compositor e timeline | Pendente da aprovação do transporte                                           |

## Próxima etapa concreta

Parear um dispositivo novo do WhatsApp Lucas no laboratório isolado e usar o
destinatário de teste já indicado, Giulia Zotelli. O
[roteiro de homologação](../operations/wuzapi-carousel/acceptance.md) define a prova
mínima e as condições de integração. O pareamento depende de ação no celular;
credenciais ou arquivos de sessão da instância compartilhada não devem ser copiados.

Depois da aprovação visual e da recuperação, integrar o contrato neutro de cartões,
mídia privada, outbox, capability por conexão, compositor e timeline. Não liberar a
capacidade globalmente para todas as conexões WuzAPI.

Classificação desta etapa: `backup_required: false`; somente fontes aditivas e um
runtime local novo. A reversão é retirar a pasta experimental e encerrar seu processo;
nenhum banco de ERP ou volume remoto precisa ser restaurado. Nenhum documento fiscal
autorizado foi alterado.
