# Saúde e recuperação de conexões

O núcleo controla a cadência e o envio para todos os provedores. O adapter interpreta o
transporte, a identidade e o meio de recuperação. Não há consulta HTTP de saúde no
caminho normal de cada mensagem.

## Envio e agendamento

- Um envio exige conexão ativa, primária, autorizada e uma observação conclusiva de
  identidade/sessão, sem bloqueio por configuração ou identidade divergente. A idade
  dessa prova não bloqueia o envio.
- O cron continua a cada minuto, selecionando no máximo 200 conexões vencidas, com
  locks, identidade única, prioridade baixa e jitter existentes. Após uma consulta
  saudável, a próxima fica para 3600 segundos. Não confundir esse cron com a frequência
  de HTTP externo.
- Falhas têm espera de 60, 120, 240, 480 e até 600 segundos. Retry-After é sempre um
  piso. Job enfileirado tem lease de 300 segundos para recuperação de falha; isso não
  significa uma nova consulta normal a cada 300 segundos.
- Uma falha de transporte/autorização no envio pode pedir consulta antecipada. O
  resultado do envio é gravado antes desse pedido, em transação separada. Há
  deduplicação, intervalo mínimo de um minuto e respeito ao backoff/cooldown existente.
  Erros de conteúdo ou destinatário e recusas por limite não pedem saúde.
- Uma leitura de saúde inconclusiva preserva a última prova, registra
  `last_probe_detail` e pausa novos envios antes do HTTP, devolvendo a tentativa. Um
  rate limit apenas do probe preserva o envio verificado e fica como aviso do painel,
  sem alerta de falha ou botão de configuração. Se já havia diagnóstico bloqueante, o
  rate limit o preserva: ele não comprova recuperação. Uma observação conclusiva
  posterior limpa o diagnóstico e retoma somente pendentes seguros.
- Timeout após a fronteira de envio continua `uncertain`. Uma consulta saudável nunca
  reenvia essa mensagem automaticamente.
- Se todas as tentativas locais de gravar o pedido antecipado falharem, a recuperação
  depende do próximo pedido bem-sucedido ou da agenda normal de até uma hora. O lease de
  cinco minutos só cobre job que chegou a ser enfileirado.

O estado conclusivo está em `last_state_observed_at/last_state_source`; `last_health_at`
indica tentativa de leitura e não pode ser usado como prova. A migração agenda
confirmação imediata das conexões legadas conectadas sem prova.

## Aviso e correção

O componente de saúde também exibe um aviso persistente no app, inclusive na lista
mobile, e na janela flutuante da caixa afetada. A capacidade local do usuário decide
quem recebe o botão de correção; um upsert do bus não concede essa permissão. O backend
revalida administrador, empresa e caixa visível.

Desconexão/autorização conclusivas continuam visíveis após uma leitura inconclusiva; o
detalhe dessa leitura permanece no painel. Uma prova saudável antiga, sem incidente,
fica como desconhecida no painel, sem aviso grande ou botão de correção.

O descriptor neutro `fix_kind` e o hook `_contact_center_fix_action` permitem que cada
integração ofereça sua recuperação. Meta e Cloud abrem a configuração existente. A
política comum não interpreta nomes de provedores nem impõe QR a outros adapters.

## Reconexão WuzAPI

O wizard restaura a conexão existente por solicitação explícita do administrador, com as
credenciais da própria conexão. Não cria instância, troca número, token, webhook ou
HMAC. Reusa o canal e o controle de operação do onboarding, preservando o onboarding
original e a revisão de configuração.

A permissão do autor é conferida antes de cada tentativa do job e nas ações privadas de
QR. Uma revogação durante uma tentativa já em andamento pode ficar invisível no
snapshot/cache do Odoo até o fim dessa tentativa, permitindo um Connect já autorizado.
Não há cancelamento instantâneo de chamadas em voo. Os locks e fences de conexão,
configuração e operação continuam valendo; a leitura posterior do QR revalida o acesso.

Uma sessão não logada pode continuar pareada. O wizard tenta restabelecer/verificar; só
oferece QR quando o provedor retorna um PNG válido. Desafio adicional por passkey deve
ser concluído no provedor. API indisponível ou credencial recusada não é motivo para
mostrar QR.

