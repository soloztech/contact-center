# Identificação de mensagem encaminhada — validação de 2026-09-01

## Resultado

A sinalização de mensagem encaminhada foi implantada de ponta a ponta no laboratório. O
marcador fica dentro da bolha e precede reply, mídia ou texto, seguindo a hierarquia
visual do WhatsApp sem transformar a informação em badge de destaque.

Versões deste corte:

- `contact_center_base 16.0.1.33.0`;
- `contact_center_wuzapi 16.0.1.27.0`;
- `contact_center_ui 16.0.1.21.0`.

## Contrato adotado

- `MessageDTO.is_forwarded` é booleano estrito e tem `false` como default compatível.
- `MessageDTO.forwarding_score` é uma evidência técnica opcional, inteira e limitada.
- O adapter WuzAPI consulta somente o `contextInfo` do conteúdo ativo.
- A mensagem é marcada somente quando `isForwarded` é exatamente `true`.
- Um score positivo sem a flag não marca a mensagem. Um encaminhamento presente apenas
  dentro de `quotedMessage` também não marca a mensagem externa.
- O binding canônico faz merge monotônico durante replays: `true` não é rebaixado e o
  maior score observado é preservado. Isso permite enriquecer registros antigos sem
  criar outra `mail.message`.
- A UiDTO entrega apenas `is_forwarded`; score e contexto bruto continuam internos.
- A timeline omite o marcador em mensagens apagadas e valida o booleano estritamente.

Essas decisões evitam dois falsos positivos comprovados nos payloads do laboratório:
mensagens com `forwardingScore` sem `isForwarded` e respostas cujo conteúdo citado era
encaminhado, embora a resposta atual não fosse.

## Prova com tráfego real

O evento de laboratório `38614` contém a flag no `imageMessage.contextInfo`. Ele foi
normalizado novamente pelo adapter e reaplicado pelo fluxo idempotente. O binding
`11443`, ligado à `mail.message` `245615`, foi enriquecido no próprio registro:

```text
before=(False, 0, False)
after=(True, 1, True)
```

Não foi criada mensagem duplicada. A interface autenticada exibiu a mesma foto e legenda
com “Encaminhada” em desktop e no viewport móvel `390 × 844`.

Evidências visuais:

- `/home/lucaszotelli/infra-ai-ops/output/playwright/contact-center-forwarded-20260901/forwarded-desktop.png`;
- `/home/lucaszotelli/infra-ai-ops/output/playwright/contact-center-forwarded-20260901/forwarded-mobile.png`;
- `/home/lucaszotelli/infra-ai-ops/output/playwright/contact-center-forwarded-20260901/qunit-debug-assets.png`.

## Gates executados

- release atômico `20260901T074721225118Z`;
- fonte local e checkout implantado revalidados com o mesmo hash
  `9cd7ed51243659cc269ae8dda0d5b340143cdb5edb1986f03a8a7c94602914da`;
- base: **399/399**;
- WuzAPI: **204/204**;
- integração: **733/733**;
- QUnit minificado: **100/100**, **887/887** asserções;
- QUnit `debug=assets`: **100/100**, **887/887** asserções;
- console da aplicação e das duas suítes: zero erro e zero aviso;
- HTTP público do laboratório: `200` após o release;
- Black, Flake8 seletivo, isort, Prettier, compilação Python, parse XML e
  `git diff --check`: aprovados.

Evidência do release:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T074721225118Z`

Revalidação read-only do checkout remoto:

`/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260901T080248440611Z`

Produção não foi acessada nem alterada.
