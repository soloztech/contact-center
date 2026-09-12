# Proposta de retenção de mensagens e mídias

Data: 2026-09-11. Revisão de requisitos: 2026-09-12. Estado: **implementação em
validação; ativação desativada por padrão**.

## Implementação de 12/09

O serviço está em `contact_center_base/models/retention.py`, com barreira de entrada em
`retention_ingress.py` e proteção das dependências em `retention_dependencies.py`. O
adaptador WuzAPI identifica rota, data e alvos sem depender da normalização do corpo. As
configurações e exceções exigem supervisor/administrador e são auditadas no backend.

O cron processa um grupo por minuto, até 100 mensagens por lote. Antes da primeira
purga, indexa os eventos legados da caixa em lotes de 500. Cópias em citações e edições
são saneadas em etapas limitadas antes da exclusão do alvo. Anexos compartilhados são
preservados; consumidores comerciais sem contrato de expiração adiam o lote.

A interface inclui aviso do prazo, exceção por grupo e atualização da conversa aberta
após a limpeza, mantendo o estado de leitura de cada usuário. A simulação conta
mensagens e registros de mídia; não estima bytes físicos liberados. A coleta física de
arquivos continua sendo responsabilidade do Odoo.

Instalar esta versão não ativa nenhuma caixa nem executa uma limpeza inicial. A
evidência de implantação e os testes executados ficam no incidente operacional
`odoo16/incidents/2026-09-12-contact-center-retention.md` do repositório de
infraestrutura.

As seções abaixo preservam o estudo original e suas recomendações para evolução.

Solicitação: permitir, por exemplo, manter somente sete dias de mensagens em grupos com
muito volume, eliminando conteúdo antigo e mídias para controlar o crescimento do Odoo.
O estudo original não acessou a produção, não executou SQL no banco, não excluiu
registros e não criou cron.

## Recomendação

Vale implementar. Recomendo começar com uma política **desativada por padrão**,
configurada na caixa para **grupos**, com uma exceção por conversa. Conforme os
requisitos de 12/09, o prazo fica centralizado na caixa:

- Na caixa: ativar/desativar exclusão automática e manter por N dias, inicialmente sete.
- No grupo: seguir a regra da caixa ou preservar este grupo.
- Supervisor e administrador podem preservar um grupo diretamente no painel lateral.
- O aviso de exclusão mostra o prazo efetivo para todos que acessam o grupo.

Não haverá prazo diferente configurável por grupo no MVP. A exceção apenas desativa a
exclusão naquele grupo, sem alterar a caixa ou os demais grupos.

A rotina remove o **histórico vencido**, incluindo texto, versões editadas, reações,
prévias, mídia exclusiva e cópias operacionais dos webhooks. A conversa continua
existindo, com nome, participantes, marcadores e responsável, e recebe mensagens novas
normalmente. Excluir o grupo inteiro a cada semana perderia configuração e favoreceria a
recriação de conversas.

Manter contatos, empresas, oportunidades, cotações, pedidos, faturas, documentos
comerciais e evidência de origem do lead. São registros de negócio, não lixo do
histórico. Um PDF usado também em uma cotação continua existindo na cotação.

**“Tudo” deve significar todo o conteúdo vencido de propriedade do chat.** É necessário
conservar um controle técnico mínimo, sem texto nem mídia, para que webhooks repetidos
não recriem o que foi eliminado. Não prometer zero linhas residuais nem eliminação no
WhatsApp, dispositivos, backups ou outros sistemas.

## Configuração e painel lateral — requisitos de 12/09

### Configuração por caixa

Em **Configuração > Caixas > caixa selecionada**, acrescentar a seção **Retenção de
histórico**, acessível a quem já pode administrar a caixa:

- **Excluir histórico antigo de grupos:** desligado por padrão, inclusive nas caixas
  existentes. Instalar ou atualizar o módulo não ativa a regra.
- **Manter histórico por (dias):** inteiro positivo, valor inicial **7**, obrigatório
  quando a regra estiver ligada. Zero não significa excluir tudo.
