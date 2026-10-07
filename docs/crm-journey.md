# Jornada comercial e origem do lead

A conversa é do Contact Center. Um lead representa um negócio; um cliente pode ter
vários negócios e falar em várias caixas. A Jornada, no formulário do lead, separa
conversas vinculadas ao negócio de outras conversas do cliente cadastrado na mesma
empresa. Essa segunda área exige empresa e parceiro e não procura pelo telefone. A
cadeia detalhada Website aparece somente nas conversas vinculadas ao negócio; outras
conversas do cliente não expõem o snapshot privado de aquisição nesta tela.

```mermaid
flowchart TD
    V[Visita e campanha no site] --> H[Clique WhatsApp: captura imutável]
    H --> R[Código recebido ou confirmação humana]
    W[Mensagem recebida na caixa] --> I[Identidade e conversa no Contact Center]
    I --> R
    R --> E[Touchpoint Website único: aquisição original]
    I --> M[Mensagens e responsável atual]
    I --> T[Touchpoint: evidência de aquisição]
    I --> C[Vínculo explícito com negócio: contexto]
    C --> J[Jornada do lead]
    C --> D{Agente confirma período UTC}
    D -->|Não| N[Sem novo crédito comercial]
    D -->|Sim| P[Janela de negócio: início inclusivo e fim exclusivo]
    P --> Q[Convergência paginada: classificação pausada]
    T --> Q
    E --> Q
    Q --> A[Crédito Website pela mensagem dentro do período]
    A --> U[Classificador UTM existente]
    U --> L[Campanha no CRM com recibo auditável]
    M --> J
```

Na origem Website, aquisição, clique e mensagem têm datas independentes. A campanha
continua com a data de aquisição; a mensagem que prova a associação determina o negócio.
Mensagem exatamente no fim de um período pertence somente ao período que começa naquele
instante. Os jobs de associação e vínculo de negócio convergem em páginas limitadas, nas
duas ordens. Enquanto pendentes, a classificação aguarda.

A Jornada distingue código recebido de associação confirmada pela equipe, além de
sugestão sem crédito, período em revisão, origem fora do período e captura legada.
Campanha informada por UTM difere de ID resolvido no catálogo local. Ambiguidade e
ausência de catálogo são explícitas; nenhum desses dados prova o anúncio exato. Editar
ou apagar a mensagem depois do recebimento preserva a associação histórica; descartá-la
explicitamente na revisão retira o crédito desta origem.

Na ausência de uma data de aquisição comprovada, a tela identifica o horário do clique
como referência temporal e informa a procedência disponível (URL, visita ou cookie).
Datas ausentes não são confundidas com o fim em aberto de um período.

“Ver evidência da associação” abre a evidência autorizada. Dela é possível navegar ao
clique e ao visitante. O botão Jornada do visitante exige leitura nativa e administração
Contact: lista visitas/campanhas mesmo sem clique, conversas e negócios visíveis com
paginação. Toda navegação revalida as permissões atuais.

A origem Website autorizada do negócio é visível ao agente com acesso ao CRM e à
conversa, mesmo sem administração Marketing ou direito ao visitante. Catálogo e demais
origens Marketing conservam suas próprias restrições. Visitas aceitam os hosts da
binding ativa; falta de domínio/origem confiável aparece como configuração pendente, sem
afirmar que não houve visitas.

A área separada “Possíveis outros acessos” compara apenas a última observação válida de
IP no mesmo site, em até 24 horas antes/depois, ordenada por proximidade. Exibe as duas
datas; não unifica pessoas, visitas ou crédito. Uma rede pode ser compartilhada. Login e
fusão nativa de contatos preservam a cadeia apenas no mesmo site/empresa; visitantes
removidos deixam uma lacuna explícita sem inventar nova campanha.

Retenção e recusa explícita apagam as cópias privadas e revogam somente a autoridade
Website-WhatsApp, preservando outras origens e UTMs manuais. A recusa é assíncrona e
limitada à decisão identificada pelo cookie atual; decisões anteriores substituídas
seguem retenção. Expiração do recibo ou pausa da captura após a projeção preserva o
crédito histórico. Legado não recebe projeção retroativa. Leads sem empresa podem ser
contexto em várias empresas; esta autoridade Website não cria crédito nem escolhe sua
empresa Marketing. Outros fluxos existentes do Base podem já ter fixado essa empresa.

Vínculos antigos ficam **legado: revisar período**, com escritor indeterminado. Novas
associações humanas e de Automation ficam **contexto**. Automation não confirma período
comercial. Confirmação/revisão aposenta a linha anterior, preserva sua vigência e cria
uma sucessora com ator/data da decisão separados da origem da associação. Repetir a
mesma janela não cria outra linha.

Sobreposição com outro negócio confirmado é bloqueada sem revelar qual negócio ou suas
datas. Um responsável CRM com acesso aos negócios e à caixa deve resolver. Merge
inequívoco de duplicatas conserva a linha; períodos diferentes ou transferência de
confirmação exigem revisão. A conversão nativa preserva o ID do negócio.

A confirmação preserva a projeção Kanban. Desvincular o documento somente do caso Kanban
preserva o vínculo da conversa com o negócio. Desvincular a conversa no painel CRM
remove essa projeção e revoga as autoridades de aquisição da conversa e
`website.whatsapp` ligadas àquela geração do vínculo neste negócio. Autoridades
independentes, como formulário Website (`website.form`) e Meta Lead Ads, permanecem.
Nenhuma classificação manual é substituída.

O estado `scope_review` conserva a tupla UTM e o último recibo aplicado. Ele cobre
origens antigas sem período, merge em revisão e convergência ainda incompleta. Excluir
uma conversa preserva sua aquisição bruta: origens órfãs exigem decisão explícita do
administrador Marketing nos campos UTM ou a reversão existente, quando permitida. Não
existe promessa de reabrir uma conversa excluída.

A listagem não carrega mensagens. Conversa restrita revela apenas sua existência. Abrir
chat revalida o negócio, sua relação com a conversa e o acesso à caixa. Histórico de
atribuições/estados respeita ACL e membership de supervisão; agentes sem essas
permissões veem “histórico restrito”. A primeira data registrada indica cobertura, sem
inferir responsáveis antigos ou autores de mensagens externas.

## Próximas etapas

A caixa Comercial foi escolhida para intake futuro. Esta entrega ainda não cria lead
automaticamente ao receber WhatsApp. P2 já fornece a cadeia Website canônica; P3 cria ou
reutiliza o negócio somente nas caixas comerciais após checar negócios existentes.
Suporte e financeiro não recebem essa regra. Receita/ROAS exige P4: leitores financeiros
opcionais ainda usam relações de documentos/eventos com CRM, sem aplicar este gate de
aquisição.

## Validação e atualização

Atualizar conjuntamente Contact CRM/Kanban/Sales e Marketing Base/Contact/Google/
Website-WhatsApp/Automation instalados. Odoo inicializa novas colunas antigas como
`legacy`/`unknown`; a prova de upgrade cobre linhas ativas e aposentadas, NOT NULL e
CHECK da janela. Não rodar o backfill histórico de extração neste esquema. Seguir branch
oficial, backup e runbook de implantação; testes locais não são deploy.

O coletor `operations/ingest_readonly.py` aceita somente leitura RPC e grava um novo
arquivo sanitizado. Inclui denominadores e janela UTC. Retry de job difere de `attempts`
persistido no evento; falha terminal não comprova mensagem ou lead perdido. Ele não
reprocessa fila nem modifica política de retry/retenção.
