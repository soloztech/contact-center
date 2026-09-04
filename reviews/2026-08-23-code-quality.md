# Qualidade de código — contact-center (base 1.7.0 / wuzapi 1.5.0 / ui 1.5.0)

- Data: 2026-08-23 · Revisor: Claude
- Ferramentas: as do `.pre-commit-config.yaml` do próprio repo (black 22.8, isort,
  flake8 com `.flake8`, pylint-odoo, eslint 8 com `.eslintrc.yml`, prettier 2.7 +
  plugin-xml) + métricas por AST. pylint-odoo 8 (pinado no pre-commit) não roda em
  Python 3.12; usei pylint-odoo 9 com os checks `odoolint` — resultado indicativo.

## Veredito

Código **acima da média** para addon Odoo: black limpo, zero `except: pass`, zero TODO,
nenhum `t-raw`, constraints SQL explícitas, locks `FOR UPDATE` onde importa, testes
extensos (6,6k linhas, concorrência real). Os problemas são de **forma e
manutenibilidade**, não de correção: dois arquivos-deus, funções longas que ultrapassam
o limite de complexidade que o próprio repo configurou (o pre-commit/CI ficaria vermelho
hoje), `sudo()` muito denso e lógica de fila triplicada.

## O que o pre-commit do repo acusaria (CI vermelho)

| Ferramenta                     | Resultado                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| black                          | ✅ limpo                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| flake8 (`max-complexity = 16`) | ❌ 4× C901: `_send_message` (28), `download_media` (27), `_send_message_mutation` (22), `update_conversation` (22)                                                                                                                                                                                                                                                                                                                                                  |
| isort                          | ❌ `tests/test_media_security.py` (os `__init__.py` são excluídos pelo hook OCA — falso positivo)                                                                                                                                                                                                                                                                                                                                                                   |
| prettier (xml)                 | ❌ `data/queue_job.xml`, `data/connection_health_cron.xml`                                                                                                                                                                                                                                                                                                                                                                                                          |
| eslint (`complexity 15`)       | ⚠️ 2 warnings: `loadConversations` (26), `loadTimeline` (18)                                                                                                                                                                                                                                                                                                                                                                                                        |
| pylint-odoo                    | ⚠️ 6× `translation-required` (controllers/main.py:133-145), 5× `no-raise-unlink` (decisão de design — documentar/`disable` local), 2× `inheritable-method-lambda` (account.py:467,599), 6× `manifest-superfluous-key`, 1× `no-wizard-in-models` (channel.py:863 — mover para `wizards/`), 2× `invalid-commit` em teste de concorrência (necessário — marcar com `# pylint: disable`), 1× `except-pass` (wuzapi adapter.py:1286), 3× `redefined-outer-name` (`uuid`) |

## Métricas

| Métrica                       | Valor                                                  | Comentário                                                                                |
| ----------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `application.py`              | 2 665 linhas, 2 classes de ~1 300 linhas               | `ContactCenterApplication` (domínio) e `ContactCenterUiApi` (35 métodos) no mesmo arquivo |
| Funções > 80 linhas           | 15 de 366 (2 > 150)                                    | `_send_message` 275 L / 51 ramos                                                          |
| `sudo()`                      | 135 ocorrências (54 em application.py)                 | padrão "sudo e re-checa" — correto nas auditorias, mas frágil a cada novo método          |
| `cr.execute`                  | 35                                                     | maioria `FOR UPDATE`/índices (legítimo); 21 `invalidate_*` são o sintoma                  |
| Literais de retry             | `12` em 6 lugares, `60` em 2                           | sem constante nem configuração                                                            |
| Docstring em métodos públicos | 14/116                                                 | `dto.py` bem anotado (72 hints); modelos sem hints                                        |
| JS                            | store 1 144 L, model 896 L; SCSS 3 414 L em um arquivo |                                                                                           |
| Testes                        | 6 657 L; `test_contact_center.py` com 42 testes        | um arquivo-deus de testes                                                                 |

## Pontos a tratar (ordem de valor)

1. **Quebrar `_send_message` / `_send_message_mutation` / `update_conversation` /
   `download_media`** em validação → preparação → persistência. Resolve o C901 e reduz o
   risco das próximas mudanças (M7/M8/M10 tocam exatamente aí).
2. **Separar `application.py`** em `application.py` (domínio inbound/outbound) e
   `ui_api.py` (UiDTO) — e `_serialize_*` em um módulo próprio.
3. **Mixin de fila**: `_enqueue`/`_has_active_queue_job`/`_finish_failure`/
   `_persist_safe_retry` existem em 3 variações (inbox, outbox, media) + health. Um
   `contact.center.job.mixin` com `_retry_ceiling` como atributo de classe elimina a
   duplicação e os literais `12`/`60`.
4. **Política de `sudo()`**: concentrar em poucos helpers nomeados
   (`_sudo_channel_for(user)`, `_as_system()`) com docstring do porquê; hoje cada método
   decide sozinho.
5. **Traduções nos controllers** e `# pylint: disable=no-raise-unlink` com comentário
   nos 5 `unlink` que bloqueiam por design.
6. **UI**: dividir `contact_center_app.scss` por componente; `loadConversations` e
   `loadTimeline` em funções menores (cursor/paginação/merge).
7. Higiene rápida: prettier nos 2 XML, isort no teste, `installable`/`application`
   redundantes nos manifests, `uuid` sombreado em `main.py`, wizard para `wizards/`.

## O que está bom e vale manter

- Fronteiras DTO com validação forte (`dto.py`), tokens de contexto para gates de
  `message_post`/`write`, constraints e índices parciais no banco.
- Nomes consistentes (`_contact_center_*` nos overrides de `mail.*`).
- Testes com dois cursores para concorrência, fixtures anonimizadas por evento WuzAPI,
  testes de segurança de mídia dedicados.
- Zero dívida "escondida" (sem TODO, sem `except: pass`, sem `# noqa` gratuito — só 4).