- **Abrangência:** grupos dessa caixa; conversas individuais permanecem fora do MVP.
- Explicação: o prazo conta desde a data original de cada mensagem. Nova atividade no
  grupo não renova o prazo das mensagens antigas. Mídias seguem o mesmo prazo.

O cadastro do grupo, nome, participantes, responsável e marcadores são preservados,
mesmo que não reste nenhuma mensagem. Por isso o texto do produto deve dizer
**“mensagens e mídias”** ou **“histórico”**, evitando sugerir que o grupo inteiro será
excluído após sete dias.

Ativar a regra, reduzir o prazo ou voltar a aplicá-la em um grupo preservado torna
elegível também o histórico que já ultrapassou o prazo. A futura interface deve mostrar
isso antes de salvar, com a simulação de impacto prevista neste estudo. O salvamento da
configuração não executa a purga dentro da requisição do usuário.

### Aviso e exceção no painel do grupo

No painel direito da imagem fornecida, acrescentar **Retenção do histórico** logo após
**Dados do grupo**, antes de **Operação**. A seção mostra a regra efetiva a qualquer
usuário com acesso à conversa; somente supervisor e administrador veem o controle.

Quando ativa, mostrar um aviso persistente e discreto, sem modal a cada abertura:

> Mensagens e mídias deste grupo com mais de 7 dias serão excluídas da Central. O
> cadastro do grupo será mantido.

O número é obtido da configuração da caixa, sem texto fixo em sete. A ajuda detalha que
a limpeza ocorre na próxima execução da rotina após vencer o prazo; não promete
liberação física do disco no instante exato. Arquivos vinculados também a documentos ou
outras conversas seguem a proteção de compartilhamento descrita abaixo.

Com o painel fechado, manter um indicador compacto **Histórico: N dias** no cabeçalho da
conversa, que abre essa seção. Aviso e indicador pertencem ao Odoo; não são mensagens
automáticas enviadas ao grupo no WhatsApp.

O controle para supervisor/administrador será **Preservar histórico deste grupo**:

| Regra da caixa | Preservar grupo | Estado exibido no painel                                                                                 |
| -------------- | --------------- | -------------------------------------------------------------------------------------------------------- |
| Ligada         | Desmarcado      | **Exclusão automática após N dias**, com o aviso acima.                                                  |
| Ligada         | Marcado         | **Histórico preservado. Este grupo está isento da exclusão automática da caixa.**                        |
| Desligada      | Desmarcado      | **Exclusão automática desativada nesta caixa.**                                                          |
| Desligada      | Marcado         | **Exclusão automática desativada nesta caixa. Este grupo continuará preservado se a regra for ativada.** |

Marcar preservação interrompe exclusões futuras após a confirmação do servidor;
desmarcar volta a seguir a caixa. Ajuda do controle: **“Preserva as mensagens e mídias
deste grupo. Não recupera conteúdo já excluído.”** A exceção permanece salva quando a
caixa é desligada e ligada novamente e vale para todos os usuários, não apenas para quem
marcou. Ela é independente de silenciar, arquivar, resolver ou ignorar o grupo.

### Contrato para a futura implementação

- Persistir a política na caixa e a exceção no vínculo canônico caixa/conversa, não em
  estado local do navegador nem na conexão temporária do provedor. Aliases e merges
  devem preservar a exceção; em conflito dentro da mesma caixa, preservar prevalece.
- Validar supervisor/administrador e acesso à caixa/conversa no backend. Ocultar o
  controle para agentes não basta; uma chamada direta por agente deve ser recusada. Ser
  administrador do grupo no WhatsApp não concede permissão de supervisor no Odoo. A
  capacidade é própria da retenção, independente das flags de excluir ou ignorar.
- Retornar ao painel o estado efetivo calculado no backend, prazo e capacidade de
  alterar a exceção. Atualizar outras abas e usuários após commit; só confirmar a
  alteração visual quando o servidor tiver salvo.
