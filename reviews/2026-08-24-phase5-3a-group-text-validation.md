# Validação da Fase 5.3a — texto outbound em grupos

- Data: 2026-08-24
- Ambiente: SERVIDOR05, banco neutralizado `odoo16`
- Produção: não acessada nem alterada
- Resultado: implantada e validada; nenhum envio real para grupo foi executado

## Escopo entregue

- Capability provider-neutral por tipo de conversa em
  `conversation_types.group.send_message`.
- Opt-in por conta, desligado por padrão; somente a caixa Lucas foi habilitada no lab.
- Composer de grupo restrito a texto novo, usando a conexão exata do profile e o JID
  canônico completo `@g.us`.
- Correlação por `client_message_id` entre ledger, `CommandDTO`, payload WuzAPI,
  resposta e eventual eco `from_me`.
- Mensagens enviadas por aparelho próprio são projetadas uma única vez com o autor
  técnico da conta.
- Mídia, reply, reaction, edit, delete e receipts de grupo permanecem bloqueados em
  todas as camadas.

## Hardening da revisão final

Os commits da entrega são:

- `eb6bd09` — capability, opt-in, composer textual, adapter WuzAPI e testes;
- `72c8134` — isolamento cross-conversation e revalidação do ledger no boundary.

A revisão final encontrou e fechou dois riscos médios antes da homologação:

1. eco de grupo desconhecido não pode usar uma busca global por ID dentro da conexão;
2. dispatch exige que conexão e os três `client_message_id` persistidos coincidam.

Testes adversariais cobrem grupo desconhecido com ID colidente, troca de conexão do
binding e adulteração dos IDs superior e interno do comando.

## Versões e testes

- OCA `queue_job`: `16.0.3.0.2`;
- `contact_center_base`: `16.0.1.12.1`;
- `contact_center_wuzapi`: `16.0.1.9.0`;
- `contact_center_ui`: `16.0.1.10.0`;
- tree hash: `36d68c66e1a42981a79e0856031234489abe109838affa3a7909cbea67c71a65`;
- Odoo base: **153/153**, zero falha/erro;
- Odoo integrado: **230/230**, zero falha/erro;
- QUnit UI: **47/47**, 419/419 asserções, minificado e `debug=assets`;
- QUnit JSON viewer: **4/4**, 15/15 asserções, minificado e `debug=assets`.

## Prova no laboratório

- As cinco conexões permaneceram `connected`; a UI mostrou `5/5 conectadas`.
- Todas as conexões WuzAPI publicam suporte a texto de grupo, mas somente a conta
  `WhatsApp Lucas - Laboratorio` possui o opt-in ativo.
- Um grupo da caixa Lucas exibiu textarea e botão de envio habilitável por conteúdo; o
  botão de anexos permaneceu desabilitado. O texto local de validação foi apagado sem
  envio.
- Um grupo da caixa Comercial03 exibiu `Envio não habilitado` e nenhum composer.
- A sessão limpa terminou com zero erro e zero warning no console.
- Permaneceram **0** outboxes de grupo e **0** message bindings de grupo com origem
  `agent`, comprovando que o smoke não enviou mensagem externa.
- Logs desde o restart final: zero `ERROR/CRITICAL`, zero HTTP 500, zero `PoolError` e
  zero request exception. Os tracebacks observados eram apenas logs `DEBUG` do storage
  de sessões para cookies de automação já ausentes, sem erro operacional.

## Evidências

- Deploy final:
  `scans/raw/20260824-odoo16-contact-center-phase5-3a-group-text/deploy-hardening/`.
- Testes finais:
  `scans/raw/20260824-odoo16-contact-center-phase5-3a-group-text/test-hardening/`.
- Upgrade final:
  `scans/raw/20260824-odoo16-contact-center-phase5-3a-group-text/upgrade-hardening-fix/`.
- Validate final:
  `scans/raw/20260824-odoo16-contact-center-phase5-3a-group-text/validate-final/`.
- Navegador: `output/playwright/phase5-3a-group-text/`.

## Próximo passo

A Fase 5.3b implementará mídia outbound e reply de grupo. Reply só poderá sair quando o
participante protocolar do alvo estiver persistido; ausência deve falhar fechado, sem
degradar silenciosamente para uma mensagem comum.
