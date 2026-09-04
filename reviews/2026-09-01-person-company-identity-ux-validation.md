# Identidade person-first e contexto empresarial — validação do corte

Data: 2026-09-01  
Escopo: `contact_center_base 16.0.1.32.0` e `contact_center_ui 16.0.1.20.1`

## Decisão

- Pessoa é o vínculo padrão da identidade externa.
- Empresa direta é uma exceção explícita, restrita a supervisor, para número central
  compartilhado sem interlocutor fixo.
- Pessoa e empresa principal usam a relação nativa `res.partner.parent_id`.
- `contact.center.identity.partner_link_kind` persiste a intenção `person` ou
  `central_company`; ela não é inferida de um campo mutável do cadastro.
- Integrações futuras devem consumir `partner_id.commercial_partner_id` para obter o
  contexto comercial sem perder a identidade pessoal.
- Vincular ou desvincular nunca converte nem remove o `mail.guest`; aliases,
  memberships, mensagens e autoria histórica permanecem intactos.

## Contrato coberto pelo corte

- O fluxo pessoal rejeita `is_company=True`.
- Busca, criação e vínculo de número central aceitam somente empresa e exigem papel de
  supervisor, além do escopo normal de empresa e caixa.
- Operações de vínculo são idempotentes, usam bloqueio da identidade e rejeitam troca
  concorrente ou desvínculo baseado em estado obsoleto.
- O desvínculo exige o ID esperado, aceita replay inofensivo depois de concluído e
  mantém a exigência de supervisor para uma empresa central mesmo se o tipo do partner
  for alterado depois.
- Criações concorrentes informam se o cadastro foi realmente criado ou se outro vínculo
  venceu, evitando confirmação enganosa na interface.
- O perfil distingue pessoa de número central, orienta o uso correto e abre o
  `res.partner` nativo ao clicar no nome vinculado.
- Depois de identificar uma pessoa, a empresa principal fica disponível no mesmo painel,
  sem misturar os dois conceitos.

## Gates de fonte

- Inventário AST: base **398**, WuzAPI **203**, Meta **95**, CRM **35**; total integrado
  **731** testes Python.
- Suíte publicada da UI: **100** casos QUnit.
- Os pins correntes do laboratório e seus allowlists foram alinhados às versões deste
  corte. Nenhum script histórico ou evidência anterior foi reescrito.

## Aceite operacional no SERVIDOR05

- Release atômico aplicado em `20260901T072204332691Z`, com hash de fonte executável
  `b571c38e5f159925d358eb347533205d5209188cee3650c38b925e7cad054e97`.
- Gates remotos: base **398/398**, WuzAPI **203/203** e integração **731/731**, sem
  falhas ou erros.
- Upgrade offline concluído com status `0`; os cinco módulos ficaram instalados nas
  versões deste corte e a rota pública retornou HTTP **200**.
- QUnit autenticado no Chromium: **100/100** testes, **883/883** asserções, sem falhas,
  skips ou TODOs.
- Navegador real confirmou o estado guest com a orientação pessoa primeiro, os botões
  `Vincular pessoa` / `Criar pessoa`, a exceção `Número central de empresa`, o bloco de
  empresa após o vínculo pessoal e a abertura do contato de laboratório no formulário
  nativo correto de `res.partner`.
- O mesmo fluxo guest foi inspecionado em viewport móvel `390 × 844`, sem erro de
  console e sem realizar mutações em contatos reais.
- Produção não foi acessada nem alterada. Evidência operacional em
  `/home/lucaszotelli/infra-ai-ops/scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T072204332691Z`.

O primeiro ensaio `20260901T070532330656Z` foi interrompido corretamente antes do
upgrade por um fixture CRM legado que criava `partner_id` sem `partner_link_kind`. O
ambiente anterior foi restaurado com HTTP 200; o fixture foi corrigido sem afrouxar a
constraint de domínio antes do release aprovado acima.
