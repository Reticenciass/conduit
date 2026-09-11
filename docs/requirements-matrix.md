# Matriz de requisitos

Estados: **implementado** = entregue e coberto por testes locais; **parcial** = contrato e
guardas entregues, integração externa ainda depende do laboratório; **pendente** = não faz parte
da instalação atual.

| Grupo | Estado | Evidência no projeto |
| --- | --- | --- |
| A — estados verdadeiros, processos e saída final | implementado | `ForwardProcessService`, PTY/multiviewer, testes de regressão |
| B — tarefas, idempotência e migrações 13→34 | implementado | `TaskRepository`, hash de conteúdo para replay seguro, transição+evento na mesma transação, migração incremental 13→34, backup antes de migration |
| B — AsyncSSH reutilizável | parcial | `AsyncSSHConnectionManager` mantém geração/capacidades e reutiliza conexões; terminais, inspeção, SFTP e forwards SSH do dashboard usam o canal compartilhado; parser seguro preserva `ProxyJump` tipado e categoriza falhas; CLI e adapters não-SSH mantêm fallback/processo |
| C — React, TypeScript, Vite e xterm.js | implementado | `frontend/`, build incluído em `ctfws/frontend/dist`; abas, rename, busca, divisão somente leitura e backend WebSocket incluído no extra web |
| C — PTY Linux, resize, controle e buffer limitado | implementado | `TerminalManager`, backpressure, fila de entrada no xterm, histórico sequenciado, cursor de retomada e compartilhamento explícito no WebSocket v2 |
| D — coleta parcial, proveniência e histórico | implementado | `RemoteInspectionService`, `collection_runs`, snapshots, saída bruta consultável na Atividade e API de coleta individual |
| D — prova de caminho com validade | implementado | `access_path_checks`, `path verify`, TTL de 5 min |
| E — arquivos atômicos, limite e hashes | implementado | `SFTPTransferService`, upload web, streaming AsyncSSH/SFTP, limite configurável (`CTFWS_MAX_FILE_BYTES`), verificação remota, política de conflito explícita e tabela `transfers` |
| E — catálogo de ferramentas e evidência vinculada | implementado | `ToolCatalogService`, tabela `tool_catalog`, hash sem execução, UI e job de transferência explícita |
| F — SSH local/remoto/dinâmico | implementado | adaptador SSH e planos revisáveis; forwards e SOCKS do dashboard usam listeners AsyncSSH, argumentos estruturados e launchers por contexto |
| F — Chisel, nc e gsocket | parcial | catálogo v2 com papéis/local de execução, validação de cliente/servidor, versão detectada e planos sem execução automática; runtime externo continua explícito |
| F — namespace + Ligolo-ng | parcial | helper com lotes apply/remove, protocolo Unix tipado, serviço systemd restrito, plano de limpeza reverso e escopo de workspace; diagnóstico/capacidades do contexto são persistidos; contrato/versão/checksum do manifesto são exigidos e o botão permanece bloqueado até a orquestração isolada do runtime ser validada |
| G — vault e bootstrap individual | implementado | `SecretVault`, `AuthManager`, API v2; `vault:` resolvido apenas em memória pelo AsyncSSH |
| G — contas locais, papéis, bootstrap, OIDC e membership | implementado no motor único | `AuthManager` entrega papéis e sessões revogáveis; memberships por workspace são administráveis por API, WebSocket/SSE revalidam sessões longas e podem ser exigidos com `CTFWS_REQUIRE_MEMBERSHIP=1`; gateway multi-motor continua fora |
| H — backup completo, restore novo, doctor, quota e retenção | implementado | `WorkspaceBackupService`, CLI, hashes, schema check e `WorkspaceMaintenanceService` com quota durante streaming |
| H — systemd e instalação Linux | implementado | `deploy/ctfws.service`, `deploy/ctfws-namespace-helper.service`, `deploy/install-linux.sh` |

O critério de produção empresarial continua condicionado ao laboratório Linux real, OpenSSH,
credenciais exclusivas, testes de navegador e contrato do adaptador roteado. Nenhum recurso
pendente é apresentado pela interface como disponível.
