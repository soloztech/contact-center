# Política de exibição de mensagem apagada — validação

Data: 2026-09-01  
Escopo: `contact_center_base` e `contact_center_ui`  
Ambiente: Odoo 16 neutralizado no SERVIDOR05

## Resultado

Foi implantado um flag por caixa, **Manter conteúdo riscado ao apagar**, desligado por
padrão. A política é copiada para cada mutação de delete quando ela é criada, portanto
uma mudança posterior da configuração não altera o significado histórico da exclusão.

- **Desligado (`redact`)**: a timeline mostra somente “Mensagem apagada”; corpo,
  `original_body`, reactions e anexos da projeção operacional são removidos, mídia
  pendente vira `discarded` e deixa de ser servida pelas rotas do Contact Center e por
  `/web/content`.
- **Ligado (`strike`)**: o corpo vigente é copiado para um snapshot próprio e a UI o
  mantém visível, atenuado e riscado, junto com a mídia e as reactions que pertenciam à
  mensagem. O `mail.message.body` canônico permanece como tombstone e as ações de
  reply/reaction/edit/delete ficam desabilitadas.
- **Histórico anterior (`legacy`)**: exclusões preexistentes não recebem retroativamente
  uma promessa de expurgo ou um conteúdo que já não pode ser reconstruído.

Os eventos técnicos de entrada/saída, IDs externos e ledgers de mutação permanecem
imutáveis para deduplicação, correlação e diagnóstico. O flag trata a projeção
operacional; não é um mecanismo de expurgo LGPD/forense.

## Robustez

- O job de download revalida a política depois do I/O do provider e bloqueia o binding
  antes de persistir bytes. Uma exclusão concorrente não consegue recriar a mídia.
- Conteúdo redacted invalida a rota autenticada, remove o anexo que alimentaria
  `/web/content` e usa cache privado com revalidação obrigatória.
- A UiDTO falha fechada: somente o booleano literal `true` autoriza conteúdo riscado;
  qualquer ausência ou valor inválido produz somente o tombstone.
- A confirmação do delete informa ao operador se o conteúdo será removido ou mantido
  riscado conforme a caixa selecionada.

## Gates

- `contact_center_base`: **405/405**, sem falhas ou erros;
- `contact_center_wuzapi`: **204/204**, sem falhas ou erros;
- suíte integrada base/WuzAPI/Meta/CRM/UI: **739/739**, sem falhas ou erros;
- QUnit UI minificado: **101/101**, **931/931** asserções;
- QUnit UI `debug=assets`: **101/101**, **931/931** asserções;
- Black, Flake8 seletivo, compilação Python, Prettier, ESLint, parse XML e
  `git diff --check`: aprovados;
- navegador autenticado: campo exibido na caixa real, aplicação e duas suítes QUnit sem
  erro de console.

O primeiro gate pre-upgrade encontrou um helper colocado na classe errada e dois
fixtures incompletos. O release foi interrompido antes do upgrade, restaurou a fonte e a
rota automaticamente e manteve o banco intacto. Após a correção, o release atômico
`20260901T084441053850Z` terminou com `applied_and_validated`.

## Evidências

- Release atômico:
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T084441053850Z`
- Campo na configuração da caixa:
  `/home/lucaszotelli/infra-ai-ops/output/playwright/contact-center-delete-policy-20260901/account-policy-field.png`

Versões publicadas: base `16.0.1.34.0`, WuzAPI `16.0.1.27.0`, Meta `16.0.2.0.0`, CRM
`16.0.2.0.1` e UI `16.0.1.22.0`. Produção não foi acessada nem alterada.
