# Iniciar conversa — revisão crítica da implementação

Data: 2026-09-10. Referência consultada: `start_conversation_spec.md`, preservada sem
alterações. Este documento registra decisões de implementação; não comprova homologação
nativa nem publicação em produção.

## Escopo e contrato

`contact.center.ui.api.start_conversation(account_id, phone)` prepara uma conversa
direta autorizada e devolve `schema_version`, `channel_id`, `created`,
`normalized_phone` e `item`. O botão da lista abre uma janela compacta para selecionar a
caixa e informar o telefone; o envio ocorre depois, no compositor existente, com sua
outbox, autoria, políticas e auditoria.

Não criar mensagem vazia, evento inbound fictício ou envio direto ao provedor para
provocar um webhook. Abrir uma conversa não significa que o cliente entrou em contato,
que uma mensagem foi enviada ou que houve reabertura. Se a conversa já existir,
reutilizar o vínculo sem mudar estado, responsável, marcadores, mensagens ou motivo de
resolução. Nova conversa usa a criação gerenciada, participantes e atribuição automática
já previstos para a caixa.

O cenário de aproximadamente 440 contatos de feira e o script de disparo citados na
sugestão não fazem parte desta entrega. Não há campanha, importação em lote, cadência de
disparos ou autorização implícita para enviar mensagens a clientes.

## Telefone: formato tolerante, identidade conservadora

O país vem da empresa da caixa, com BR como fallback quando não configurado. Espaços,
parênteses, pontos e traços são aceitos; letras, ramais, JIDs e entradas ambíguas são
rejeitados. No contexto BR, exigir DDD e oito ou nove dígitos locais; acrescentar DDI 55
quando ausente e distinguir DDD 55 do DDI 55 pelo comprimento. Para outro país, orientar
`+DDI`; `00DDI` também explicita formato internacional. A prévia normalizada é calculada
no servidor, sem consulta ao WhatsApp.

Usar `phonenumbers` para metadados de país e E.164 segue a base técnica utilizada pelo
[Odoo 16 em phone_validation](https://github.com/odoo/odoo/blob/16.0/addons/phone_validation/tools/phone_validation.py).
A função dedicada é intencionalmente mais restrita que um formatador de contato: não
pode aceitar silenciosamente entrada inválida quando a biblioteca faltar, nem inferir um
destino diferente. A dependência é declarada no addon.

Não acrescentar/remover o nono dígito para escolher o destinatário. O formato móvel
brasileiro legado pode passar pela validação para consulta, mantendo os dígitos
originais. Somente a resposta correlacionada do provedor pode confirmar uma equivalência
restrita entre as formas móveis de oito e nove dígitos; DDDs, outros países e telefones
fixos não entram nessa equivalência.

## Prova do provedor e limite PN/LID

O adapter WuzAPI consulta `/user/check` e valida quantidade, `Query`, `IsInWhatsapp` e
JID retornados; esses campos estão no
[handler oficial fixado em 9487eca](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go).
PN confirmado pode ser identidade portátil na empresa. LID é opaco e permanece no escopo
da caixa; nunca interpretar seus dígitos como telefone. Quando a resposta é LID, o
telefone consultado é alias observado local, não prova global.

O lookup opcional `/user/lid/{jid}` aproveita o mapeamento PN→LID do provedor. HTTP 404
não impede preparar a conversa com PN confirmado. O
[mesmo código oficial](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go)
consulta o armazenamento de mapeamentos e retorna 404 quando não encontra LID. Isso não
garante que uma resposta futura somente em LID poderá ser associada: sem alias ou
evidência PN+LID, o sistema não deve inventar a ligação. A proposta de enriquecer ecos
existentes deve ser validada separadamente, inclusive quanto à ordem de locks; não é
tratada como garantia já entregue.

Os endpoints acima são consultas de registro/mapeamento, não envio de mensagem. Falhas
de autenticação, limitação, resposta inválida ou indisponibilidade geram erro
compreensível, sem expor tokens. Outros adapters precisam aderir explicitamente ao
contrato; capacidade de enviar mensagens não implica poder iniciar conversa por
telefone.

## Autorização, concorrência e atomicidade

- Antes de qualquer I/O: usuário agente, caixa ativa, empresa da sessão, ACL e escopo
  explícito da caixa; conexão primária ativa/outbound, capacidade, identidade de sessão
  e saúde válidas. Administrador/sudo não substitui escopo.
- Consulta ao provedor antes dos locks de projeção. Depois, adquirir conta → conexões →
  identidade → canal/vínculo, reutilizando os serviços existentes.
- Após o I/O, revalidar autorização, readiness e configuração, incluindo
  `health_configuration_revision`; resultado de uma configuração substituída não pode
  ser aplicado à caixa atual.
- Rejeitar o próprio número e contatos/conversas ignorados. Resolver identidade e
  aliases com as evidências recebidas, sem unir pessoas por nome ou foto.
- Savepoint engloba identidade, aliases, conversa, enriquecimento, notificação e
  serialização. Falha tardia ou conflito não deixa projeção parcial mesmo se um chamador
  capturar a exceção. Reutilizar a unicidade e o revisionamento de projeção existentes,
  sem alterar revisão de acesso para simular atividade.

Bootstrap apenas apresenta elegibilidade e país; não consulta o provedor. Adapter
ausente deve desabilitar essa ação na caixa, não derrubar toda a inbox. O tratamento
opcional de `get_adapter()` e seu teste cobrem essa regressão.

## Marcadores e filtros da lista

O cadastro `contact.center.tag` já existe. Expor seu menu em Operações para supervisores
preserva ACLs e empresa; não ampliar todo o menu de administração. Cadastrar e aplicar
continuam separados: criar alimenta o catálogo reutilizável; a seleção explícita associa
o marcador à conversa.

Responsável específico usa `responsible_id` do catálogo acessível. Selecioná-lo limpa o
atalho Minhas/Não atribuídas; selecionar um atalho limpa o ID específico. `tag_ids`
aceita vários marcadores com OR entre eles, combinado por AND com as demais facetas.
Manter `tag_id` legado no backend, rejeitando uso simultâneo ambíguo. Catálogo vazio
deve explicar como o supervisor cadastra marcadores. Paginação, total e reconciliação da
conversa selecionada devem obedecer aos mesmos filtros; não limitar a seleção aos itens
já carregados no navegador.

## Validação e pendências no momento desta revisão

Revisão estática e casos de teste locais não substituem Odoo nativo. A aprovação final
ainda depende dos resultados nativos e de QA visual desta entrega, não dos resultados de
versões anteriores. Não há declaração de produção homologada aqui.

Cobertura necessária: formatos nacionais/internacionais e inválidos; correlação PN/LID e
nono dígito; agente/empresa/caixa fora de escopo; saúde e adapter ausente; revogação ou
mudança de configuração durante consulta; duplicidade; ignorados; conflito e falha
tardia sem registros parciais; reutilização de conversa resolvida/arquivada preservando
seus campos; integração com envio existente sem realizar envio a clientes durante QA;
menu supervisor e filtros OR com paginação. No navegador: foco, Escape, clique externo,
envio duplo, respostas atrasadas, troca de caixa, gravação de áudio em andamento e
abertura sob filtros restritivos.
