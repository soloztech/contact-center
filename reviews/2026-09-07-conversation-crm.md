# CRM ligado à conversa

O vínculo anterior existia em `contact.center.crm.case.link`, pertencente a um
atendimento de pipeline. Isso exigia Kanban para vincular a conversa ao CRM e não
oferecia a ação na tela principal do chat. O usuário corrigiu a arquitetura: o CRM deve
funcionar diretamente na conversa e o Kanban deve ser opcional.

## Estrutura

| Addon                                 | Responsabilidade                                  | Dependências diretas relevantes                         |
| ------------------------------------- | ------------------------------------------------- | ------------------------------------------------------- |
| `contact_center_base`                 | Conversas, clientes, permissões e mensageria      | Sem CRM ou Kanban                                       |
| `contact_center_ui`                   | Caixa de entrada e painel de contato              | Base                                                    |
| `contact_center_crm`                  | Vínculo conversa–CRM e painel de oportunidades    | UI e CRM nativo                                         |
| `contact_center_kanban`               | Atendimentos, pipelines e sincronização de etapas | CRM do Contact Center                                   |
| `marketing_center_contact_center_crm` | Atribuição de Marketing aos vínculos da conversa  | CRM do Contact Center e pontes de Marketing; sem Kanban |

O registro canônico é `contact.center.crm.conversation.link`, com vários leads ou
oportunidades por conversa e unicidade do par ativo. Não há oportunidade principal
implícita. Vincular não cria atendimento nem altera equipe ou etapa do CRM.

Os modelos técnicos antigos de pipeline, vínculos de etapas e atendimentos foram
transferidos para Kanban, preservando tabelas e identificadores. Não foi criado outro
addon de compatibilidade. O nome visível `Service Cases` foi substituído por
**Atendimentos**; ele designa somente o recurso opcional de Kanban.

## Painel

O aperto de mãos na barra principal abre um painel lateral, alternando com os detalhes
do contato. Ele mostra oportunidades da pessoa e da empresa comercial, permite buscar,
paginar, abrir o formulário nativo, vincular e desvincular. As relacionadas são
sugestões; somente o vínculo explícito associa a conversa. Leads e registros vinculados
arquivados são identificados na interface.

A projeção exige acesso ao chat e aplica as regras nativas do CRM às oportunidades e aos
IDs vinculados. O ledger não concede leitura ou escrita direta a agentes; as ações
públicas validam contexto, cliente e empresa. Respostas antigas não reaparecem depois de
trocar ou fechar a conversa. Não foi adicionada criação rápida de oportunidades ou
sincronização adicional nesta entrega.

## Integridade e atualização

O vínculo tem histórico de desvinculação e é preservado no merge nativo do CRM, com
consolidação de pares duplicados. Exclusão do lead aposenta a associação. Mudanças de
empresa, inclusive as calculadas a partir de vendedor/equipe/cliente, não podem deixar
uma associação com empresa incompatível.

Desvincular um atendimento preserva o vínculo da conversa. Desvincular a conversa
encerra somente suas projeções correspondentes de Kanban e revoga a atribuição de
Marketing correspondente. A ordem de locks é extensível: autoridades do Kanban quando
instalado, lead, atendimento, conversa e atribuição.

A atualização de instalações anteriores exige os scripts explícitos de extração de
metadados e backfill ORM. O plano preserva IDs de casos, leads, equipes, pipelines e
histórico; as antigas autoridades de atribuição são reconciliadas. Não existe caminho de
migração disparado a cada abertura do chat.

Um marcador técnico permanece pendente entre a transferência dos metadados e o commit do
backfill. O aplicador bloqueia esse estado mesmo quando os identificadores antigos já
foram transferidos. Rollback preserva a pendência; somente a conversão confirmada libera
a atualização.

## Validação

Evidências locais em `scans/raw/20260907-conversation-crm/` no repositório de
infraestrutura:

- CRM sem Kanban: **18 testes aprovados**.
- Contact Center completo: **982 testes aprovados**, incluindo **143** de Kanban.
- Marketing–CRM sem Kanban: **25 testes aprovados**.
- QUnit conjunto: **190 testes / 1.624 assertions** em cada modo, minificado e
  `debug=assets`, sem falhas.
- Navegação em desktop e celular, busca, troca de cliente, vínculo/desvínculo,
  alternância de painéis e CRM nativo aprovados. Zero emails/outboxes.
- Upgrade de dados sintéticos antigos, rollback e repetição aprovados: **47 XMLIDs**,
  **5 atendimentos**, **7 transições** e **2 oportunidades** preservados; **3 vínculos
  diretos** com proveniência original e atribuição reconciliada.
- Interrupção entre as fases, rollback, visibilidade antes/depois do commit e repetição
  do marcador de conclusão aprovados em banco restaurado do backup.
- Procedimentos operacionais: **89 testes Contact Center** e **29 Marketing Center**
  aprovados.
- Pre-commit, inclusive pylint obrigatório, aprovado.

A atualização real do laboratório ainda não foi aplicada. O aplicador histórico dispensa
backup do banco e não comprova um backup pareado restaurável; a extração fica bloqueada
nesse caminho até resolver esse requisito operacional. Instalações novas e releases sem
extração permanecem disponíveis. Detalhes em `operations/crm-independent-upgrade.md`.
