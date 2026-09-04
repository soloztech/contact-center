# Reconciliação de envio por evidência positiva — 2026-09-02

## Sintoma e causa

Duas mensagens enviadas para uma conversa direta de laboratório apareciam em amarelo
como `uncertain`, apesar de terem sido entregues pelo WhatsApp. A WuzAPI respondeu
`200`, retornou IDs externos exatos e depois publicou receipts de entrega. A falha
ocorreu no Odoo depois do envio: o flush do ledger disputou a linha da conta com o
webhook e sofreu `SerializationFailure`. Por segurança, a outbox ficou ambígua e nunca
foi reenviada.

O conflito era amplificado porque cada webhook WuzAPI usava o lock mutante de topologia
e incrementava `access_topology_revision`, mesmo sem alterar configuração.

## Correção

- O ingresso do webhook agora usa um fence de leitura em ordem
  `account -> provider connection`, preservando a proteção contra cutover/HMAC sem
  escrever na conta a cada callback.
- Receipt exato, eco `from_me` e receipt de participante de grupo passam a concluir uma
  outbox `pending`, `retry` ou `uncertain` como `done`, sem nova chamada ao provider.
- Se o worker ainda estiver na fronteira `processing`, o evento espera/reexecuta em vez
  de disputar a finalização.
- O fluxo de grupo mantém a ordem global
  `account -> connection -> channel -> outbox -> message binding`, eliminando um
  possível ABBA entre receipt rápido e finalização do worker.
- A migração `16.0.1.37.0` reconciliou somente legados com prova forte: comando de envio
  `uncertain`, delivery `sent/delivered/read` e ID externo não vazio.
- Na UI, delivery positivo prevalece sobre metadata antiga de dispatch; a classe amarela
  e o aviso são removidos tanto no carregamento quanto por atualização em tempo real.

## Resultado no laboratório

- As outboxes `30`, `31` e `32` ficaram `done`; seus bindings permanecem `delivered`.
- A consulta de outbox `uncertain` com delivery positivo e ID externo retornou zero.
- Na conversa do ensaio, as duas mensagens aparecem com `✓✓ Entregue`, sem o aviso.
- Nenhum reenvio externo foi executado pela correção ou pela validação.
- Versões finais: base `16.0.1.37.2`, WuzAPI `16.0.1.28.0` e UI `16.0.1.25.3`.

## Validação

- Base: **415/415**.
- WuzAPI: **204/204**.
- Integração: **764/764**.
- QUnit: **115/115** testes e **1069/1069** asserções, tanto minificado quanto
  `debug=assets`, sem erro ou warning de console.
- Revisão adversarial do caminho direto e de grupos: nenhum bloqueador.
- Screenshot funcional preservado na evidência bruta local, fora do Git.
- Release canônico final:
  `scans/raw/20260903-cc-positive-delivery-reconciliation-r5-release`.

Produção não foi acessada nem alterada.
