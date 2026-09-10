# Relações entre pessoa e empresas

A empresa principal continua no campo nativo `res.partner.parent_id`. Portanto,
`commercial_partner_id` e o cliente padrão de cotações continuam seguindo o Odoo. Os
vínculos secundários ficam em `contact_center_secondary_company_ids`, uma relação
many2many entre registros `res.partner`, independente da conversa. Não representam
empresas operadoras (`res.company`) nem concedem acesso a documentos.

Foi examinado o
[OCA partner_multi_relation 16.0](https://github.com/OCA/partner-contact/blob/16.0/partner_multi_relation/README.rst).
Ele oferece relações genéricas, tipos com nomes inversos, regras de parceiros e
tratamento de relações inválidas. É adequado quando existe a necessidade de um catálogo
de diferentes relações. Para principal/secundárias, um campo dedicado preserva a
semântica nativa e evita manter uma configuração de tipos desnecessária.

Em Configuração → Relacionamentos dos contatos, o administrador habilita novos vínculos
secundários por empresa operadora. Desativar preserva e permite remover os vínculos
existentes. Toda operação do agente exige conversa autorizada, pessoa esperada, registro
empresarial visível e compatibilidade da empresa operadora. Não são ampliadas permissões
gerais de Contatos.

Cada empresa tem sua ação de desvincular. A ação modifica a relação no cadastro
compartilhado, mantendo a pessoa, demais empresas e documentos; a confirmação explica
esse alcance. Ao remover a principal, nenhuma secundária é promovida automaticamente:
novas cotações voltam ao contato individual até escolher uma principal. Documentos
existentes não são reescritos.

Depois de remover os vínculos, aparece Desvincular pessoa. Para corrigir uma pessoa
selecionada por engano, Corrigir contato vinculado permite retirar só a associação da
identidade, preservando o cadastro e todas as relações empresariais. Isso evita exigir a
remoção de relações globais para corrigir apenas uma conversa.
