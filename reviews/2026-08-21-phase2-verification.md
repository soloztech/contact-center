# Verificação da Fase 2 (UI de atendimento) + status dos gaps anteriores

- Data: 2026-08-21 (final da noite)
- Revisor: Claude (auditor)
- Versões no lab SERVIDOR05: base `16.0.1.3.1`, ui `16.0.1.3.3`, wuzapi `16.0.1.2.0`,
  queue_job `16.0.3.0.2`
- Evidência: incident `2026-08-21-odoo16-contact-center-phase2-ui.md`,
  `scans/raw/20260821-odoo16-contact-center-phase2-ui/`, capturas em
  `output/playwright/`, leitura JSON-RPC do lab.

## Veredito

**Fase 2 entregue e dentro do plano.** Aceites do plano para a Fase 2, verificados:

| Aceite                                                                               | Estado             | Evidência                                                                                                                                                 |
| ------------------------------------------------------------------------------------ | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Nenhum patch/import privado de Discuss ou `im_livechat`                              | ✅                 | imports só de `@odoo/owl` e `@web/core/*` (registry, hooks, l10n/dates, browser)                                                                          |
| Não existe sincronização entre dois modelos de conversa                              | ✅                 | store só chama `contact.center.ui.api` (13 métodos); `mail.channel`/`mail.message` canônicos                                                              |
| Somente `send_message(UiDTO)` cria outbox                                            | ✅                 | único `outbox.create` em `_send_message`; UUID de requisição idempotente por conversa                                                                     |
| Promoção não remove guest/membership nem reescreve histórico                         | ✅ (teste parcial) | `link_partner`/`create_and_link_partner`/`unlink_partner`; allow-list de campos e empresa derivada; falta assert explícito do `mail_guest_id` preservado  |
| Lista, timeline, composer, unread, equipe, responsável, transferência, tags, estados | ✅                 | `update_conversation` reconcilia `mail.channel.member` ao trocar equipe/responsável; `claim_conversation`; bus próprio `contact_center/event` por partner |
| Tempo real por bus próprio e estável                                                 | ✅                 | `_notify_ui` → `contact_center/event`; store trata reconnect/disconnect                                                                                   |
| Testes                                                                               | ✅                 | 42/42 base, 70/70 integrado, 15 QUnit (59 asserções); console sem erros em bundle minificado e `debug=assets`; mobile 390×844                             |

Uso real no lab: 3 conversas (uma `resolved`, uma com responsável atribuído), 31
mensagens, 4 envios pela UI (`done`, receipts `sent→delivered→read`), 42 inbox `done`.

## Status dos gaps da verificação anterior

| Gap (fase 1)                                                                       | Agora                                                                                                                              |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Atribuição/transferência inexistente                                               | **Resolvido** — `update_conversation` + `claim_conversation` com reconcile de membership e notificação aos membros antigos e novos |
| Incident da Fase 1 ausente                                                         | **Ainda ausente** (só 0, 0.1 e 2)                                                                                                  |
| Código não versionado                                                              | **Ainda untracked** (`odoo16/addons/` inteiro, sem repo `contact-center`)                                                          |
| Conexão não se recupera sozinha (sem cron, `Connected/Disconnected` → unsupported) | **Não alterado** (`last_health_at` parado em 21:11)                                                                                |
| Receipts órfãos → `dead` após 12 tentativas                                        | **Não alterado** — agora 8 `dead`, todos "receipt antes da correlação"                                                             |
| Throttle/jitter por conexão                                                        | **Não alterado**                                                                                                                   |
| Raw payload integral                                                               | **Não alterado**                                                                                                                   |
| PII real no lab                                                                    | **Aumentou** — 178 eventos de grupo/broadcast `unsupported` com payload bruto de conversas pessoais e 3 identidades reais          |

## Sugestões de interface (prioridade por impacto)

### Correções

1. **Avatar do inbound aparece abaixo do balão** (visível na captura desktop: chip "GZ"
   solto sob cada bolha). Causa: `.cc-message { align-items: flex-end }` alinha o avatar
   ao fim do `.cc-message__content`, que inclui o botão "Responder" oculto com
   `opacity: 0` (ocupa altura). Corrigir com `align-items: flex-start` + `margin-top`
   para alinhar ao cabeçalho da bolha, ou retirar o botão do fluxo (`position: absolute`
   / `visibility: hidden; height: 0`).
2. **Agrupar mensagens consecutivas do mesmo autor**: hoje cada bolha repete nome +
   `WHATSAPP · WUZAPI` e avatar. Mostrar cabeçalho/avatar só na primeira do grupo (mesmo
   autor dentro de ~5 min) e reduzir o espaçamento interno; o rótulo de provider pode ir
   para o cabeçalho da conversa apenas.
3. **Balões largos demais para texto curto** em desktop: `max-width: 78%` com
   `min-width: 6.5rem`; reduzir para ~60% em ≥1280px e deixar a largura seguir o
   conteúdo. Rodapé hora/✓✓ em 0.55rem é pequeno demais — 0.68rem.
