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