- Mudança de política e exceção usa a mesma ordem de travas da rotina de retenção.
  Revalidar a regra dentro de cada lote, depois das travas, inclusive em jobs já
  enfileirados. Se uma purga já confirmou um lote antes da preservação, esse conteúdo
  não volta; depois da confirmação de preservação, novos lotes não podem excluir o
  histórico desse grupo.
- Registrar autor, data e estado anterior/novo da configuração, sem copiar conteúdo das
  mensagens e sem enviar aviso ao WhatsApp. Desativar a regra não reduz o limite de
  expiração já aplicado nem permite recriação do histórico por replay.

Esta revisão registra os três requisitos para implementação posterior. Não cria campos,
controles, jobs ou exclusões no teste ou na oficial.

## O que existe hoje, de fato

Inspeção do código local de `contact_center_base`, `contact_center_meta`,
`contact_center_crm`, `contact_center_kanban`, `meta_webhook_base` e
`marketing_center_contact_center`. O HEAD observado no Contact Center durante a inspeção
foi `61fdce58a774061666ff059db004e8bc0ad282b7`; outros agentes estavam trabalhando na
mesma árvore. O estado instalado de módulos opcionais e eventuais diferenças do OCB
precisam ser inventariados antes de uma implementação.

1. Já existe exclusão integral por conversa em
   [`delete_conversation`](../contact_center_base/models/conversation_actions.py). Ela
   serializa a política da caixa, impede exclusão durante envios de resultado incerto,
   cancela trabalhos pendentes conhecidos, trata anexos compartilhados entre mensagens e
   chama extensões de dependências.
2. Esse fluxo **não apaga o ledger de entrada**: `_erase_conversation_content` substitui
   envelope/DTO por um recibo vazio de conteúdo, bloqueado, conservando a chave de
   deduplicação. Isso evita repetição do evento já recebido.
3. A exclusão por provedor de uma mensagem tem outro significado. O modo `strike` pode
   manter `original_body`/`deleted_body`; mesmo a redação de conteúdo não equivale a
   retenção completa de todos os ledgers.
4. Existe limpeza de uploads expirados, limitada a 500 por execução, em
   [`media.py`](../contact_center_base/models/media.py). Isso não remove todo o
   histórico: o anexo consumido pertence à mensagem e deve acompanhá-la.
5. Meta já possui um cofre de URLs temporárias, uma limpeza dessas URLs e um hook de
   exclusão por conversa. O cofre conserva a linha sem a URL; o ledger Meta conserva
   recibos. O hook respeita consumidores compartilhados.
6. Não encontrei uma política temporal integrada que feche todos esses caminhos e
   atualize os cursores de leitura após apagar somente parte de uma conversa.

Portanto, não é suficiente agendar `mail.message.unlink()` por data, chamar
`delete_conversation()` para cada grupo, nem limpar somente `ir.attachment`.

## Mapa dos registros e dependências

Os nomes de tabela abaixo seguem `_table` explícito ou o mapeamento do modelo Odoo com
pontos substituídos por sublinhados. Tabelas de relação e extensões instaladas devem
entrar no inventário automatizado antes da implantação.

