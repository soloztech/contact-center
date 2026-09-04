# Validação da Fase 5.1 — grupos inbound somente leitura

- Data: 2026-08-24
- Ambiente: SERVIDOR05, banco neutralizado `odoo16`
- Produção: não acessada
- Backup: não executado, conforme fluxo rápido acordado para o laboratório

## Escopo entregue

- Core provider-neutral com um canal por conta + referência do grupo e
  `identity_id=False`.
- Participantes resolvidos separadamente como identity + `mail.guest`; autores
  persistidos em `mail.message.author_guest_id`.
- Adapter WuzAPI separando Chat `@g.us` de Sender/SenderAlt PN/LID e rejeitando
  broadcast, protocolo sem conteúdo, `from_me`, receipts e mutações de grupo nesta fase.
- UI com badge de grupo, autor no preview/timeline e estado somente leitura.
- Guards de servidor para envio, upload, reply, reaction, edit, delete, outbox e
  dispatch.

## Revisão e testes

- Revisão adversarial final: nenhum achado alto ou médio aberto.
- Gates estáticos: Black, Flake8, pylint obrigatório, OCA checks, ESLint, Prettier e
  `git diff --check` sem erro.
- Odoo isolado: **121/121** testes do core e **181/181** integrados, incluindo corrida
  real em dois cursores na criação do primeiro canal do grupo.
- QUnit minificado e `debug=assets`: UI **35/35**, 237/237 asserções; viewer base
  **4/4**, 15/15 asserções.

## Implantação e prova real

- Versões: base `16.0.1.9.0`, WuzAPI `16.0.1.7.0`, UI `16.0.1.7.0` e OCA queue_job
  `16.0.3.0.2`.
- Tree hash implantado:
  `52d533492f4198c035f51ee3f4620097f42ce4cfb204461fc1d9640c3100f032`.
- Dry-run transacional, upgrade e validação concluídos; bancos temporários removidos.
- Um webhook real de grupo terminou `done`: um binding ativo, sem identidade própria, um
  alias `group`, uma mensagem com guest autor, zero duplicata, zero outbox e zero evento
  de grupo não concluído.
- Browser: 28 conversas visíveis, uma de grupo, rodapé somente leitura, zero composer,
  participante visível, frota 5/5 conectada e zero erro/warning de console.

## Backlog baixo

- Validação sintática mais estrita dos JIDs WuzAPI.
- Teste integrado de mídia de grupo cobrindo projeção + download em uma única prova.
- Ledger específico para conflito de roteamento entre canais.
- Regressão provider-neutral em que grupo e participante compartilham namespace, mas
  possuem valores e papéis distintos.
