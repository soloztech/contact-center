# Revisão crítica dos achados de backend — 10/09/2026

Escopo: respostas rápidas, início de conversa e consulta da identidade própria
WuzAPI. Revisão da auditoria `2026-09-10-post-implementation-audit.md` contra a
implementação da branch `16.0` e o código nativo do Odoo 16.

## Conclusões e alterações

| Achado | Parecer | Correção ou evidência |
| --- | --- | --- |
| Respostas pessoais vazam no bootstrap do Discuss | **Confirmado, prioridade alta.** A regra de leitura protege RPCs comuns, mas o Odoo lê todos os shortcodes com `sudo()` em `_init_messaging`. | A extensão substitui somente `shortcodes` por uma leitura sujeita às regras do usuário destinatário, também quando o chamador interno usa `sudo()`. O restante do envelope nativo é preservado. |
| Agentes perdem edição de respostas nativas sem vínculo | **Confirmado.** Era uma restrição incidental exclusiva de agentes, sem relação com a audiência das respostas da Central. | Removida a restrição por `create_uid` nos shortcodes sem vínculo. Vínculos pessoais e compartilhados continuam validados antes de escrever ou excluir. |
| `DTOValidationError` escapa no início de conversa | **Confirmado na fronteira de adaptadores.** Não é necessário alegar que o WuzAPI atual produz esse erro para tratar a violação contratual. | Conversão explícita para o mesmo erro amigável usado para um destinatário inválido, sem expor conteúdo técnico do provedor nem criar dados. Não se captura `ValueError` indiscriminadamente. |
| Falta cerca de empresa em respostas rápidas | **Não há defeito de implementação confirmado.** A busca já restringe cada escopo à empresa da conversa, além das regras de leitura. | Acrescentada regressão com duas empresas ativas e chamada elevada: uma resposta pessoal da outra empresa não entra no catálogo da conversa. O bootstrap do Discuss também é testado com a outra empresa fora da sessão. |
| Falta cobertura de início multiempresa, `sudo()`, LID primário e concorrência | **Lacunas de cobertura confirmadas, sem falha funcional demonstrada previamente.** | Testes exercitam autorização antes do I/O, separação de identidade entre empresas, LID primário com PN alternativo e corrida entre transações independentes. A disputa exige retry da transação obsoleta e converge para uma conversa, sem envio. |
| Falha de `/user/lid` pode interromper mensagens | **Refutado para o comportamento observado.** O código já captura erros do adaptador e preserva a saúde de mensagens quando a consulta de sessão foi bem-sucedida. | Testes cobrem 401, 404, 503, timeout e par PN/LID contraditório. A prova válida anterior não é sobrescrita por uma falha de enriquecimento. |
| Resposta 429 de `/user/lid` perde cooldown | **Confirmado.** O erro era capturado junto dos demais sem propagar seu prazo. | `retry_after_seconds` segue para o mecanismo de cooldown de saúde já existente. Nenhum campo, cron ou sistema de retry adicional foi introduzido; a sessão continua conectada. |
| Chamada com identidade divergente permanece em retry | **Comportamento intencional.** Escolher uma identidade sem prova pode misturar conversas de pessoas diferentes. | Mantido o bloqueio. A operação precisa corrigir a sessão/configuração e reprocessar o evento quando houver prova válida; não remover o latch para fazer o webhook passar. |
| Msgids portugueses e tradução incompatível do filtro de responsável | **Confirmado nos textos de interface/código.** | Padronizadas 52 mensagens e rótulos em resolução, início de conversa e respostas rápidas, além dos menus correspondentes, com traduções `pt_BR.po`. Corrigido o msgid de responsabilidade para corresponder exatamente ao código. |

A afirmação de “ampliação de privilégio sem registro” precisa considerar as
instruções explícitas do usuário que autorizaram agentes a vincular/criar empresas
e configurar números centralizados. A decisão de produto está autorizada; cabe
documentá-la e testar o escopo da conversa. Não é motivo para retirar a permissão.

A auditoria também se contradiz sobre o ensaio de upgrade: cita ausência de
validação de versão por módulo na tabela de lacunas e, nos pontos sem defeito,
declara que essa validação existe. Essa afirmação isolada não justifica alterar o
aplicador de produção sem conferir seu estado atual.

Os três motivos iniciais de resolução permanecem em português: são valores de
negócio editáveis, não traduzíveis, gravados com `noupdate` na instalação
brasileira. Trocar apenas o XML para inglês não traduziria esses nomes no Odoo.
Transformar o campo em traduzível exigiria mudar o armazenamento e migrar os
registros; esse custo não se justifica para corrigir os textos da interface.
Nenhum motivo ou nota histórica é reescrito por esta padronização.

## Validação

- `black`, `isort --check-only`, `flake8`, pylint obrigatório/opcional e compilação
  Python; catálogo validado com `oca-checks-po --disable=po-pretty-format`.
- Regressões adicionadas em `TestContactCenterQuickReplyManagement`,
  `TestContactCenterStartConversation`, `TestContactCenterStartConcurrency` e
  `TestWuzapiAdapter`. A classe de concorrência usa cursores reais independentes,
  fixture isolada e limpeza ORM; nenhuma chamada de envio ao provedor é feita.
- **156 testes Odoo aprovados** no ensaio backend isolado, evidência privada
  `scans/raw/20260910-contact-center-audit-backend-overlay-03/summary.json`.
- **187 testes Odoo aprovados** após a padronização de idioma, incluindo resolução,
  notas de atividade e teste explícito de tradução do campo, ação e erro em
  `en_US`/`pt_BR`. Evidência:
  `scans/raw/20260910-contact-center-audit-backend-overlay-04/summary.json`.
- Os ensaios usam o commit base `5bd6381` mais os arquivos desta revisão, com
  SHA-256 de cada arquivo e do tar registrados. Não representam a validação de
  um commit final integrado. Ambos preservaram o fingerprint do serviço ativo;
  não acessaram a base oficial nem enviaram mensagens ao provedor.
- Duas execuções iniciais falharam em fixtures novas de teste (cópia defensiva de
  conexão que não mantém uma primária ativa, e ausência de `active` explícito).
  Foram corrigidas sem alterar as proteções funcionais do produto. As evidências
  `overlay-01` e `overlay-02` foram preservadas.

Fonte nativa confirmada: [Odoo 16, `addons/mail/models/res_users.py`](https://github.com/odoo/odoo/blob/16.0/addons/mail/models/res_users.py).
