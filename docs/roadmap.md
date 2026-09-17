# Roadmap do Contact Center

[Voltar ao README](../README.md) · [Arquitetura](architecture.md) ·
[CRM e atribuição](crm-and-attribution.md)

Este índice separa lacunas de produto, pesquisas e verificações por ambiente. Não
constitui compromisso de prazo nem autorização para ativar integrações. As observações
foram confrontadas com o código e a documentação disponíveis na reorganização de
15/09/2026; resultados históricos continuam vinculados à versão que foi ensaiada.

## Lacunas e decisões abertas

| Tema                                      | Situação e próximo passo delimitado                                                                                                                                                                                                                                                                              | Evidência                                                                                                                                                       |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Presença, colisão e distribuição avançada | Atribuição individual e assignee automático já existem. Presença dos agentes, distribuição avançada e SLA de atendimento exigem desenho próprio, sem ampliar implicitamente o acesso às caixas.                                                                                                                  | [Consolidação histórica](history/plans/plan.md#consolidação-native-first-e-ux--decisão-de-2026-09-02); [acesso atual](../contact_center_base/models/account.py) |
| Cartão Pix recebido                       | O parser de conteúdo estruturado não implementa `payment_info`/`pix_static_code`. Definir apresentação validada e cópia explícita da chave, sem criar pagamento, fatura ou estado de quitação.                                                                                                                   | [Auditoria dos formatos](history/reviews/2026-09-10-workspace-webhook-gaps.md); [parser](../contact_center_wuzapi/services/structured_content.py)               |
| Formatos e recibos do provedor            | Investigar ocorrências e contratos antes de ampliar suporte. `albumMessage` é tratado como envelope de coordenação, não como mensagem humana independente; `ReadSelf` não deve significar leitura por todos os agentes. Recibos e `IdentityChange` já têm suporte e exigem diagnóstico específico quando falham. | [Auditoria dos formatos](history/reviews/2026-09-10-workspace-webhook-gaps.md); [adapter WuzAPI](../contact_center_wuzapi/services/adapter.py)                  |
| Experiência unificada e indicadores       | O painel comercial, relações entre empresas e Atendimentos já existem. A visão unificada entre conversas/canais e indicadores operacionais mais amplos precisam de escopo e critérios próprios; não reabrir esses recursos entregues como se faltassem por completo.                                             | [Arquitetura](architecture.md); [benchmark histórico](history/reviews/2026-09-02-suite-roadmap-rdstation.md)                                                    |
| IA de apoio ao atendimento                | Há avaliação de copilot, assistente e SDR, ainda para discussão. Definir um piloto separado; a transcrição existente não equivale a agente conversacional ou envio autônomo.                                                                                                                                     | [Pesquisa de IA em andamento](../research/ai-sdr-copilot-evaluation-2026-09-10.md)                                                                              |
| Novos adapters e Helpdesk                 | WAHA, Evolution, Telegram, WhatsApp oficial e Helpdesk são possibilidades de extensão, sem implementação declarada neste repositório. Preservar DTOs, autoridade de acesso e fronteiras de envio ao avaliar cada proposta.                                                                                       | [Arquitetura](architecture.md); [plano histórico](history/plans/plan.md#depois-do-piloto)                                                                       |

Campanhas de envio, jornadas automatizadas, importação de histórico e gestão de
participantes de grupo permanecem fora do escopo entregue. Não são necessários para usar
a caixa atual. O [README](../README.md) registra as capacidades efetivas.

## Integrações que precisam de validação própria

### Marketing e CRM

A associação explícita conversa–CRM e as pontes de evidência já existem. A evolução de
campanha externa → UTMs nativas e do lookup GCLID pertence ao Marketing Center e ainda
não está implementada como fluxo do produto. Não reproduzir essa política nos adapters
de mensagens nem interpretar `fbads` como autorização comercial. Acompanhe o
[guia entre projetos](https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md).

### Retenção e filas

Retenção por prazo, preparação de eventos legados, saneamento dos jobs relacionados e
proteção contra replay já estão implementados. Não constituem um projeto ainda por
começar. O trabalho recorrente é diagnosticar lotes adiados e validar contratos de
expiração de novos consumidores, mantendo envios pendentes/incertos e registros
comerciais protegidos. Nunca substituir esse diagnóstico por uma limpeza global da fila.

A [extensão de dependências](../contact_center_base/models/retention_dependencies.py)
recusa relações comerciais restantes sem contrato. A simulação informa registros, não
uma promessa de bytes físicos liberados; a coleta do filestore pertence ao Odoo. O
[estudo de retenção](../research/message-retention-proposal-2026-09-11.md) conserva
requisitos originais e um resumo da implementação.

### Provedores, áudio e interface

- Revalidar fixtures, permissões, assinaturas de webhook e capacidades ao mudar o
  provedor ou sua versão. Uma evidência antiga de App Review pendente não comprova o
  estado atual de uma instalação; o gate deve ser conferido no ambiente alvo.
- A transcrição está implementada e começa desabilitada. Qualidade de reconhecimento,
  disponibilidade de credenciais e aceitação de codec pertencem ao backend selecionado.
  Limites de arquivo/duração não são uma quota mensal rígida; requisições externas podem
  voltar a ser cobradas após falha ambígua. Veja
  [operação de áudio](../operations/audio-transcription.md).
- Testes DOM e resultados nativos antigos não comprovam layout, reprodução ou QUnit da
  próxima entrega. Executar a aceitação proporcional sobre a fonte candidata, conforme
  [resiliência](../operations/resilience-capacity-drill.md) e
  [cutover](../operations/production-cutover-rollback.md).

Esses itens são critérios de validação, não afirmações de que produção está com defeito
ou de que algum recurso precisa ser reimplantado agora.

## Como ler o histórico

Os [reviews](history/reviews/) registram achados, refutações e correções sobre snapshots
específicos. “Histórico” não significa que todos os achados foram resolvidos. Para
reabrir um item, identifique o código atual, o comportamento ainda presente e a prova
necessária; preserve o registro anterior como evidência.

Não transferir automaticamente para este roadmap afirmações antigas de que faltam:

- início de conversa, notas internas, respostas rápidas, acompanhamento ou agendamento;
- painel comercial, associação direta conversa–CRM ou Kanban opcional;
- vínculos secundários entre pessoa e empresas;
- retenção por prazo, transcrição ou prévias de anúncio.

Esses recursos possuem implementação e contratos atuais. Melhorias específicas podem ser
propostas com evidência, sem reabrir a implantação inteira.