Se a leitura após restabelecer a sessão falhar, o wizard guarda somente a observação
segura necessária para conferir eventos e rede, inclusive enquanto o QR ainda não está
disponível. **Verificar** refaz a leitura com espera controlada. Se a sessão já está
aberta, não repete Connect; se o status confirma que ela não abriu, pode tentar Connect
idempotente uma vez nessa ação, conservando a observação original. Ao reabrir a
correção, o mesmo administrador retoma sua verificação pendente enquanto as revisões
continuam válidas. Mudança de configuração exige uma nova solicitação. O indicador de
rede exclui usuário, senha, caminho e query do proxy; nenhuma credencial é guardada. Uma
divergência real de eventos aparece com mensagem específica e no diagnóstico de webhook
existente.

O pedido guarda sua empresa de origem. Mudança da empresa da conexão torna a solicitação
obsoleta, mesmo que o administrador tenha acesso às duas empresas.

A política de rede do provedor permanece aplicável: o runtime pode atribuir proxy do seu
pool ao iniciar uma sessão que ainda não tenha proxy. O wizard compara indicadores
seguros de rede e eventos antes/depois e informa a mudança; não edita o pool nem desfaz
a atribuição automaticamente.

O fluxo novo foi verificado contra os contratos históricos do addon e fontes do runtime
`d1f705527bada8458c2b802c1d6ac2ef6abc5364`. Antes da promoção, conferir o runtime real e
a proteção contra Connect concorrente; não presumir compatibilidade de builds antigos.

## Pareamento e eventos retidos

Ao obter um QR real, o sistema persiste um ciclo (`wuzapi_pairing_revision`) antes de
expor a imagem. Nessa janela, eventos de conteúdo são retidos antes de preview ou
projeção. Eventos de sessão continuam sendo processados. Latch, mudança de configuração
ou falha de transporte sem novo QR não iniciam essa retenção de entrada.

- Identidade correta no ciclo atual permite novos callbacks.
- Número divergente ou logout recebido durante o pareamento torna os eventos daquele
  ciclo inelegíveis para liberação, mesmo após prova positiva posterior.
- Eventos retidos nunca voltam automaticamente para a fila. O administrador usa
  **Revisar eventos retidos**, seleciona até 200 eventos de uma conexão e **Confirmar
  origem e reenfileirar**.
- A confirmação exige prova posterior aos eventos, ciclo e configuração atuais, ausência
  de divergência/logout e escopo válido. Uma seleção mista inelegível falha
  integralmente. O requeue comum não contorna essa confirmação.
- O administrador deve confirmar que os eventos pertencem ao número da caixa. A ação
  registra autor/data/razão; não certifica a origem automaticamente. Ciclos anteriores,
  divergentes ou rejeitados exigem investigação e correção de dados separada.

Se o telefone errado foi pareado, desvincule a sessão em **Dispositivos conectados**
desse aparelho, ou administre o unlink no provedor quando não houver acesso ao telefone.
Depois reconecte com o número correto. Não há Logout destrutivo automático.

A garantia de retenção anterior à projeção vale para QR emitido por este fluxo.
Pareamento feito diretamente no painel/API do provedor não fornece um início de ciclo
confiável ao Odoo e mantém esse limite de isolamento de entrada.

## LID e limites do lote

A reconexão usa a transação temporária do queue_job (`allow_commit`), mantendo o
bloqueio de execução na transação externa. Após consultar o provedor, renova o snapshot
do banco e revalida os vínculos antes de gravar. Uma mensagem enfileirada durante a
consulta não obriga a repetir o Connect. Um resultado já gravado também é preservado se
o worker reiniciar antes de concluir o job.

Se o worker cair após Connect e antes de gravar o resultado, a retentativa pode repetir
o Connect idempotente e perder a observação anterior usada no aviso de rede e na
comparação dos eventos assinados. Não há garantia de execução única nessa janela. A
publicação exige o runtime compatível já descrito para Connect.

O vínculo PN/LID válido é reaproveitado sem renovação periódica. Quando falta ou é
inconsistente, a consulta usa o PN configurado e valida ambos os lados; nunca infere PN
pelo LID. O status recente do runtime pode apresentar LID e é aceito somente com esse
vínculo protegido. Nova configuração/QR ou divergência invalida a prova correspondente;
restabelecer a sessão do mesmo número sem QR não a invalida.

As fontes principais são `contact_center_base/models/account.py`, `models/queue.py`,
`services/adapter.py`, os adapters Meta/Cloud/WuzAPI e
`contact_center_wuzapi/models/reconnect.py`. Este lote não altera a cadência de
perfis/fotos/grupos, a saúde administrativa do servidor WuzAPI, retenções ou limpezas.
