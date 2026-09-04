# Primeira implementação native-first — registro de release

## Estado do marco

Os dois cortes native-first foram aplicados e validados no SERVIDOR05: a fronteira
Website/CRM do Marketing Center e o conjunto `CC-SEND` + `CRM-MAP` do Contact Center.

Produção não foi acessada nem alterada. Este marco não autoriza escrita automática de
UTM, cutover de produtores de tracking nem criação automática de bindings CRM.

## CC-SEND — reenvio seguro e auditável

Versões aplicadas: `contact_center_base 16.0.1.36.0` e `contact_center_ui 16.0.1.24.0`.

O reenvio não reabre nem reaproveita a outbox terminal. Ele só fica disponível para um
`send_message` em `dead`, com delivery local `failed`, origem `agent`, sem resolução
administrativa, sem ID externo positivo e com conteúdo ainda validável. `uncertain`,
`cancelled`, evidência de aceitação externa, provider indisponível, capability ausente
ou conversa fora do escopo permanecem fechados.

Uma ação aceita cria uma nova `mail.message`, binding e outbox, vinculando a tentativa
por `retry_of`. A origem continua terminal e auditável. UUID da requisição, constraint
de filho único e revisão monotônica convergem duplo clique e concorrência sem produzir
dois disparos. Texto, mídia privada e reply são revalidados e recriados sob o provider
primário atual; upload consumido não é reutilizado.

Validação da árvore implantada:

- **413/413** testes do base;
- **204/204** testes WuzAPI;
- **762/762** testes integrados;
- **114/114** testes QUnit e **1062/1062** asserções em ambos os modos de assets.

O release atômico foi aplicado e validado com a árvore
`59e653bcde155f838228424e79e4dcd85343d712f1edbdcc746a7bab4943cd86`. Evidência:
`scans/raw/20260902-cc-send-crm-map-release-r4`. Upgrade offline, replay idempotente,
HTTP privado/público `200` e restauração byte a byte da rota foram comprovados.

No navegador autenticado, o aplicativo abriu com dados reais do laboratório; a densidade
compacta preservou a identificação da caixa e a timeline renderizou texto,
encaminhamento e mídia. QUnit minificado e `debug=assets` terminaram sem erro ou warning
de console. Screenshots: `output/playwright/20260902-cc-native-first-release`.

Nenhum reenvio externo real foi acionado, pois isso atravessaria a fronteira do
provider. A criação da tentativa filha e todos os invariantes de elegibilidade estão
cobertos pelas suítes dirigidas.

## CRM-MAP — inventário explícito de mapeamentos

Versão aplicada: `contact_center_crm 16.0.2.1.0`.

O administrador pode gerar um inventário transitório de candidatos para equipes e
pipelines. O inventário mostra evidências explicáveis de nome, roster, líder, pipeline e
etapas, inclusive fontes sem candidato, casos ambíguos, arquivados e bloqueados. Esses
sinais são somente consultivos: nenhuma pontuação, coincidência de nome ou supervisor
cria binding automaticamente.

Aceitar um candidato é uma ação administrativa explícita. O sistema relê e revalida o
snapshot sob lock da fonte antes de delegar ao modelo autoritativo de binding. O corte
possui **50 testes**, incluindo isolamento por empresa, permissões, ambiguidades,
mudança concorrente e idempotência da aceitação.

Nenhum binding real foi criado automaticamente no laboratório. O smoke não aceitou um
candidato real porque isso alteraria o mapeamento operacional; o formulário
administrativo abriu com o aviso consultivo e console limpo. A ação continua
deliberadamente humana e fail-closed.

## Marketing Center — bridge Website/CRM native-first

Versões aplicadas: `marketing_center_website 16.0.2.1.1` e
`marketing_center_website_crm 16.0.1.3.0`.

O bridge participa do MRO cooperativo do controller nativo `website_crm`, preservando os
hooks nativos de telefone, geolocalização, visitante e criação do `crm.lead`. O
Marketing Center correlaciona o sucesso do formulário com o lead nativo; não substitui o
CRM como fonte operacional.

`link_tracker` continua opcional. Quando um clique elegível já pertence ao fluxo nativo
de `link.tracker.click`, o capturador do Marketing Center o exclui do seu fato próprio,
evitando um segundo clique canônico. Essa exclusão não promete identificar todo clique
físico e não torna `link_tracker` dependência obrigatória dos addons Website.

A suíte Website passou **93/93** testes. O release completo passou **117/117** testes
base, **533/533** integrados e **4/4** da suíte. A árvore final possui hash
`c0fdc3ed8fde3f7bf13a5935581b2d82fc2029628c6d70b89e9573d8af68f36c`; evidência aplicada e
validada em `scans/raw/20260902-native-first-marketing-qunit-cleanup`.

O primeiro smoke QUnit funcional encontrou os **11/11** testes corretos, mas o
verificador de poluição do DOM registrou uma falha adicional quando o banner assíncrono
da base neutralizada apareceu dentro do runner. O harness passou a remover somente o
wrapper de `#oe_neutralize_banner`, com guarda para nunca remover `document.body`. A
versão patch foi submetida novamente ao release canônico. Resultado autenticado final:
**11/11**, **45/45** asserções e zero erro ou warning em minificado e `debug=assets`. O
painel gerencial também abriu com dados reais do laboratório e console limpo.

A tentativa inicial anterior, preservada em
`scans/raw/20260902-native-first-marketing-release`, abortou antes de alterar a base
principal por uma assertion frágil sobre metadata. O mecanismo restaurou fonte, serviço
e rota. A primeira árvore bem-sucedida está em
`scans/raw/20260902-native-first-marketing-release-r2`; o release final acima a
substitui somente pelo patch do harness QUnit.

## Iterações de release do Contact Center

As três primeiras execuções falharam fechadas nos testes, antes do upgrade da base
principal: duas no base e uma na integração. Em todas, a fonte anterior foi recuperada,
a rota foi restaurada byte a byte e o HTTP público voltou a `200`. Os testes expuseram
expectations antigas e dois fixtures CRM incompatíveis com a associação mono-time padrão
do Odoo 16. Artefatos: `20260902-cc-send-crm-map-release`, `-r2` e `-r3`.

## Gates preservados

- Nenhuma escrita UTM automática foi habilitada.
- Nenhum cutover ou desligamento automático de produtor de tracking foi autorizado.
- Nenhum binding CRM foi criado por heurística ou automaticamente.
- Produção permaneceu intocada.
- Escrita UTM exige shadow, reconciliação e go/no-go próprios.
- Binding CRM exige inventário, decisão humana e ação administrativa explícita.

## Fechamento

O marco técnico está implantado e validado no SERVIDOR05. Restam dois aceites
operacionais que deliberadamente cruzam efeitos externos: exercer um reenvio para um
destinatário de teste autorizado e aceitar um binding CRM escolhido pela pessoa
responsável. Até essas decisões, ambos permanecem fechados e nunca são disparados
automaticamente.
