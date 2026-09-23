# Próximas etapas — plano de implementação

Estas integrações ainda não estão implementadas. Vamos validar cada uma contra
o painel de origem antes de acrescentá-la à visão consolidada.

## Etapa 2 — TurboDesk / GLPI (API indisponível, uso dispensado por conter as mesmas informações que o Grafana disponibiliza)

Primeiro confirmar versão do GLPI e API disponível, entidades/grupos atendidos e
se a visão será “minha fila” ou “fila da equipe”. Não assumir endpoints de um produto
chamado TurboDesk: o adaptador deve seguir o GLPI instalado e suas customizações.

Se a instalação usar REST API V1, o caminho é autenticação com user_token e App-Token
conforme configuração, abertura de sessão via initSession e uso do Session-Token
nas consultas. Gerenciar e encerrar a sessão sem expor tokens ao modelo.
Validar perfis/entidades, paginação e total de resultados. Referência:
[REST API V1 oficial](https://help.glpi-project.org/documentation/modules/configuration/general/api/api).

Tool planejada: `get_turbodesk_tickets(scope, status, idle_hours, limit)`.
Campos: ID/link, assunto, status original, prioridade, entidade, responsável/grupo,
abertura, última atualização, última interação relevante e prazos SLA.
“Tempo parado” precisa de definição: mudança administrativa em `date_mod` não
equivale a atendimento. Usar acompanhamentos/tarefas para identificar interação,
ou rotular explicitamente a aproximação “sem atualização”. Separar aguardando
cliente, aguardando fornecedor e pendente da equipe. SLA deve respeitar calendário
e pausas definidos no GLPI, não apenas diferença de horas corridas.

Aceite: total coincide com mesma fila/filtro no GLPI, todas as páginas contadas,
falha nunca vira zero e ticket fechado não entra como pendência.

## Etapa 3 — Zabbix e Grafana

Primeira ferramenta Zabbix implementada: `get_zabbix_problems`, ajustada à versão
6.2.3. Falta validar token e resultados contra o painel real. O catálogo e os
metadados dos dashboards das duas instâncias Grafana 12.1 estão implementados;
faltam consultas dos valores atuais dos painéis escolhidos.

Confirmar grupos/hosts permitidos. No Zabbix, usar token com permissões
de leitura no campo `auth` do JSON-RPC. Começar por
[`problem.get`](https://www.zabbix.com/documentation/current/en/manual/api/reference/problem/get)
para problemas, enriquecendo com hosts, triggers e itens quando necessário.
Tool implementada: `get_zabbix_problems(group_ids, host_ids, min_severity, limit)`.
Preservar severidade original, supressão/manutenção, reconhecimento, horário
e estado de recuperação. Reconhecido não significa resolvido.

No Grafana, identificar primeiro as fontes dos painéis. Se mostram o mesmo Zabbix,
consultar Zabbix e usar links dos painéis como contexto para evitar duplicação.
Para métricas de outras fontes, definir consultas específicas por datasource e
janelas limitadas; não expor uma ferramenta de consulta arbitrária.
Regras configuradas e alertas efetivamente disparados são objetos diferentes:
a API de provisionamento lista configuração, não basta como prova do estado ativo.
[APIs oficiais do Grafana](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/).

Aceite: comparar problema conhecido com painel, manter estado de manutenção e
mostrar quando o valor foi medido, além de quando foi consultado.

## Etapa 4 — ClickUp

Confirmar workspace, listas e seu ID de usuário. Tool planejada:
`get_clickup_tasks(assignee, overdue_only, limit)`.
Consultar todas as páginas necessárias, excluir concluídas/fechadas conforme o
contrato, considerar subtarefas e tarefas em várias listas conforme seu uso.
Deduplicar por ID, preservar prazo e timezone. Tarefa sem prazo não está atrasada;
definir tratamento de prazo apenas por data no fuso America/Sao_Paulo.
[Get Tasks oficial](https://developer.clickup.com/reference/gettasks).

Aceite: atrasadas coincidem com sua visualização no ClickUp, inclusive subtarefas
e limites entre dias.

## Etapa 5 — Consolidação e prioridade de trabalho

Criar `get_workflow_summary()` que consulta os adaptadores sob demanda. Falha de
uma origem deve preservar os demais resultados e declarar cobertura incompleta.
Normalizar:

```text
source, source_id, source_url, client_id, asset_id, kind, title,
original_severity, status, owner, created_at, updated_at,
last_meaningful_activity_at, due_at, fetched_at,
priority, priority_reasons, evidence, data_quality
```

Manter mapeamento explícito entre IDs de clientes/ativos das plataformas.
Mesmo hostname sozinho não prova que dois registros representam o mesmo ativo.
Agrupar candidatos por cliente+ativo+serviço+janela temporal, preservando IDs e
contagens originais. Três alertas podem representar um incidente, mas continuam
sendo três alertas nas respectivas fontes.

Proposta inicial, a calibrar com seu processo:

| Faixa | Critério de trabalho |
|---|---|
| P1 | Indisponibilidade de serviço crítico ou evidências fortes de ameaça ativa |
| P2 | Degradação relevante, SLA vencido/próximo ou alerta alto exigindo investigação |
| P3 | Ticket sem interação acima do limite acordado ou tarefa atrasada, sem impacto crítico |
| P4 | Rotina sem prazo ameaçado e alertas informativos |

Python aplica regras explicáveis; o modelo organiza a apresentação e propõe ações.
Severidade, prioridade operacional e confiança de triagem são campos diferentes.
Não permitir que uma pontuação por idade rebaixe uma indisponibilidade crítica.
Definir os limites de atraso/SLA com você, sem inventar a política da Net Turbo.

Formato de resposta desejado: horário/escopo e fontes consultadas; críticos;
fila priorizada com motivo; tickets parados; tarefas atrasadas; fontes indisponíveis;
próximos passos com evidências. Em consulta parcial, não apresentar totais globais.

## Etapa 6 — Apoio a triagem de segurança

Começar com recomendações revisáveis. Identificar EDR e acesso à telemetria:
Tactical/Zabbix por si só podem não fornecer árvore de processos, linha de comando,
hash/assinatura, conexões, usuário, contexto de mudança e recorrência.
O resultado deve separar fatos observados, hipóteses, evidências faltantes e
confiança, com “inconclusivo” quando apropriado. Não fechar alertas ou isolar hosts
automaticamente. Futuras ferramentas de ação serão uma etapa separada.
