# Instruções para agentes — publicação oficial Odoo 16

## Branch única de publicação: `16.0`

Por instrução expressa do operador em 2026-09-15, toda publicação na base
oficial deve partir exclusivamente da **`16.0`, idêntica à `16.0` do GitHub**.

1. Integrar por **merge para `16.0`** todos os commits da entrega. Fast-forward
   é válido. Resolver conflitos e validar a árvore resultante antes de publicar.
2. Fazer push normal para o repositório GitHub correto. Comparar o SHA de
   `git rev-parse refs/heads/16.0` com a resposta atual de
   `git ls-remote origin refs/heads/16.0`. Exigir igualdade; `origin/16.0` em
   cache não comprova o estado do GitHub. Revalidar antes da promoção e
   reconciliar se outra entrega tiver avançado o remoto. Não usar force-push.
3. Publicar apenas arquivos versionados desse commit verificado. Proibido
   publicar código exclusivo de branch alternativa, commit somente local,
   alteração não commitada ou patch manual fora dessa fonte.
4. Checkout oficial deve estar na `16.0`, no mesmo SHA do GitHub, sem alterações
   nos arquivos da entrega. Releases imutáveis podem ser usadas se forem
   extraídas exatamente do commit verificado da `16.0`, com SHA e hashes
   registrados. Um snapshot não dispensa merge, push e verificação do remoto.
5. Registrar repositório, SHA local/remoto, módulos e hashes no incidente da
   entrega. Conferir o provider efetivo e `addons_path` em todos os serviços
   que usam a base oficial, inclusive principal e auxiliar quando existirem.
6. Merge/push não autoriza automaticamente deploy, restart, upgrade ou escrita
   no banco. Confirmar o escopo já autorizado na conversa e seguir o runbook
   operacional aplicável, com estado anterior, validações e backup proporcional.

Branches e worktrees temporários servem ao desenvolvimento; somente `16.0` é
fonte de publicação oficial. Não apagar nem incorporar indiscriminadamente
trabalhos de outros agentes. Preservar WIP; exportar a entrega do commit, sem
copiar o diretório de trabalho com arquivos não commitados.

Releases anteriores permanecem identificadas por seus commits. Rollback segue
procedimento e autorização próprios, com origem e hashes registrados.
