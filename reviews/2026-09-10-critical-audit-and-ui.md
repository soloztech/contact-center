# Revisão crítica da auditoria e ajustes do atendimento

Revisão da [auditoria recebida](2026-09-10-post-implementation-audit.md), sobre
`5bd6381`, com implementação dos achados confirmados e dos pedidos de interface e
relacionamentos. A avaliação distingue defeito, lacuna de teste e decisão deliberada de
produto. Não há mudança na arquitetura ou no código do WuzAPI.

## Parecer sobre os achados

| Achado                                          | Avaliação e ação                                                                                                                                                                                                                                                                              |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Respostas pessoais no bootstrap do Discuss      | Confirmado. O `sudo()` nativo ignorava a regra global. A coleção de shortcodes é filtrada pelas permissões do destinatário; demais dados do bootstrap permanecem nativos.                                                                                                                     |
| Edição de shortcodes comuns por agentes         | Confirmado. Retirada a restrição incidental por autor nas respostas sem vínculo; respostas pessoais/compartilhadas da Central mantêm suas proteções.                                                                                                                                          |
| Permissão para agentes criarem empresas         | Funcionalidade expressamente autorizada pelo usuário, não escalada acidental. Mantida, documentada e testada com negação de acesso a outra conversa/empresa.                                                                                                                                  |
| Ausência de QUnit                               | Uma ausência de registro em `reviews/` não comprova ausência de execução: evidências operacionais também ficam no repositório de infraestrutura. Ainda assim, Python CI não valida Owl. A promoção desta rodada exige as suítes da fonte exata nos dois modos de assets e verificação visual. |
| Multiempresa, sudo, LID primário e concorrência | Lacunas de teste confirmadas. Acrescentadas transações concorrentes reais e matrizes de autorização; não foi necessário reestruturar o início de conversa.                                                                                                                                    |
| OR de marcadores com paginação                  | Acrescentado cenário com três conversas distintas, duas tags, interseção e paginação unitária sem duplicidade.                                                                                                                                                                                |
| Cerca de empresa nas respostas rápidas          | Já existia. Regressão adicionada; não se alterou comportamento correto.                                                                                                                                                                                                                       |
| Validação de versão por módulo no ensaio nativo | Já existia; o relatório se contradiz ao reconhecê-la em outra seção. Mantida a validação por manifest.                                                                                                                                                                                        |
| DTO inválido propagado ao operador              | Confirmado na fronteira do adaptador. Convertido `DTOValidationError` para erro operacional; não se captura todo `ValueError`.                                                                                                                                                                |
| Falha do probe complementar LID                 | Falhas transitórias já eram toleradas; cobertura adicionada. HTTP 429 perdia o cooldown: corrigido pelo mecanismo existente.                                                                                                                                                                  |
| Retry com identidade divergente                 | Intencional para impedir mistura de clientes. Documentada a recuperação operacional, mantendo o bloqueio.                                                                                                                                                                                     |
| Idioma e msgid sem tradução                     | Corrigidos os textos de código, metadados e views apontados, com traduções pt_BR e teste de idiomas. Três nomes-semente editáveis/noupdate permanecem dados da instalação brasileira; transformá-los em campos traduzíveis exigiria migração sem ganho proporcional.                          |
| Documentação, versões e peer da CI              | README e operação atualizados; versões por addon, requisito `phonenumbers`, revisão de produção para ensaio e peer compatível explícitos. O spec de início está versionado como pesquisa histórica, não como novo contrato.                                                                   |
| Fechar início de conversa durante RPC           | Confirmado. Fechar, Escape, clique fora e abrir filtros não descartam mais a operação pendente; o sucesso fecha o painel e o erro permite tentar novamente.                                                                                                                                   |
| Repetir mark_seen com contador defasado         | Confirmado. O mesmo ponteiro já confirmado não gera nova escrita. Marcar não lida e trocar a seleção invalidam a confirmação como antes.                                                                                                                                                      |
| Leitura por trás de modal/painel                | Confirmado. A verificação considera o elemento ativo nativo, o painel sobreposto e a oclusão real da mensagem visível.                                                                                                                                                                        |
| Catálogo de marcadores malformado               | Confirmado. Validação de array, IDs únicos e nomes antes de atualizar o estado; falhas aparecem no painel. Escape também funciona após o foco sair da raiz.                                                                                                                                   |
| Decisão de resolução congelada após erro        | Intencional para repetir a mesma operação quando o resultado da rede é incerto. Em revisão obsoleta o operador deve conferir a conversa e reabrir; não se troca silenciosamente a decisão ou seu UUID.                                                                                        |
| Foco preso em popovers                          | Não são modais; não se adicionou armadilha de foco. Modais nativos continuam responsáveis por sua própria gestão de foco.                                                                                                                                                                     |
| Contador inclui o filtro padrão aberto          | Comportamento consistente: existe um filtro ativo. Limpar remove todos os filtros, ampliando os estados. Não se alterou esse contrato por preferência da auditoria.                                                                                                                           |
| Collator fixo e função sem uso                  | Ordenação usa o idioma ativo; função `formatTime` sem referência removida.                                                                                                                                                                                                                    |
| Avisos de complexidade em dois métodos          | Avisos de manutenção, sem defeito funcional demonstrado. Mantidas as rotinas existentes; não se fragmentou o fluxo só para reduzir uma métrica.                                                                                                                                               |

