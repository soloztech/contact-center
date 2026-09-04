# Disposição da auditoria — ordenação, mídia e tempo real

- Data: 2026-08-23
- Escopo: reavaliar M7, M8, M10, M11, baixos selecionados e pendências de processo
- Alvo de implantação: somente SERVIDOR05, banco neutralizado `odoo16`
- Versões: base `16.0.1.8.0`, WuzAPI `16.0.1.6.0`, UI `16.0.1.6.0`
- Status: implantado e validado no laboratório

## Veredito

M7, M8, M10 e M11 eram achados válidos. M7/M8 foram corrigidos como regras de domínio;
M10 recebeu uma redução mensurável de memória sem mudar o contrato do provider; M11 foi
corrigido no store e no componente da timeline. Parte do backlog baixo já podia ser
resolvida no mesmo release. Itens que exigem streaming verdadeiro, novo cursor de API,
expurgo destrutivo ou ensaio externo permaneceram explícitos.

## Aplicado

- **M7:** edit e reaction agora são monotônicos por `occurred_at` e ID local. Reaction
  usa uma lane por ator; delete é terminal e edit posterior vira no-op aplicado. O
  binding é bloqueado antes da projeção e eventos superados ficam auditáveis.
- **M8:** eco `from_me` usa o autor técnico da conta, não o autor da mensagem-alvo. A
  busca do alvo aceita ID externo ou `client_message_id`, rejeita ambiguidade e valida a
  coerência entre `direction` e `is_from_me`.
- Reações nativas do Discuss foram bloqueadas em mensagens Contact Center; reação
  externa passa exclusivamente pelo ledger/outbox.
- **M10:** outbound lê `ir.attachment.raw` e executa validações de metadados antes de
  materializar o conteúdo. O binário é codificado em base64 uma vez no adapter. Em
  ensaio isolado de 50 MiB, o pico caiu de aproximadamente 400 MiB para 281 MiB.
- **M11:** refresh em tempo real faz merge incremental, preserva histórico e cursor de
  paginação e ignora eventos de outro canal. A timeline acompanha o final somente se o
  operador já estava próximo dele; caso contrário, mantém o scroll e mostra o contador
  de novas mensagens.
- Sanitização WuzAPI passou a remover recursivamente qualquer chave `*sidecar` e valor
  `data:`. Não foi adotada uma allow-list rígida sem inventário completo do provider.
- Edit após delete virou no-op, lookup de mutação aceita `client_message_id`, preview da
  lista usa rótulo humano, `create_contact` reflete a capacidade já autorizada, upload
  pode ser abortado de verdade e regiões `aria-live` amplas foram substituídas por um
  anúncio granular.
- A rota de mídia deixou de decodificar `attachment.datas` e usa `attachment.raw`,
  preservando ACL, CSP, Range e validações existentes.

## Concorrência e boundary externo

O teste com dois cursores comprovou uma particularidade correta do Odoo/PostgreSQL em
`REPEATABLE READ`: a transação antiga recebe `SerializationFailure` ao aguardar um
binding atualizado pela concorrente. Inbox refaz a transação inteira por
`RetryableJobError` e converge. Retry dentro de `_apply_projection` seria incorreto por
reutilizar a snapshot antiga.

Após o boundary durável do outbound, a mesma falha permanece `uncertain` e nunca causa
novo I/O no provider. Isso evita duplicar uma mutação já aceita. Continua no backlog uma
reconciliação exclusivamente local da outbox `uncertain` quando um eco correlacionável
confirmar a operação.

## Refutado ou adiado deliberadamente

- O claim de que os hotspots de complexidade e os dois XML ainda estavam abertos era
  desatualizado; eles já haviam sido corrigidos e os gates permanecem limpos.
- Receipts/mutações com prova de grupo já terminam como `unsupported`. Sem essa prova,
  inferir grupo pelo ID incompleto poderia descartar evento direto válido; não foi
  criado curto-circuito inseguro.
- Streaming HTTP verdadeiro do conteúdo e envio por URL curta assinada permanecem para
  um release próprio. `attachment.raw` reduz cópias, mas ainda materializa o arquivo.
- Reconexão com lacuna superior a 100 mensagens exige cursor forward no backend. O merge
  atual não perde histórico já carregado, mas não promete recuperar uma lacuna
  ilimitada.
- Migração ampla de todos os textos JavaScript para `_t` e separação física dos arquivos
  grandes continuam como refatorações estruturais, sem bloquear este incremento.
- Expurgo de PII não foi executado: é destrutivo e requer política de retenção e escopo.
- Mídia inbound real e queda real de celular continuam ensaios coordenados do piloto;
  não foram simulados por chamadas externas nesta implantação.
- A capacidade `root:4,root.contact_center.health:2` já havia sido aplicada e observada
  no laboratório no marco M4; deverá ser medida novamente para a topologia do piloto.

## Validação

- testes Odoo isolados: **104/104** no base e **159/159** integrados;
- QUnit autenticado, minificado e `debug=assets`: UI **32/32 testes e 214/214
  asserções**; viewer base **4/4 e 15/15**;
- navegador real: 25 conversas carregadas, frota **5/5 conectada**, previews humanos de
  documento/áudio/vídeo e zero erro ou warning de console;
- árvore implantada: `68812fa2b4fdc866c672de3f8b6e71f0e7bfa8be897f10a8b6f5f43bce742546`;
- upgrade sem backup por decisão explícita para o laboratório descartável;
- nenhuma mensagem WhatsApp foi enviada e produção não foi acessada nem alterada.

## Processo

O addon agora é um repositório Git independente na branch `16.0`, com commit inicial e
tag local `16.0.1.8.0-lab`. Artefatos do Playwright e caches não entram no
versionamento, e o repositório pai ignora o diretório independente. O remoto
`soloztech/contact-center` ainda não existe; nenhum remote foi configurado e nada foi
enviado ao GitHub.

Evidências principais:

- deploy:
  `scans/raw/20260823-odoo16-contact-center-phase3-audit-followup/deploy/20260824T002238324716Z/`;
- testes:
  `scans/raw/20260823-odoo16-contact-center-phase3-audit-followup/test/20260824T002304929941Z/`;
- upgrade:
  `scans/raw/20260823-odoo16-contact-center-phase3-audit-followup/upgrade/20260824T002417681782Z/`;
- validação:
  `scans/raw/20260823-odoo16-contact-center-phase3-audit-followup/validate/20260824T002451078306Z/`;
- navegador/QUnit:
  `odoo16/addons/contact-center/output/playwright/phase3-audit-followup/`.
