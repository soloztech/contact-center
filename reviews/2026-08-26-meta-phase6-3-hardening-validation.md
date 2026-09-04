# Meta Fase 6.3 — hardening e validação

Data: 2026-08-26  
Alvo: SERVIDOR05, Odoo 16 laboratório (`odoo16-teste.soloz.com.br`)  
Produção: não acessada

## Origem e disposição

O chat paralelo `definir arquitetura whatsapp...` encerrou a entrega de referrals
autônomas de Messenger/Instagram, `AttributionDTO`, ledger imutável e validação no
laboratório. A revisão posterior não encontrou P0 e confirmou cinco lacunas P1. Todas
foram incorporadas antes de avançar:

1. edit do Instagram passa pelo mesmo ledger de revisão monotônica do Messenger;
2. delivery do Messenger aceita `watermark` sem `mids`, aplica ambos quando coexistem e
   persiste cursor cumulativo para echoes tardios;
3. download por URL CDN assinada não depende do token Graph, enquanto profile sync
   continua exigindo autorização válida;
4. conversa direta iniciada no dispositivo agenda profile sync imediatamente;
5. sticker/transições e conteúdo compartilhado documentado são normalizados sem expor
   URL privada.

## Arquitetura aplicada

- Novo `contact.center.delivery.watermark`, único por
  `(provider_connection_id, channel_binding_id, state)` e monotônico por tempo.
- Advisory lock transacional serializa receipt e correlação tardia sem criar uma ordem
  inversa de locks ORM.
- Um binding outbound direto com ID externo reaplica cursors elegíveis pela data da
  mensagem. O ledger de `contact.center.delivery.event` permanece idempotente.
- Payload com IDs explícitos e watermark não escolhe uma única lane: aplica os alvos
  conhecidos, persiste o cursor e deixa alvos ainda ausentes convergirem no echo.
- `share`/`ig_post`, `story_mention` e `ig_reel`/`reel` viram conteúdo interativo com
  rótulo visível e snapshot provider-neutral. Locators assinados permanecem somente no
  vault privado até consumo/expiração.

## Implantação

| Módulo                  | Versão        |
| ----------------------- | ------------- |
| `contact_center_base`   | `16.0.1.22.0` |
| `contact_center_meta`   | `16.0.1.3.0`  |
| `contact_center_wuzapi` | `16.0.1.17.0` |
| `contact_center_ui`     | `16.0.1.17.1` |
| `queue_job`             | `16.0.3.0.2`  |

Tree hash final implantado:
`a2d4f5e89ed281754a95b89d2d3049eebf095085a20974dbd189b7e3bf8dbbd9`. Somente
`contact_center_base` e `contact_center_meta` exigiram upgrade. O backup do banco foi
omitido deliberadamente no laboratório; registros de negócio permaneceram inalterados.

## Testes

- Base isolado: **278/278**, zero falha, zero erro.
- Integrado base + WuzAPI + Meta: **485/485**, zero falha, zero erro.
- Bancos temporários e data directories foram removidos pelo executor.
- A primeira rodada teve uma única expectativa incorreta em teste novo: vincular um
  `res.partner` não deve renomear a identidade técnica. A UI usa `partner.display_name`;
  o teste foi corrigido sem alterar código funcional.
- `black`, `isort`, compilação Python, `flake8` dos arquivos alterados e
  `git diff --check` passaram. Permanece o C901 histórico de
  `_apply_group_delivery_event` (complexidade 17), fora deste patch funcional.

Evidências:

- `scans/raw/20260826-odoo16-contact-center-phase63-p1-deploy-r2`
- `scans/raw/20260826-odoo16-contact-center-phase63-p1-upgrade`
- `scans/raw/20260826-odoo16-contact-center-phase63-p1-tests-r2`
- `scans/raw/20260826-odoo16-contact-center-phase63-p1-validate`

## Estado externo Meta

Read-back Graph `v26.0` confirmou que o Page token é válido, do tipo `PAGE` e pertence
ao app dedicado `Soloz Contact Center`.

- Page/Messenger: `message_deliveries`, `message_echoes`, `message_edits`,
  `message_reactions`, `message_reads`, `messages`, `messaging_postbacks`,
  `messaging_referrals`.
- Instagram: `message_reactions`, `messages`, `messaging_postbacks`,
  `messaging_referral`, `messaging_seen`.

Nenhum token, secret ou URL privada foi exibido ou gravado. A credencial local do app
legado de Lead Ads está vazia, então esta rodada independente não pôde reler seu
`leadgen`; a operação anterior atuou somente no app dedicado. A comprovação externa do
legado deve ser repetida quando a credencial operacional estiver disponível.

## Pendências de aceite externo

- Enviar imagem, áudio, vídeo e PDF reais por Messenger/Instagram e observar download,
  retry e expiração.
- Gerar reaction, edit/unsend, delivery/read/seen e postback reais em cada plataforma.
- Repetir o ingresso com remetente sem app role e concluir publicação/App Review.
- Implementar health/reconciliação automática de token, scopes, tasks, ativos e drift
  das subscriptions na Fase 6.5.
