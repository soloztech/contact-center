# Mídia, interações e cartões — implementação de pré-release

Esta entrega implementa a sequência aprovada após a análise de webhooks. Usa a fila, o
armazenamento privado e o controle de acesso existentes. O código é local; a ativação em
provedores e a implantação ainda exigem a validação operacional do conjunto publicado.

| Recurso                   | Comportamento implementado                                                                       | Limite relevante                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| Vários arquivos           | Seleção, prévia, remoção e envio individual; cada arquivo tem identificador próprio              | Até dez arquivos; não é álbum nativo do WhatsApp                              |
| Texto com vários arquivos | Uma legenda no primeiro arquivo que aceite legenda; mensagem de texto separada quando necessário | Sem repetir o texto em cada arquivo                                           |
| Mídia Messenger/Instagram | Upload privado seguido de envio por `attachment_id`                                              | Um anexo por mensagem, sem legenda; formatos e tamanhos por canal             |
| Botões WhatsApp           | Botões de resposta, URL HTTPS e telefone; respostas recebidas preservam ID e rótulo              | Até três botões; corpo até 1024 caracteres                                    |
| Listas WhatsApp           | Opções com IDs estáveis e descrições; resposta projetada na conversa                             | Dez opções; formulário oferece uma seção; contrato aceita até dez             |
| Contatos WhatsApp         | Cartão com nome, telefones e emails; campos vCard limitados e escapados                          | Um contato por envio; até dez contatos recebidos                              |
| Localização WhatsApp      | Coordenadas, nome e endereço; link para mapa                                                     | Localização estática; WuzAPI fixado rejeita coordenada zero no envio          |
| Compartilhamentos Meta    | Cartão de post, reel, story ou link; mídia privada quando há descritor reconhecido               | Permalinks públicos canônicos; URLs privadas não aparecem no DTO da interface |
| Vídeo circular recebido   | Vídeo reproduzível com o caminho privado de mídia existente                                      | Não oferece gravação/envio de vídeo circular                                  |

## Integridade do envio

`structured_content` é um objeto pequeno com tipos e campos permitidos. Não é um motor
genérico de blocos, um payload livre do provedor ou um interpretador de ações. IDs
recebidos são dados de correlação. A interface exibe os rótulos sem executar IDs.

A admissão confere o conteúdo, a capacidade do canal e as limitações do adaptador antes
de criar a mensagem. O identificador da requisição inclui o cartão na comparação de
repetição. Reutilizar um identificador com opções diferentes é erro. A fila compara o
comando com o cartão persistido e verifica novamente as capacidades. O reenvio de falha
definitiva cria uma tentativa própria e preserva o conteúdo; não reabre o comando
original.

Assinaturas automáticas não alteram cartões. Contato/localização exigem o corpo vazio
porque esses endpoints não possuem legenda; o núcleo gera uma prévia legível. Conteúdo
apagado com política de ocultação também oculta o cartão na interface.

No envio Meta, repetir um upload que perdeu a resposta pode criar um objeto de mídia
órfão no provedor, mas não envia outra mensagem ao destinatário. Uma falha ambígua no
envio final permanece incerta, impedindo repetição automática. A janela de resposta é
verificada novamente após o upload. Detalhes e fontes primárias estão em
[contratos Meta](2026-09-06-rich-messaging-meta.md).

## Escopo da atualização

É necessário atualizar o módulo `contact_center_base` para criar o campo JSON do binding
e atualizar os assets da interface. As versões seguem `16.0.1.0.0` durante a preparação
da primeira release limpa.

Esta entrega não enriquece mensagens históricas já projetadas, não reprocessa eventos
`unsupported`, não oferece álbum nativo, templates da Cloud API, votos de enquete ou
semântica de visualização única. Eventos de coordenação de álbum continuam sem criar
mensagem humana própria; seus arquivos filhos são recebidos. Botões/listas de saída
estão habilitados no WhatsApp pelo adaptador WuzAPI, conforme o contrato da revisão
fixada `9487eca9a40f292d19953a44983979c85d91ccce`.

A comparação funcional com toda a plataforma RD continua sendo um roteiro maior; esta
implementação cobre os formatos de conversa aprovados nesta etapa.

## Validação

Validação concluída no ambiente sintético local:

- Instalação limpa dos 24 addons: **1.719 testes, zero falhas e zero erros**.
- Atualização subsequente de todos os módulos: concluída sem erro.
- Chromium: **184 testes / 1.479 assertions por modo**, nos assets minificados e de
  depuração; 166 desses testes pertencem à interface Contact Center.
- Gates locais de release: **109 testes e 36 subtests**, todos aprovados.
- Pre-commit dos dois repositórios aprovado, incluindo verificações obrigatórias.
- QA visual com dados sintéticos: seis formatos recebidos, duas imagens com UUIDs
  distintos e legenda única, botão de resposta e correção de localização recusada.
  Controles também exercitados em viewport de 390 px, sem overflow horizontal.

A validação visual encontrou e corrigiu duas regressões: o compilador LibSass
interpretava `min()` com unidades incompatíveis, e o microfone escondia o botão de
contato/localização sem texto. A regressão de escolha do botão está na suíte. Os testes
novos foram incluídos no filtro comum da CI e seus mínimos atualizados.

Os arquivos e cartões pendentes sobrevivem à troca de conversa na interface aberta; o
lote não é restaurado após recarregar o navegador. Nenhuma recuperação histórica ou
envio a contatos reais foi executado. A homologação com credenciais/permissões reais dos
provedores e a janela de produção permanecem etapas externas.

Evidências locais: `scans/raw/20260906-centers-rich-messaging/`; capturas da interface:
`output/playwright/centers-rich-20260906/` no repositório de infraestrutura. Nenhum
envio para contatos reais faz parte dos testes.
