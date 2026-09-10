# Ações da conversa

As ações ficam na seta de opções de cada conversa na lista da caixa de entrada.

## Permissões por caixa

Em **Configuração → Caixas**, administradores configuram:

- **Mostrar origem aos agentes**: acesso à projeção da origem de aquisição.
- **Permitir excluir conversas aos agentes**: exclusão do histórico local.
- **Permitir ignorar contatos e grupos aos agentes**: suspensão do recebimento daquele
  contato ou grupo na caixa.

As opções começam desativadas para agentes. Administradores da Central sempre têm as
três ações disponíveis. Essa exceção não concede acesso a outras empresas nem substitui
a autorização da conversa. A verificação também ocorre no servidor.

## Excluir conversa

A confirmação exclui a conversa e suas mensagens da Central, incluindo notas internas,
anexos exclusivos do histórico e projeções operacionais relacionadas. Os registros de
contato, oportunidades e documentos comerciais são preservados. A origem de aquisição e
os eventos de negócio já registrados pelo Marketing também permanecem; os vínculos com o
histórico excluído são removidos.

A operação não apaga o histórico no aplicativo do provedor. Excluir não implica ignorar:
uma mensagem nova pode abrir outra conversa. O controle técnico de duplicidade de
eventos anteriores permanece sem o conteúdo das mensagens.

Nos webhooks Meta, o conteúdo do item compartilhado também é apagado quando ele pertence
exclusivamente à Central. Se outro módulo é consumidor do mesmo item, seu registro
compartilhado é preservado; a exclusão remove o histórico e a cópia operacional da
Central. Os demais itens do lote permanecem intactos.

## Ignorar contato ou grupo

Novas mensagens do contato ou grupo ignorado não serão persistidas pela Central de
Atendimento **nesta caixa**. O histórico existente permanece consultável. O mesmo
cliente pode continuar sendo atendido por outra caixa normalmente.

**Deixar de ignorar** permite receber as próximas mensagens. As mensagens descartadas
durante o período ignorado não são recuperadas.

O menu **Central de Atendimento → Conversas ignoradas** permite desfazer a regra mesmo
depois da exclusão do histórico, respeitando a permissão da caixa.

## Marcar como não lida

O estado de leitura é pessoal e fica no backend, no registro de participação
`mail.channel.member.seen_message_id`. Ele persiste ao atualizar a página e ao trocar de
dispositivo. A leitura de um agente não altera o marcador dos demais.

Se a conversa já tem mensagens não lidas, o marcador existente é preservado. Se está
toda lida, a última mensagem operacional volta a ser não lida. A interface fecha a
seleção para que a visualização aberta não desfaça imediatamente a ação. Uma conversa
vazia ou contendo somente notas internas não cria pendência de leitura.

O recibo de leitura enviado ao WhatsApp ou a outro provedor é um mecanismo separado.
Marcar como não lida na Central não desfaz um recibo já enviado.

## Notas de operação

Resolver, reabrir, assumir e trocar ou remover o responsável registram uma nota interna
com o autor da ação. O estado e a nota são gravados na mesma transação. Repetir uma
operação sem mudar o estado não cria outra nota. Essas notas não são enviadas ao cliente
e não aumentam o contador de mensagens não lidas.

## Iniciar conversa e resolver com motivo

O botão ao lado da pesquisa abre uma conversa por telefone na caixa escolhida.
O provedor confirma o número, e uma conversa existente é reutilizada. Essa ação
não envia mensagem. Durante a confirmação, o painel permanece aberto para
apresentar o resultado ou o erro; fechar com Escape ou clicar fora não descarta
uma operação em andamento.

Resolver exige selecionar um motivo e informar a justificativa. A decisão é
registrada em nota interna. Se a conversa mudar durante a decisão, confira-a e
reabra o diálogo; não se aceita silenciosamente uma revisão mais recente.
Os motivos são específicos de cada empresa operadora e podem ser mantidos pelo
supervisor. Os três motivos iniciais pertencem à empresa principal da instalação;
outras empresas configuram seus próprios motivos, sem cópia automática.

## Contato, empresa principal e empresas secundárias

Agentes podem criar e vincular contatos e empresas nas conversas às quais têm
acesso, inclusive números centrais. Essa autorização não concede administração
geral de Contatos nem acesso a caixas de outra equipe/empresa.

O painel apresenta a empresa principal e, abaixo, as secundárias. A principal
continua sendo a referência comercial para novas cotações. Adicionar uma
secundária não troca o cliente de documentos existentes nem amplia permissões.
Cada empresa tem sua própria ação de desvinculação. Remover uma empresa altera
o cadastro do contato e é informado na confirmação; a pessoa continua vinculada
ao atendimento. Para corrigir uma pessoa associada por engano, **Corrigir contato
vinculado** remove somente o vínculo com o atendimento, preservando seu cadastro
e suas relações com empresas.

Em **Configuração → Relacionamentos dos contatos**, o administrador pode
desativar novos vínculos secundários sem apagar os existentes.

## Mídias, datas e prévias

O dia em leitura permanece no topo da área de mensagens ao rolar. O menu de uma
mensagem usa rótulos curtos para download. Vídeos abrem em uma janela flutuante
com controles; o Picture-in-Picture do navegador fica disponível quando suportado.

Prévias de links reutilizam `mail.link.preview` e sua configuração nativa
`mail.link_preview_throttle`. A fila gera metadados para até três URLs públicas
por mensagem, sem modificar o conteúdo enviado ao cliente. Páginas sem Open Graph,
indisponíveis ou fora dos limites continuam como links normais. A consulta usa
DNS validado, IP fixado durante a conexão, TLS verificado, redirecionamentos
validados e corpo limitado; a resolução DNS segue os limites do resolver do SO.

## Identidade própria em chamadas WuzAPI

A saúde da conexão registra o par telefone/LID da própria caixa. Em um aceite
feito pelo celular da caixa, `From` pode ser essa identidade local; o adaptador
seleciona então o participante remoto em `CallCreator`.

Prova ausente/obsoleta ou bloqueio por identidade divergente mantém chamadas em
tentativa posterior, até o limite normal da fila. Corrija a sessão e confirme
novamente sua saúde antes de reprocessar os eventos que esgotaram tentativas.
HTTP 429 na consulta complementar ao LID agora alimenta o cooldown já existente.
