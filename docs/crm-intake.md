# Entrada automática no CRM

Um administrador Contact configura **Entrada automática no CRM** na caixa WhatsApp, com
usuário de execução ativo, acesso à caixa e permissão nativa para criar leads. A empresa
é a da caixa; a equipe comercial opcional deve pertencer à mesma empresa. Todas as
caixas começam com a regra desligada. A implantação Soloz ativa somente Comercial, ID10,
empresa1, usuário de execução Lucas, ID6.

A ativação registra revisão, horário e último ID de conversa existente. Somente
conversas novas e mensagens humanas recebidas após esse corte entram. Mensagens antigas
atrasadas, conversas já existentes, grupos, reações, chamadas e eventos de sistema não
criam leads. Não há varredura nem preenchimento retroativo.

O recebimento salva a mensagem antes de agendar o CRM. Em outra transação, o worker
prioriza negócios abertos já vinculados à conversa; depois procura por contato explícito
ou telefone completo comprovado, na empresa da caixa. Um candidato visível é
reutilizado; candidatos múltiplos, sem empresa ou restritos exigem associação humana.
Sem candidato e com identidade suficiente, cria um lead. IP, nome, sufixo telefônico,
LID e outros contatos da mesma empresa não são identidade suficiente.

O responsável atual da conversa recebe o novo lead se tiver acesso e permissões; caso
contrário, o vendedor fica vazio, aguardando a atribuição inicial elegível. Nos novos
leads automáticos, **Criado por** fica vazio; o recibo de entrada e o vínculo da
conversa preservam a auditoria do executor técnico. Os registros históricos não são
reescritos. Reutilizar não modifica vendedor, equipe, etapa, descrição ou UTMs do
negócio. O vínculo aparece na Jornada como **Entrada automática**, em **contexto**. A
equipe confirma o período comercial depois.

A primeira atribuição elegível posterior preenche o vendedor uma única vez. Depois,
transferências e desatribuições da conversa não sincronizam o vendedor do CRM, que
continua editável. Escolher um vendedor no CRM ou alterar o vendedor existente encerra a
inicialização pendente. Converter para oportunidade sem escolher vendedor mantém a
pendência. Uma escrita que apenas mantém o vendedor já vazio também mantém a pendência.

O worker examina os eventos persistidos em ordem e verifica as permissões disponíveis no
processamento. Atribuições já examinadas como inelegíveis não voltam a concorrer após um
ganho de permissão. Nesse caso, escolha o vendedor no CRM ou faça uma nova atribuição da
conversa; a recuperação não revive o evento já processado.

Leads criados por esta entrada ficam permanentemente excluídos de automações nativas,
enrolamento OCA e passos de email, ação, atividade e Contact Center. Quando instalado, o
enriquecimento IAP também fica excluído. Não há mensagem automática ao cliente. A
qualificação humana segue o CRM nativo; merge conserva a marca de exclusão. As
integrações de automação instaladas precisam ter os guards compatíveis: a ativação e o
worker recusam uma combinação de versões sem essa proteção.

O painel de oportunidades informa processamento pendente, criado, reutilizado ou revisão
necessária, sem revelar negócios restritos. A retenção de mensagens preserva o recibo de
decisão na conversa. Uma decisão terminal não cria outro lead nem repõe um vínculo
removido manualmente.

Para parar novas entradas, desmarque a regra na caixa; isso invalida jobs antigos e
pausa a atribuição inicial dos leads pendentes, mesmo com executor configurado. A
pendência e a posição dos eventos permanecem intactas enquanto a regra estiver
desligada. A política completa e os guards também são verificados antes de atribuir.
Reativar cria novo corte e não admite conversas antigas. Uma conversa já admitida, ainda
não decidida, pode retomar com uma nova mensagem elegível. Após falha técnica do
agendamento, um administrador pode invocar `action_recover_crm_intake` sobre até 100
bindings explicitamente selecionadas. A recuperação de uma entrada ainda não decidida
aplica os mesmos cortes. Para recibos **Lead criado** cujo lead ainda aguarda vendedor,
ela também pode reagendar a atribuição inicial após a reativação e revalidação da
política. Isso preserva a decisão de criação, respeita os eventos já processados e não
cria outro lead. Não há cron de recuperação histórica, alteração do vendedor escolhido
manualmente nem reconstrução de vínculo removido.

O corte começa no segundo seguinte à ativação, conservando a precisão das datas do
provedor. Mensagens recebidas antes desse horário ficam fora da regra, inclusive
webhooks antigos processados depois. Arquivar a caixa desliga a entrada e invalida jobs
pendentes; desarquivar mantém a entrada desligada até nova ativação explícita.

Sem equipe CRM configurada na caixa, os novos leads ficam explicitamente **sem equipe
comercial**, com o responsável elegível da conversa ou vendedor vazio. Configure a
equipe da empresa na caixa quando quiser incluí-los automaticamente no funil de uma
equipe. Remoção manual do vínculo também exige revisão se ocorreu antes da primeira
resposta do cliente; a entrada não refaz essa associação.

### Recuperar um agendamento que falhou

Procurar no log
`CRM intake admission deferred for channel binding ID (message binding ID; CLASSE)`. O
primeiro ID é a conversa a selecionar, não o ID da mensagem. Um administrador Contact
autorizado pode recuperar somente os IDs analisados via shell nativo sob os locks
operacionais existentes (não usar UID1 como executor permanente nem varrer todo o
histórico):

```python
# env é o ambiente do shell nativo; administrador deve ter grupo Contact Admin.
# Substituir pelos IDs de channel bindings explicitamente conferidos no log.
selected_ids = [123]
assert 0 < len(selected_ids) <= 100
bindings = env['contact.center.channel.binding'].browse(selected_ids).exists()
assert len(bindings) == len(selected_ids)
result = bindings.action_recover_crm_intake()
env.cr.commit()
```

O método aplica autorização e elegibilidade novamente. Não confirma períodos nem associa
manualmente um lead; apenas reagenda entradas elegíveis ou a atribuição inicial de um
lead criado e ainda pendente. Em produção, seguir o wrapper de deploy e registrar
IDs/resultado sem corpo de mensagem.

### Limite quando Kanban estiver instalado

Com Kanban e casos ligados ao candidato, a orquestração nativa de permissões pode
atualizar a revisão de acesso da caixa durante o reuso. Mensagens concorrentes da mesma
caixa podem ter retry por serialização; não são descartadas. O timeout de 250 ms
continua aplicado ao worker. Essa ampliação de contenção é um residual aceito para a
composição opcional. Kanban não está instalado na produção desta entrega; o aplicador
preserva o conjunto de módulos instalados. A prova de corrida com candidato ligado a
caso fica como cobertura adicional futura. As provas de concorrência de entrada CRM e
atribuição inicial executadas para este fluxo não afirmam cobrir essa composição.
