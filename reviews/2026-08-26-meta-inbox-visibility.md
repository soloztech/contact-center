# Visibilidade das caixas Meta na UI

Data: 2026-08-26  
Alvo: SERVIDOR05, Odoo 16 laboratório  
Produção: não acessada

## Causa

As contas e conexões de Instagram e Messenger estavam ativas, no time do usuário e
possuíam uma conversa `open`. O bootstrap também devolvia ambas. A tela agrupada, porém,
criava cabeçalhos apenas a partir da página global de conversas carregada. Como as
conversas Meta não estavam entre as 50 atividades mais recentes, suas caixas não eram
renderizadas.

O estado amarelo `provider_paused` é independente: permanece deliberadamente enquanto o
health Graph da Fase 6.5 não está implementado e não impede o ingresso por webhook.

## Correção

- O agrupamento preserva primeiro a ordem das conversas carregadas e acrescenta todas as
  contas autorizadas ainda ausentes.
- Uma caixa sem conversa na página atual exibe contador zero e estado vazio explícito.
- O filtro por uma conta continua limitando o agrupamento à conta selecionada.
- Foi adicionado teste QUnit para uma frota autorizada inteiramente vazia e para caixas
  de Instagram/Messenger fora da página carregada.

## Validação

- `contact_center_ui 16.0.1.17.2` implantado no laboratório.
- Árvore implantada: `e9ebb60d895a5fd1d4e5b7f2af0a6e16c71d5a734caa5b22544af7364e9c4df9`.
- QUnit minificado: **76/76**, **630/630** asserções.
- QUnit `debug=assets`: **76/76**, **630/630** asserções.
- Browser autenticado encontrou `Instagram @solozindustrial` no agrupamento com zero
  conversas carregadas e registrou zero erro de console.
- Evidências do release:
  `scans/raw/20260826-odoo16-contact-center-meta-inbox-visibility/`.
