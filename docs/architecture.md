# Arquitetura do Contact Center

[Voltar ao README](../README.md) · [CRM e atribuição](crm-and-attribution.md) ·
[Roadmap](roadmap.md)

Este documento descreve os contratos atuais do código Odoo 16. Datas de implantação,
resultados de testes e decisões de uma entrega específica pertencem ao histórico; não
substituem a verificação do ambiente que será atualizado.

## Responsabilidades dos módulos

| Módulo                  | Responsabilidade                                                                         | Dependências diretas                       |
| ----------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------ |
| `contact_center_base`   | Conversas, identidades, acesso, inbox/outbox, mídia, evidência de origem e produtividade | `mail`, `queue_job`, `rating`, `web`       |
| `contact_center_ui`     | Caixa de atendimento Owl sobre a API local                                               | Base e `web`                               |
| `contact_center_wuzapi` | Transporte WhatsApp e normalização do provedor                                           | Base                                       |
| `contact_center_meta`   | Messenger e Instagram vinculado a Página                                                 | Base, `meta_api_base`, `meta_webhook_base` |
| `contact_center_crm`    | Painel comercial do cliente e associação explícita conversa–CRM                          | UI e `crm`                                 |
| `contact_center_kanban` | Atendimentos, pipelines, ações CRM, sincronização de etapas e integrantes                | Contact Center CRM                         |

