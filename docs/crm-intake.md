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
contrário, recebe o usuário de execução. Reutilizar não modifica vendedor, equipe,
etapa, descrição ou UTMs do negócio. O vínculo aparece na Jornada como **Entrada
automática**, em **contexto**. A equipe confirma o período comercial depois.

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

Para parar novas entradas, desmarque a regra na caixa; isso invalida jobs antigos.
Reativar cria novo corte e não admite conversas antigas. Uma conversa já admitida, ainda
não decidida, pode retomar com uma nova mensagem elegível. Após falha técnica do
agendamento, um administrador pode invocar `action_recover_crm_intake` sobre até 100
bindings explicitamente selecionadas. A recuperação aplica os mesmos cortes; não há cron
de recuperação histórica e decisões terminais permanecem intactas.

O corte começa no segundo seguinte à ativação, conservando a precisão das datas do
provedor. Mensagens recebidas antes desse horário ficam fora da regra, inclusive
webhooks antigos processados depois. Arquivar a caixa desliga a entrada e invalida jobs
pendentes; desarquivar mantém a entrada desligada até nova ativação explícita.