4. **Área vazia grande entre a timeline e o composer** quando há poucas mensagens:
   ancorar as mensagens ao fundo (`margin-top: auto` no `.cc-timeline__inner`) para a
   conversa "crescer" de baixo para cima, como apps de chat.

### Clareza operacional

5. **Estado da conversa** (Open/Pending/Resolved) como botões no cabeçalho é pouco
   legível e não está traduzido — usar chips "Aberta / Pendente / Resolvida" com cor de
   estado e uma ação primária explícita ("Resolver") à direita; manter seletor em
   mobile.
6. **Indicar o responsável na conversa aberta** (hoje só aparece na lista e no drawer):
   chip com avatar do agente ao lado do nome, clicável para "Assumir/Transferir".
7. **Contador de não lidas** na lista: aparece como `<b>` pequeno no preview; usar badge
   de cor de destaque à direita da hora, e negrito no nome enquanto houver não lidas.
8. **Filtros da lista**: "Todas/Open/Pending/Resolved" mistura idiomas; adicionar
   contadores por estado e um filtro "Minhas" (responsável = eu) e "Sem responsável",
   que são os dois recortes operacionais mais usados.
9. **Ícones de entrega**: hoje texto (✓ ✓✓) no rodapé; adicionar cor para `read` e
   tooltip com horário de cada transição (os `delivery.event` já existem) e um estado
   visível para `uncertain`/`failed` com ação "Reenviar".
10. **Eventos de sistema na timeline** (atribuição, transferência, mudança de estado,
    vínculo de contato): hoje invisíveis; mostrar como linhas discretas centralizadas,
    usando `message_type='notification'` interno ao canal.

### Polimento visual

11. **Tipografia**: `Bahnschrift`/"Arial Narrow" condensada nos títulos não existe em
    Linux/macOS e cai para Segoe/Arial; usar a pilha do Odoo (`Roboto`/system-ui) para
    coerência com o resto do backend.
12. **Rail esquerdo de 3.4rem** com "CC", pulso e "tempo real" consome largura sem
    função: mover o indicador de tempo real para o cabeçalho da lista (já existe um
    ponto lá) e remover o rail em desktop.
13. **Contraste/cor**: a paleta cinza-azulada é discreta, mas inbound (branco, borda
    cinza) e outbound (azul-claro) diferem pouco à distância; dar ao outbound um fundo
    mais saturado (`--cc-cobalt-soft`) e manter o inbound branco.
14. **Drawer de detalhes**: "Vincular existente / Criar contato" em dois botões grandes
    de mesmo peso; deixar "Vincular existente" primário e "Criar contato" como link. Os
    identificadores (`5519…@s.whatsapp.net`) são úteis para suporte, mas devem ficar
    colapsados por padrão e com botão copiar.
15. **Composer**: hint "Enter envia · Shift+Enter quebra linha" sempre visível vira
    ruído; mostrar apenas no foco. Adicionar contador/aviso quando a conexão estiver
    pausada (o botão já desabilita, mas sem explicar o motivo visível — só tooltip).
16. **Estados vazios e erro** estão bem resolvidos (skeleton, retry, "Posto
    disponível"); manter.

## Reavaliação e disposição das sugestões

Reavaliado no código e no navegador em 2026-08-21/22. Foram incorporados os pontos que
eram verificáveis e independiam de novos contratos de backend:

| Item           | Disposição                                                                                                                               |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| 1              | **Implementado** — avatar inbound alinhado ao início do balão; diferença medida de 0 px entre os topos.                                  |
| 3              | **Implementado com ajuste conservador** — mensagens limitadas a 68% em desktop largo e rodapé elevado para 11 px.                        |
| 4              | **Implementado** — timeline curta cresce a partir da base e permanece próxima ao composer.                                               |
| 5 e 8 (idioma) | **Implementado parcialmente** — `Aberta/Pendente/Resolvida`, com cores; contadores e filtros operacionais dependem de extensão do DTO.   |
| 12             | **Implementado** — rail removido; status real do bus ficou explícito e acessível no cabeçalho da inbox.                                  |
| 14             | A hierarquia `Vincular existente` + `Criar contato` como link **já existia**; colapso/cópia de aliases fica para um refinamento próprio. |
| 15             | **Implementado parcialmente** — hint aparece somente no foco; motivo de bloqueio do envio fica para o estado de saúde/capability.        |

Também foi aplicada uma escala desktop explícita de 11–15 px sem alterar a densidade
mobile aprovada. Os itens 2, 6–10 exigem agrupamento, novos filtros/eventos ou dados que
o UiDTO v1 ainda não expõe; não devem ser simulados apenas no CSS. Os itens 11 e 13 são
decisões de identidade visual, não defeitos funcionais, e permanecem para comparação com
usuários. PII/LGPD não integra o escopo atual por decisão do projeto.

Validação posterior: 42 testes base, 70 integrados, QUnit 15/15 com 59/59 asserções,
bundles minificado e `debug=assets` sem erros de console, além de desktop 1903 px e
mobile 390 px registrados em `output/playwright/contact-center-ui-v133-*.png`.
