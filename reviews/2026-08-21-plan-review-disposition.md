# Disposição da revisão de arquitetura — 2026-08-21

Esta é a decisão aplicada sobre
[`2026-08-21-plan-review.md`](2026-08-21-plan-review.md). O documento original permanece
inalterado como parecer do auditor.

## Adotado agora

- Suprimir somente `_channel_message_notifications()` para `contact_center`, mantendo
  `_notify_thread()` nativo; cobrir edit/reaction/delete na Fase 3.
- Usar bus e operações `mark_fetched/mark_seen` próprios da UiDTO.
- Criar canais por factory que remova membership implícita do usuário criador.
- Chamar `ir.cron._trigger()` na mesma transação do inbox/outbox; o wake-up ocorre no
  post-commit. Persistência de webhook com falha retorna 5xx.
- Resolver DM por `(account, identity)` e prever consolidação/redirect explícito de
  canais duplicados; grupos continuam por identificador de conversa.
- Incluir endereços e snapshot protocolar no `CommandDTO`.
- Usar `client_message_id` pré-atribuído para correlação, sem prometer idempotência do
  WuzAPI.
- Tratar membership como concessão de acesso que precisa ser sincronizada em atribuição
  e transferência.
- Fixar `message_type='comment'`, `mail.mt_comment` e `date=occurred_at`; nota interna
  usa `mail.mt_note`, não tem binding e nunca gera outbox.
- Adotar pause por conexão, throttle mínimo, backoff com jitter, receipt fora de ordem
  em retry, testes concorrentes com dois cursores e métricas já na Fase 1.
- Impedir base64 no inbox: `-skipmedia` no piloto de texto e S3-only na fase de mídia.
- Usar nomes neutros `channel_id`/`message_id`, não expor membership na UiDTO e
  registrar o mapa de porte `mail.channel → discuss.channel`.
- Definir nome obrigatório do canal, coerência company/account/identity, metas de
  latência/piloto e websocket de homologação como precondição da Fase 2.
- Marcar o runbook antigo de `engagement.conversation` como superado e manter OCA
  `mail_gateway` apenas como referência.

## Não adotado agora

- LGPD, anonimização, pedido de titular e cron de expurgo: fora do escopo por decisão do
  operador. Permanecem apenas limites técnicos de payload e armazenamento de mídia.
- Override global de `mail.guest.unlink()`: `ondelete='restrict'` já protege a
  integridade; uma mensagem de erro amigável pode ser adicionada depois.
- Tratar `client_message_id` como idempotência externa: WuzAPI `v1.0.8` chama
  `SendMessage` novamente em retry com o mesmo ID; timeout ambíguo continua `uncertain`.

## Correção de baseline

A revisão consultou WuzAPI `919c72c9`, mas o ambiente está fixado em `v1.0.8`, commit
`9487eca`. ID customizado, `markread`, reaction, edit e delete existem na versão
implantada; fixtures e testes do adapter ficam pinados nela. Nenhum comportamento de
`main` será assumido sem revalidação.
