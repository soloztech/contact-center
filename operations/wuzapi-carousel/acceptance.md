# Homologação do carrossel WuzAPI

Este procedimento acompanha a extensão experimental desta pasta. Compilar o protobuf e
obter HTTP 200 não comprova que o cliente WhatsApp recebeu um carrossel horizontal. A
opção no Contact Center permanece indisponível até essa prova.

## Ambiente e identidade

- Usar uma instância própria, com diretório de dados, banco SQLite e credenciais novos.
  Fixar o commit upstream e o hash dos arquivos da extensão.
- Vincular um dispositivo novo pelo procedimento normal do WhatsApp. Não copiar nem
  abrir simultaneamente os arquivos de sessão da instância compartilhada. O número da
  caixa Lucas pode ser usado como dispositivo vinculado adicional; isso depende do
  pareamento pelo operador e de uma vaga de dispositivo.
- O destinatário de teste indicado pelo usuário é Giulia Zotelli. Confirmar a identidade
  pela agenda da caixa Lucas; não deduzir nem inventar um número.
- Manter a API apenas em loopback. A fase sem sessão deve usar rede isolada; a fase de
  WhatsApp real precisa de saída de rede, sem publicar portas ou receber callbacks do
  ERP.
- Não atualizar o WuzAPI compartilhado do SERVIDOR04 durante este ensaio. As sessões
  Lucas e Comercial04 compartilham esse processo com outras sessões.

## Prova de transporte antes da integração

1. Consultar a rota de capabilities autenticada e conferir a identidade e os limites da
   extensão. O campo experimental não é uma capability aprovada do Contact Center e não
   deve ser copiado para `capabilities_json`.
2. Preparar dois cartões com imagens sintéticas distintas e texto identificando
   claramente o teste. Usar ações de resposta com IDs diferentes e opacos. Guardar uma
   identificação estável para a mensagem antes da chamada HTTP.
3. Enviar uma única requisição. Registrar apenas o ID, os hashes das imagens, horários,
   estado da resposta e contadores. Não registrar token, QR de sessão, conteúdo pessoal
   ou mídia em base64 em evidências versionáveis.
4. Comprovar no WhatsApp do destinatário uma única mensagem, navegação horizontal, ordem
   correta, imagens e textos completos. Confirmar separadamente WhatsApp Web e
   aplicativo móvel; registrar o cliente e sua versão.
5. Selecionar uma resposta em cada cartão. Conferir o ID opaco da seleção e o contexto
   da mensagem de origem. Receber um texto semelhante sem identidade correspondente não
   aprova correlação.
6. Homologar ações de URL e telefone em um ensaio separado, sem iniciar ligação ou
   executar uma ação comercial. Homologar os limites maiores somente depois do exemplo
   mínimo; dez cartões é um teto local proposto, não um limite provado do transporte
   instalado.
7. Validar recebimento após indisponibilidade e pedido de retransmissão do cliente. Na
   dependência fixada, o caminho interno de retry reconstrói a mensagem sem os
   `AdditionalNodes` do envio original. Os nós `biz` usados pelo carrossel podem ser
   perdidos nessa etapa. Essa limitação é uma pendência do transporte: um recebimento
   inicial correto não aprova sua recuperação. Ver
   [o caminho de retry fixado](https://github.com/tulir/whatsmeow/blob/b572e5bcb92bbc285b68cb6d6540da3093330e04/retry.go#L379).

Timeout ou erro após `SendMessage` deve resultar em envio **incerto**. Consultar o
destinatário e os eventos correlacionados antes de decidir qualquer novo envio. Manter o
mesmo ID não prova deduplicação no servidor ou no cliente WhatsApp. A extensão não
autoriza repetição automática, nem oferece armazenamento durável de idempotência.

## Falhas que o teste local deve demonstrar

- Autenticação obrigatória e caminhos/métodos estritos.
- Corpo HTTP limitado; campos desconhecidos, dados inválidos e limites excedidos
  rejeitados antes de upload ou envio.
- Nenhum download de URL fornecida pelo cliente. Imagens somente em data URI JPEG/PNG,
  conferidas pelos bytes e dimensões.
- Falha de uma imagem impede a mensagem final. Uploads já realizados podem existir no
  provedor; nenhuma imagem deve virar uma mensagem avulsa.
- Um envio final por chamada, ID preservado e classificação conservadora de erro após
  despacho. Mensagem parcial ou omissão silenciosa de cartão reprova o teste.
- Cancelamento interrompe o trabalho antes dos próximos uploads e do envio final.

## Integração do Contact Center após aprovação visual

Reutilizar o contrato de mensagens estruturadas, a outbox e o upload privado já
existentes. A capacidade deve ser específica da conexão que executa a extensão
homologada; não liberá-la globalmente por ser WhatsApp ou WuzAPI.

O contrato neutro deverá associar cada cartão à mídia autorizada da mensagem. Não
aceitar IDs de anexos arbitrários, URLs públicas de anexos privados ou JSON do provedor
enviado pelo navegador. Validar empresa, conversa, usuário, tamanho agregado e
integridade antes de admitir uma única operação na outbox.

O compositor deve preservar o rascunho por conversa e cancelar uploads removidos ou
substituídos. Uma resposta de upload tardia não pode alcançar outra conversa. O envio
existente de múltiplos anexos cria mensagens individuais e não serve como implementação
de carrossel. Agendamento e notas internas mantêm suas restrições.

Validar a timeline, as imagens autorizadas e as respostas dos botões, além de repetição
do comando, falha durante upload e envio incerto. Só então habilitar o editor de cartões
na caixa piloto. Selecionar produtos do Odoo é uma extensão posterior; não é necessário
para provar o transporte nativo solicitado.

## Encerramento

Encerrar somente a instância de teste. Revogar o dispositivo de teste pelo fluxo normal
após a homologação e tratar seu banco/token como segredo. Nenhum documento fiscal,
configuração de caixa do ERP ou sessão compartilhada integra este ensaio. O relatório
final deve distinguir testes locais, recebimento real e integração no Contact Center,
com aprovado/pendente em cada camada.
