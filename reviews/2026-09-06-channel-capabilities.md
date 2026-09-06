# Cartões e capacidades por canal

## Divisão de responsabilidades

O núcleo mantém um conteúdo tipado de mensagem (`structured_content`), sem endpoints ou
payloads nativos de provedores. Os tetos do contrato neutro limitam o tamanho dos dados
processados; não representam a capacidade de uma API externa.

Cada adaptador declara `outbound_structured_content`, um mapa de tipo de cartão para
suas restrições. Esse contrato descreve **envio**. A recepção usa o normalizador do
adaptador e continua independente: receber uma seleção ou um compartilhamento não
habilita seu envio. Conversas diretas e grupos mantêm escopos de capacidades separados.

O compositor oferece somente tipos e modalidades declarados. A admissão valida o
conteúdo contra o contrato neutro, contra as capacidades da conexão e contra as
restrições adicionais do adaptador. A fila revalida antes do envio. Não há conversão
automática de cartão incompatível em texto, nem conversão silenciosa de botão URL em
botão de resposta quando o canal muda.

## Contrato de saída

Todos os tipos declaram `body_mode` (`required`, `optional` ou `none`) e
`max_body_length`. As demais restrições são próprias de cada tipo:

| Tipo       | Restrições declaradas                                                                                         |
| ---------- | ------------------------------------------------------------------------------------------------------------- |
| `buttons`  | Quantidade, modalidades (`reply`, `url`, `phone`) e comprimentos de título, rodapé, rótulos e IDs.            |
| `list`     | Quantidade de seções e opções no total; comprimentos de título, rodapé, botão, seção, opção, descrição e IDs. |
| `contacts` | Quantidade de contatos, telefones e emails; comprimentos dos campos.                                          |
| `location` | Comprimentos de nome/endereço e indicação de suporte a localização ao vivo.                                   |

Capacidade ausente ou inválida não habilita envio. A chave anterior, uma lista chamada
`structured_content` dentro das capacidades, não é interpretada. Esta é uma alteração do
candidato greenfield anterior à primeira release; não existe camada de compatibilidade
com aquela representação intermediária.

O formulário continua oferecendo um contato por cartão, uma seção de lista e localização
estática. Descrever restrições no contrato não cria recursos novos de interface nem
implementação de transporte em outros adaptadores.

## Provedores implementados

WuzAPI conserva seus limites de envio: três botões, dez opções de lista, um contato e
localização estática. Sua rejeição de coordenada zero permanece no adaptador porque zero
é uma coordenada válida no contrato neutro. Os limites atuais de texto e rótulos também
permanecem iguais para esse provedor.

O adaptador Meta continua sem anunciar cartões de saída. Isso descreve a cobertura do
nosso adaptador, não uma afirmação sobre todas as funcionalidades das plataformas
Messenger e Instagram.

Quando um cartão não tem corpo enviado, a prévia legível existe na projeção da conversa.
Ela não é inserida como legenda no comando. Um adaptador com suporte a corpo opcional
pode preservá-lo sem alterar o significado da mensagem.

## Homologação e release

Em banco de laboratório já existente, as capacidades armazenadas precisam ser
atualizadas pelo fluxo normal de verificação da conexão antes da homologação. A ausência
do novo contrato deve manter os cartões indisponíveis até essa atualização.

A homologação real deve registrar a revisão dos dois repositórios, versão do provedor,
conta/conexão de teste, destinatário de teste, comando, ID do provedor e resultado
observado no cliente destinatário. Exercitar os três tipos de botão, suas respostas
aplicáveis, lista, contato, localização e mídia. Um HTTP de sucesso sozinho não comprova
que o cartão foi apresentado corretamente.

As evidências desta etapa ficam em `scans/raw/20260906-centers-channel-capabilities/` no
repositório de infraestrutura.

## Resultados locais

- Instalação limpa dos 24 addons: **1.727 testes**, zero falhas e zero erros.
- Atualização subsequente do conjunto: concluída sem erro.
- Chromium: **190 testes / 1.607 assertions por modo** (minificado e debug assets),
  incluindo 172 testes da interface Contact Center.
- Gates dos procedimentos de release: **109 testes e 36 subtests**, aprovados.
- Ensaio operacional isolado: 4.800 mensagens e 200 controles aceitos sem erro; 100
  pares simultâneos com uma duplicata reconhecida por par. Vinte conexões retomadas, com
  uma chamada ao provedor por comando recuperado. Os três cenários ambíguos permaneceram
  incertos sem repetição; nenhuma fila ativa ou falha ficou no ensaio.

A revisão cruzada encontrou e corrigiu dois problemas adicionais na interface. O replay
de uma admissão incerta conserva seu conteúdo e UUID mesmo quando as capacidades mudam,
enquanto novos envios continuam sujeitos ao contrato atual. A contagem de caracteres dos
cartões agora usa pontos Unicode, igual ao servidor, sem recusar emojis válidos por
contar cada par UTF-16 duas vezes.

O ensaio operacional reutiliza as fases do harness canônico, em rede isolada, com
provedor simulado. Recalcula os hashes das fontes atuais; não reutiliza o inventário
antigo. Ele comprova a recuperação funcional, não a capacidade do hardware do
laboratório ou de produção.

## Prova de transporte real

O usuário indicou a caixa Lucas e o contato Giulia Zotelli. A leitura do laboratório
confirmou uma única conversa direta com afinidade de conta/conexão. O WuzAPI em uso
corresponde ao commit fixado. Seu acesso por IP LAN, preservando hostname, SNI e
verificação TLS, permite executar a prova no runtime local sem alterar o ERP, os
callbacks ou as assinaturas de webhook do laboratório.

O plano é limitado a sete mensagens identificadas como teste, admitidas no banco local
dedicado e despachadas pelos jobs correspondentes. A preparação privada contém apenas os
dados da conexão e do destinatário autorizados e fica fora do Git. O plano exige backup
pareado de banco/filestore e interrompe diante de falha ou ambiguidade, sem repetição
automática. O resultado desta prova será registrado após sua execução.

Essa prova de transporte não aprova a implantação do ERP. O laboratório permanece com a
fonte anterior e com pouca memória disponível para um runtime adicional; a janela de
produção anterior continua cancelada.