O [parecer de backend](2026-09-10-audit-backend-verdict.md) detalha os cenários e os
ensaios isolados. A observação de que falta `phonenumbers` na estação da auditoria não
prova ausência nos servidores: o gate usa o Python real do Odoo.

## Pedidos implementados

1. Menu de mensagem posicionado dentro da área visível da timeline, inclusive para
   mensagens enviadas junto à lista lateral.
2. Mensagens agrupadas em seções de dia com identificação fixa durante a rolagem e troca
   ao entrar no próximo dia. Chaves estáveis preservam players quando páginas anteriores
   são acrescentadas.
3. Downloads com nomes curtos por tipo de mídia.
4. Vídeo em janela flutuante sem bloquear o atendimento; PiP nativo opcional quando o
   navegador oferece suporte.
5. Áudio sem legenda com duração e horário no mesmo rodapé, reduzindo espaço vazio.
   Áudio com legenda mantém espaço adequado para o texto.
6. Previews usam os registros e o extrator Open Graph de `mail.link.preview`. A fila
   existente processa até três URLs por mensagem. Transporte HTTP limita
   corpo/tempo/redirecionamentos, recusa endereços privados e fixa o IP com TLS
   validado. Conteúdo comprimido é recusado antes da leitura. Mensagens editadas ou
   excluídas e jobs repetidos são revalidados antes da gravação. Histórico solicita o
   processamento ao abrir uma mensagem com URL ainda não verificada.
7. Principal nativa mais empresas secundárias no contato, com configuração por empresa
   operadora. A opção OCA foi considerada e dispensada por oferecer um motor de relações
   mais amplo que a necessidade;
   [decisão e limites](../research/partner-company-relationships.md).
8. Desvinculação por empresa e correção separada da pessoa associada. Nenhuma ação apaga
   o contato nem reatribui documentos antigos. A principal continua sendo o padrão de
   novas cotações; secundárias não são promovidas implicitamente.

## Validação e promoção

Os testes cobrem autorização, idempotência, concorrência, traduções, metadados nativos,
URLs privadas/redirecionamentos, menus, datas, áudio, vídeo e vínculos. Os ensaios de
backend e o smoke visual são intermediários: a entrega exige CI, testes nativos e QUnit
sobre o candidato integrado, além do upgrade de laboratório antes da oficial.

Evidências operacionais privadas ficam em
`infra-ai-ops/scans/raw/20260910-contact-center-post-audit/`; screenshots e resultados
do navegador em `infra-ai-ops/output/playwright/`. O incidente no repositório de
infraestrutura registra os hashes finais, resultados e a promoção, sem alterar esta
fonte após homologá-la.