As fundações Meta são distribuídas pelo
[Marketing Center](https://github.com/soloztech/marketing-center/tree/16.0). Isso não
torna os módulos funcionais de marketing dependências do inbox.

## Registros canônicos e identidade

`mail.channel` é a conversa; `mail.message` é a mensagem. Os bindings guardam os
identificadores externos, direção e contexto do transporte sem substituir esses
registros nativos. A interface usa a API local versionada e não modifica JavaScript
privado do Discuss ou Live Chat.

A pessoa remota usa `mail.guest`, uma identidade durável e aliases. O vínculo com
`res.partner` é uma ação explícita. PN de WhatsApp comprovado pelo protocolo pode ser
resolvido entre caixas da mesma empresa. LID, JID opaco, PSID e IGSID conservam seu
escopo de caixa. Empresas operadoras permanecem isoladas.

Vincular um contato não mescla conversas nem cria um lead. A empresa principal segue
`res.partner.parent_id` e `commercial_partner_id`; relações secundárias não concedem
acesso a documentos. Consulte o
[contrato de relações entre pessoa e empresas](../research/partner-company-relationships.md).

## Caixa, conexão e acesso

Uma conta representa uma caixa lógica. As conexões de transporte têm papéis `primary`,
`standby`, `migration` ou `historical`. Somente a primária ativa admite tráfego; o envio
ainda depende da habilitação de saída, saúde e identidade verificadas. Trocar a conexão
não altera as referências de mensagens já enviadas pelo provedor anterior.

O acesso atual usa `access_user_ids` e `access_team_ids`, ambos cumulativos. A união dos
usuários diretos e integrantes autorizados das equipes determina o público da caixa; as
participações nativas no canal refletem essa autoridade. Revogar acesso remove a
participação que perdeu seu fundamento. Responsável pela conversa e atribuição
automática continuam escolhas singulares, limitadas aos usuários autorizados.

Os nomes antigos `owner_user_id` e `default_team_id` encontrados em pesquisas e planos
não descrevem os campos atuais de concessão de acesso. A migração é documentada em
[account-access-upgrade.md](../operations/account-access-upgrade.md). Equipe de um
Atendimento é contexto de negócio e não constitui uma segunda concessão de acesso à
conversa.

## Entrada, saída e filas

```mermaid
flowchart TD
    P["Webhook autenticado"] --> I["Envelope durável e saneado"]
    I --> Q["JobRunner OCA"]
    Q --> D["Adapter: EventDTO e evidências de origem"]
    D --> C["Identidade, conversa e mensagem canônicas"]
    C --> UI["API local, bus e interface"]
    UI --> A["Ação explícita de envio"]
    A --> O["Mensagem, binding e outbox na mesma transação"]
    O --> J["Job após commit: revalidar e despachar"]
    J --> V["API do provedor"]
```

Adapters autenticam, traduzem e transportam. O Base resolve identidade e cria os
registros do domínio. Inbox e outbox são registros persistentes de processamento;
`queue_job` executa os trabalhos, controla concorrência e agenda novas tentativas. O
composer e o webhook não enviam diretamente ao provedor.

Chaves de deduplicação e UUIDs de requisição tornam repetições reconhecíveis. O envio
registra uma fronteira durável antes da chamada externa. Um resultado `uncertain`
aguarda evidência; não recebe reenvio cego. Reenvio explícito de falha terminal elegível
cria uma nova tentativa vinculada à anterior. Saúde recuperada só retoma trabalho
compatível com a configuração e que ainda não cruzou essa fronteira.

Mídia, avatares e miniaturas ficam em anexos privados. Downloads e enriquecimentos
ocorrem em tarefas limitadas; credenciais e URLs privadas do provedor não entram na API
operacional ou no bus. Consulte
[resiliência e capacidade](../operations/resilience-capacity-drill.md) e os contratos de
[mídia](../research/wuzapi-media-transport.md) e
[saúde da sessão](../research/wuzapi-session-lifecycle.md).

## Conversa, Atendimento e CRM

A conversa usa `open`, `resolved` e `archived`. Uma mensagem nova aceita reabre uma
conversa resolvida; uma arquivada exige restauração explícita. Replay, recibo e mutação
não equivalem a uma nova mensagem humana.

Notas internas, respostas rápidas, mensagens agendadas e acompanhamentos pertencem ao
Base. Acompanhamentos são da conversa e não exigem Kanban. O addon opcional Kanban
acrescenta casos com pipelines e histórico próprio de transições; a etapa de um caso não
substitui o estado operacional da conversa.

O painel CRM lê documentos do cliente comercial com as permissões nativas. Sua consulta
não cria associação com a conversa. `contact.center.crm.conversation.link` conserva essa
associação explícita. O Kanban acrescenta ações próprias de criação/vínculo e a projeção
de casos em etapas CRM. Seus vínculos de pipeline e de integrantes têm ciclos separados.
Consulte [CRM e atribuição](crm-and-attribution.md) para o fluxo de uso.

## Evidência de aquisição

Touchpoints conservam observações, nível de evidência e identificadores com papéis
próprios. `fbads` isolado é um sinal do provedor, não uma campanha identificada nem uma
decisão de criação de lead. Cartões de anúncio exibem contexto disponível; a exibição
também não cria associação comercial.

As pontes opcionais do Marketing Center consomem essas evidências e os vínculos CRM. Há
catálogo Meta e resolução correspondente. A associação automática de campanha externa a
UTMs nativas e a consulta GCLID → `click_view` → campanha ainda não são fluxos
implementados do produto. Veja o
[contrato entre projetos](https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md).

## Conteúdo, retenção e extensões

Excluir, ignorar, resolver e preservar histórico são operações distintas. Retenção por
prazo para grupos já existe, desabilitada por padrão, com exceção por conversa e
barreira contra recriação de conteúdo expirado. O lote preserva registros comerciais,
protege trabalho em andamento e adia dependências sem contrato seguro de expiração. A
coleta física dos arquivos continua a cargo do Odoo.

O contrato de extensão para retenção é implementado em
[retention_dependencies.py](../contact_center_base/models/retention_dependencies.py).
Novos consumidores devem preservar seus fatos e liberar apenas suas referências antes de
permitir expiração. O
[estudo de retenção](../research/message-retention-proposal-2026-09-11.md) separa o
resumo implementado das propostas originais; a ocorrência de “futuro” no estudo não
implica ausência do recurso no código atual.

Guias de operação preservados:

- [Ações da conversa](../operations/conversation-actions.md).
- [Prévias de anúncio](../operations/ad-origin-preview.md).
- [Transcrição de áudio](../operations/audio-transcription.md).
- [Extração CRM para instalações antigas](../operations/crm-independent-upgrade.md).
- [Cutover e rollback](../operations/production-cutover-rollback.md).

As instruções de publicação em `AGENTS.md` prevalecem sobre versões, datas e tags
citadas em procedimentos antigos. Uma mudança exige identificar sua origem e escopo; a
presença de um runbook não autoriza executar seus comandos.