| Modelo / tabela principal                                                                                                              | Conteúdo ou dependência observada                                                                                                                                                        | Tratamento proposto                                                                                                                                                                                         |
| -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mail.message` / `mail_message`                                                                                                        | Corpo, data, autor, anexo e reação; liga-se à conversa por `model/res_id`, não por uma FK exclusiva do CC                                                                                | Excluir por ORM somente mensagens vencidas do canal autorizado, incluindo notas internas vencidas se a regra for “todo o histórico”.                                                                        |
| `contact.center.message.binding` / `contact_center_message_binding`                                                                    | FK em cascata da mensagem; `original_body`, `deleted_body`, snapshots de protocolo, conteúdo estruturado; `reply_to_binding_id` é `set null`; origem aponta `inbox.event` com `restrict` | Excluir com a mensagem; limpar também trechos citados de mensagens sobreviventes que tenham copiado o conteúdo vencido. Não apagar o texto novo escrito pelo autor sobrevivente.                            |
| `contact.center.message.mutation` / `contact_center_message_mutation`                                                                  | FK em cascata ao alvo; `new_text` e `details_json`; mutações são imutáveis por API comum                                                                                                 | Remover edições/reações/exclusões do alvo expirado via serviço próprio, após resolver comandos associados.                                                                                                  |
| `contact.center.delivery.event`, `contact.center.group.delivery.event`                                                                 | Recebimentos, estados e JSON; cascatas ao binding, porém Marketing pode referenciá-los com `restrict`                                                                                    | Remover projeções vencidas depois dos consumidores dependentes; não simular novas confirmações de entrega.                                                                                                  |
| `contact.center.media.binding` / `contact_center_media_binding`                                                                        | Cascata ao binding; `remote_locator_json`, nome, metadados, ponteiro para anexo com `set null`, UUID de download                                                                         | Cancelar/impedir download tardio, finalizar locator privado e excluir vínculo. A cascata do binding sozinha não prova exclusão do arquivo.                                                                  |
| `ir.attachment`, relação `message_attachment_rel` e filestore                                                                          | Relação com mensagens e dono genérico `res_model/res_id`; bytes podem ser compartilhados por vários registros                                                                            | Excluir somente anexos exclusivos do conjunto expirado. Transferir a propriedade para um sobrevivente quando necessário, antes do unlink nativo da mensagem. Deixar a coleta física ao Odoo.                |
| `contact.center.media.upload` / `contact_center_media_upload`                                                                          | FK ao canal; FK `restrict` ao anexo, `set null` à mensagem consumidora; TTL existente                                                                                                    | Limpar uploads vencidos do conjunto e seus anexos exclusivos; preservar upload pendente válido e consumo por mensagem sobrevivente.                                                                         |
| `contact.center.inbox.event` / `contact_center_inbox_event`                                                                            | Envelope, DTO, metadados, erros, dedupe por conexão; não tem hoje uma FK de conversa para todos os eventos                                                                               | Apagar conteúdo inclusive de eventos unsupported/dead, conservando recibo mínimo quando necessário; acrescentar rota/data indexadas para não varrer todos os JSON da caixa a cada ciclo.                    |
| `contact.center.outbox.command` / `contact_center_outbox_command`                                                                      | `command_json`, resolução/erros e chave idempotente; cascatas a mensagem/alvo/mutação; `retry_of_id` usa `restrict`                                                                      | Cancelar somente trabalhos ainda não despachados sob trava; preservar pendentes/incertos e cadeia de retry enquanto houver um sobrevivente. Remover/sanear terminais vencidos e proteger idempotência.      |
| `contact.center.scheduled.message` / `contact_center_scheduled_message`                                                                | Cópia `body`, UUID, ligação à mensagem/outbox com `set null`                                                                                                                             | Não excluir agendamento futuro. Limpar corpo e registro terminal correspondente à mensagem expirada; `set null` sozinho deixa uma cópia de texto.                                                           |
| `contact.center.internal.note.request` / `contact_center_internal_note_request`                                                        | FK `restrict` à nota, chave de requisição e hashes                                                                                                                                       | Liberar antes de excluir a nota e conservar somente a idempotência necessária.                                                                                                                              |
| `queue.job` / `queue_job`                                                                                                              | Argumentos, kwargs, resultado, erros/traceback e referência serializada a registros; sem cascata geral por mensagem                                                                      | Localizar por UUID **e** modelo/método/identidade do job, cancelar pendentes elegíveis e limpar terminais exclusivos. Não fazer limpeza global da fila.                                                     |
| `mail.link.preview` / `mail_link_preview`                                                                                              | Prévia nativa derivada da mensagem, URLs, título, descrição e imagem remota                                                                                                              | Remover junto do alvo; impedir job de prévia em voo de recriar conteúdo e invalidar interface.                                                                                                              |
| `mail.notification`, `mail.message.reaction`, `mail.tracking.value` e relações nativas                                                 | Projeções e possíveis cópias ligadas à mensagem, conforme módulos instalados                                                                                                             | Validar cascatas reais do OCB e ausência de órfãos por ORM; incluir explicitamente extensões que não acompanham unlink.                                                                                     |
| `mail.channel.member` / `mail_channel_member`                                                                                          | `seen_message_id`, `fetched_message_id`, `last_seen_dt`; contador de não lidas por membro                                                                                                | Preservar o limite de leitura por data+ID antes de apagar ponteiros; recalcular sobre sobreviventes sem marcar como lido algo ainda não lido.                                                               |
| `mail.channel`, `contact.center.channel.binding`, aliases e preferências                                                               | Última mensagem/data, acesso, responsável, tags, grupo, CRM, fixar/silenciar                                                                                                             | Preservar a conversa; recompor última mensagem/preview e contadores. Um canal vazio não deve mostrar uma prévia antiga.                                                                                     |
| `contact.center.delivery.watermark` / `contact_center_delivery_watermark`                                                              | Dois estados cumulativos por conexão/conversa; `details_json` pode guardar evidência                                                                                                     | Conservar cursor cumulativo mínimo; sanear detalhes antigos sem regredir entregas/leitura do provedor.                                                                                                      |
| `contact.center.group.profile`, participantes/aliases e avatar; identidade/alias/avatar direto                                         | Estado do grupo e identificação corrente, compartilhados com mensagens novas                                                                                                             | Preservar cadastro atual e avatar corrente. A limpeza de histórico não deve apagar a pessoa ou perder o nome do grupo.                                                                                      |
| `contact.center.followup.request`, `mail.activity`                                                                                     | Atividade e canal; rotinas e proteções próprias                                                                                                                                          | Preservar tarefa aberta e dados de negócio; limpar eventual cópia histórica exclusiva só com contrato específico. Não cancelar follow-up porque uma mensagem expirou.                                       |
| `contact.center.attribution.touchpoint`, `.identifier`                                                                                 | FK `restrict` a inbox e bindings; evidência append-only; hook existente solta vínculos de projeção                                                                                       | Preservar origem/campanha/data e recibo necessário. Limpar conteúdo de chat do envelope. Adaptar hook para uma seleção de mensagens, sem desligar toda a conversa do touchpoint.                            |
| `contact.center.crm.conversation.link`, `crm.lead`, `res.partner`, vendas e faturas                                                    | Vínculo por canal e registros comerciais independentes                                                                                                                                   | Preservar integralmente; não reaproveitar o hook de exclusão integral para retenção parcial.                                                                                                                |
| `contact.center.case`, `.crm.case.link` quando Kanban instalado                                                                        | Hook atual de exclusão integral remove projeções do canal                                                                                                                                | Não executar esse hook na retenção parcial; manter atendimento/pipeline e seu histórico de negócio.                                                                                                         |
| `contact.center.meta.media.locator` / `contact_center_meta_media_locator`                                                              | URL privada; FKs `restrict` a delivery/inbox; limpeza atual remove URL, não linha                                                                                                        | Expirar URL imediatamente quando a mensagem vencer; excluir recibo somente quando não afetar contratos Meta/dedupe.                                                                                         |
| `meta.webhook.delivery`, `.item`, `.dispatch`                                                                                          | Envelope saneado, payload do item, consumidor, jobs e hashes; imutáveis e com `restrict`                                                                                                 | Usar erasure do consumidor no item exclusivo; conservar recibo/roteamento. Não apagar uma entrega contendo itens de Lead Ads ou mensagens de outro consumidor.                                              |
| `marketing.contact.center.response.signal`, `.cursor`, `.episode`, `.response`                                                         | FKs `restrict` a mensagens/entregas; processamento incremental e eventos de negócio                                                                                                      | Hook específico para expiração parcial: consolidar métricas antes de liberar projeções antigas e preservar episódio aberto. Até existir esse contrato, bloquear candidatos dependentes e informar o motivo. |
| `marketing.attribution.contact.center.link` / `marketing_attr_cc_link`, `marketing.attribution.touchpoint`, `marketing.business.event` | Revisões imutáveis de atribuição/negócio; referências restritivas                                                                                                                        | Preservar os fatos de negócio; não deixar o replay/backfill reconstruir projeções do chat já expiradas.                                                                                                     |

### Pontos que exigem cuidado no código atual

- `_conversation_inbox_events()` varre o ledger da caixa em páginas de 500 e lê a rota
  de cada JSON. É razoável para exclusão pontual, não para um cron que repita a
  varredura para centenas de grupos. Projeção de rota e timestamp deve ser
  persistida/indexada na ingestão, com backfill limitado dos legados.
- O fluxo atual procura compartilhamento de anexos em outras `mail.message`. Antes de
  automatizar, o catálogo de consumidores instalados também deve reconhecer documento de
  negócio, catálogo, campo binário/attachment e outra mídia/upload. Se a propriedade não
  estiver provada, conservar e contabilizar.
- Jobs de link preview usam `identity_key` com mensagem/hash e não um `queue_job_uuid`
  no binding. O teste não pode procurar somente UUIDs explícitos.
- O hook Meta **já existe** e não deve ser duplicado. O saneador base guarda contagem de
  messaging no envelope da entrega; o conteúdo de messaging reside no `payload_json` do
  item. O erasure só limpa itens de consumidor exclusivo; itens compartilhados devem
  aparecer como exceção, nunca como “apagados”.
- Os hooks de Marketing e Kanban atuais atendem **exclusão integral**, não uma janela
  móvel. Invocá-los integralmente perderia projeções de mensagens novas.
- O histórico de edições, respostas citadas e comandos contém cópias além do corpo
  principal. “Mensagem apagada” na tela não é teste suficiente de retenção.

## Regra de tempo e comportamento de replay

Proposta: prazo baseado no **instante original da mensagem**, em UTC. Sete dias
significam 168 horas, não sete trocas de data local. O instante é imutável: editar,
baixar mídia, importar ou repetir webhook não reinicia o prazo.

Para mensagens recebidas usar timestamp válido do provedor. Para mensagem criada no
Odoo, usar a data original de criação/envio do fato. Se o protocolo não traz timestamp
utilizável, usar a primeira ingestão local, registrar a origem desse tempo e tratar a
deduplicação como caso especial. Nunca usar `write_date`.

Manter um **limite de expiração já aplicado** por caixa+conversa canônica, separado do
prazo configurado. Ele é monotônico, acompanha aliases/merges e não diminui quando
alguém desativa a regra ou aumenta o prazo. O que já foi eliminado não pode reaparecer
por importação ou redelivery. O limite não deve avançar por cima de mensagens antigas
ainda protegidas por envio ou processamento em andamento.

A ingestão autenticada consulta esse limite antes de criar conteúdo durável, projeção ou
download. Eventos comprovadamente anteriores ao limite retornam o ack adequado ao
provedor e apenas incrementam uma métrica/recibo sem conteúdo. Um receipt, edição ou
reação recente cujo alvo já expirou também não pode criar uma mensagem substituta. A
data do receipt não é a data original do alvo.

Para eventos sem tempo confiável, ecos e requisições de envio antigas, conservar
chave/identificador técnico escopado e motivo de expiração, **sem texto, nome de
arquivo, URL privada ou payload**. O ledger saneado existente é um ponto de partida de
menor manutenção; uma estrutura compacta de tombstones pode substituí-lo quando forem
liberadas as referências que hoje usam `restrict`.

Não propor simplesmente “apagar os hashes depois de 30/90 dias”: sem um limite máximo de
replay demonstrado por cada transporte, isso permite recriação. O limite temporal reduz
a necessidade de recibos por evento para mensagens com tempo confiável; os casos
ambíguos ainda precisam de recibo ou rejeição explícita. Existe uma escolha inevitável
entre apagar **toda** memória de um ID antigo e garantir que esse mesmo ID nunca seja
aceito novamente. Nenhuma garantia finita de replay foi presumida neste estudo.

Para evitar um produto complexo, lançar primeiro para grupos WuzAPI, cujo adaptador já
exige timestamp nos eventos de mensagem. Mesmo aí, chamadas, receipts, eventos
sintéticos e ecos precisam de testes próprios. Só habilitar outros transportes após
fechar seus contratos de erasure e consumidores.

## Execução proposta, com baixo impacto

Um serviço de retenção do próprio Contact Center, com um cron e extensões por módulo
opcional. Reutilizar validação/posse de anexos e mecanismos de exclusão existentes,
extraindo operações por conjunto de mensagens; não criar um motor genérico de políticas
para todo o ERP.

1. **Simular:** apurar por caixa/conversa quantidade de mensagens, anexos e bytes,
   intervalos, payloads, dependências e exceções. Nada de conteúdo pessoal no relatório;
   distinguir bytes lógicos de arquivos físicos únicos.
2. **Selecionar:** páginas por chave estável, inicialmente 200 mensagens por lote, uma
   conversa por transação e orçamento de aproximadamente dois segundos. São parâmetros
   iniciais a medir no LAB, não metas garantidas. Evitar OFFSET e varredura global de
   JSON; índices de rota/data/ID devem acompanhar o plano.
3. **Travar e revalidar:** usar a ordem de locks já adotada pelo serviço. Ingress,
   download, envio e purga devem participar da mesma barreira. Inicialmente a trava de
   caixa existente pode ser usada em transações curtas; uma trava só de conversa exige
   que todos esses caminhos a respeitem. Não basta acrescentá-la apenas ao cron.
4. **Proteger trabalho em curso:** não apagar envio pendente/incerto nem agendamento
   futuro. Não segurar lock durante I/O externo. Job iniciado ou resultado incerto adia
   o conjunto dependente e registra causa; recuperação continua no mecanismo normal.
   Proteger também replies/retries sobreviventes que dependam do alvo.
5. **Registrar bloqueio de replay e eliminar:** na mesma transação, preparar as
   dependências por extensão, sanear ledgers/locators, remover filas terminais e
   registros exclusivos por ORM, preservar/reassociar anexos compartilhados e excluir
   mensagens. Usar capabilities internas, não expor `sudo().unlink()` genérico à
   interface. Falha em qualquer etapa reverte o lote inteiro.
6. **Atualizar estado:** recalcular prévia e data da última mensagem, contadores,
   âncoras de leitura individuais, limites de paginação e agregados. Preservar a posição
   cronológica de leitura com cursor sem FK, quando necessário: apenas deixar
   `seen_message_id` virar NULL pode ressuscitar “não lidas”. Não emitir mark_read ao
   provedor como efeito da limpeza.
7. **Invalidar clientes após commit:** retirar mensagens vencidas inclusive de conversas
   abertas, fechar mídia removida, atualizar lista/paginação e o dia fixo no topo. Uma
   linha discreta pode informar “Histórico anterior a … removido pela política desta
   caixa”, sem criar milhares de mensagens de sistema.
8. **Verificar e continuar:** medir eliminados, preservados compartilhados,
   bloqueados/adiados, erros, duração, atraso da fila e bytes aguardando coleta. Parar
   ao atingir orçamento ou contenção; continuar em execução posterior.

MVP não deve ter dois relógios independentes para mídia e texto, combinações por tipo de
arquivo ou regras automáticas por tamanho. Um prazo para todo o histórico da conversa é
mais compreensível. Adicionar políticas especiais só com uso real.

## Banco e disco: o que a limpeza entrega

Há três verificações distintas: conteúdo indisponível no aplicativo, registros
exclusivos removidos no banco e arquivos físicos liberados.

No Odoo 16, `ir.attachment.unlink()` remove o registro e coloca o arquivo na fila de
coleta. A coleta do filestore verifica se o `store_fname` ainda é usado antes de remover
o arquivo. Portanto, exclusão por ORM e liberação física não são simultâneas, e bytes
compartilhados continuam corretamente no disco. Não executar remoção manual de arquivos
nem invocar a coleta dentro do lote de purga: ela abre fronteiras próprias de
transação/lock. Verificar o código OCB efetivo e o agendamento da coleta na homologação.
[Código oficial de anexos do Odoo 16](https://github.com/odoo/odoo/blob/16.0/odoo/addons/base/models/ir_attachment.py).

O unlink nativo de `mail.message` também remove anexos cujo dono é a própria mensagem.
Por isso a classificação e reassociação de um anexo compartilhado precisam acontecer
**antes** dessa chamada.
[Código oficial de mensagens do Odoo 16](https://github.com/odoo/odoo/blob/16.0/addons/mail/models/mail_message.py).

No PostgreSQL, remover linhas não reduz imediatamente o arquivo da tabela. O autovacuum
recupera espaço para reutilização e evita crescimento contínuo por versões mortas.
`VACUUM FULL` reescreve a tabela, exige espaço temporário e lock exclusivo; não é parte
desse cron nem indicação para a rotina de um ERP em uso. O objetivo inicial é
estabilizar o volume, e não truncar tabelas ou reescrever o banco toda semana.
[Manutenção oficial do PostgreSQL](https://www.postgresql.org/docs/current/routine-vacuuming.html).

Não estimar economia como soma de `ir.attachment.file_size`: vários registros podem
apontar ao mesmo arquivo, e um arquivo compartilhado não será liberado. Relatar
separadamente: bytes de conteúdo removido, bytes exclusivos elegíveis, bytes ainda
compartilhados e bytes físicos efetivamente coletados.

A garantia abrange o armazenamento ativo controlado pelo módulo. Backups, réplicas, WAL,
exportações, logs externos, cache de navegador, arquivos já baixados, WhatsApp/WuzAPI e
Meta seguem ciclos próprios. O estudo não promete apagamento forense ou remoto e não
altera retenção desses ambientes. Depois de restaurar backup antigo, reaplicar o limite
de expiração antes de liberar ingestão/UI para não ressuscitar conteúdo vencido.

## Critérios para considerar pronta uma futura implementação

- Regra desligada não altera nenhum registro; grupos de uma caixa não afetam outras
  caixas nem conversas diretas; exceção de preservação prevalece.
- Prazo e aviso correspondem à caixa atual; alterar sete para outro valor atualiza o
  painel. Agente vê o estado, mas não altera a exceção nem por chamada direta.
- Supervisor com acesso e administrador podem preservar e voltar a seguir a caixa;
  usuário sem acesso não altera outro grupo. Exceção persiste após troca de conexão,
  merge de aliases e desativação/reativação da caixa, com auditoria do autor.
- Preservar enquanto há purga enfileirada ou concorrente respeita a confirmação e as
  travas; outras abas recebem o novo estado, sem mostrar sucesso em falha de gravação.
- Limite exato em UTC, importação histórica, mudança de fuso, mensagem editada,
  timestamp ausente/inválido e replay não renovam prazos indevidamente.
- Texto, versões, snapshots citados, prévias, JSON, locators, uploads, jobs e mídia
  exclusiva desaparecem dos caminhos operacionais mapeados; exceções são medidas.
- Arquivo compartilhado por duas mensagens, outra conversa, negócio ou catálogo
  permanece íntegro; arquivo exclusivamente expirado desaparece após coleta.
- Replay original, receipt fora de ordem, edição/reação de alvo expirado, reconexão,
  troca de conexão, merge LID/telefone e importação não recriam histórico.
- Duas purgas concorrentes, entrada durante purga, download em voo e envio de resultado
  incerto não duplicam envios, não materializam mídia órfã e não bloqueiam atendimento
  por uma transação longa.
- Uma conversa aberta e outra aba atualizam corretamente: não lidas por usuário, última
  mensagem, rolagem, dia fixo, busca e paginação sem lacunas inesperadas.
- Pipeline/CRM, tarefas, documentos comerciais e fatos de atribuição continuam íntegros;
  extensões instaladas com dependência não tratada bloqueiam a purga.
- Simulação, execução em lotes, falha/retry, retomada e indicadores de GC têm evidência
  em LAB com volume realista, antes de escolher grupos oficiais.

Uma exclusão já confirmada não tem “desfazer” sem fonte restaurável. Desativar a regra
apenas interrompe ciclos futuros. A futura implantação deve apresentar o dry-run e
tratar restauração separadamente de rollback de código, respeitando a autorização e a
política operacional vigentes. **Neste pedido, somente o estudo foi realizado.**
