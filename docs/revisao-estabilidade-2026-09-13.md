# Conduit — revisão de falhas e plano de estabilização

Data: 13 de setembro de 2026. Base inspecionada: commit `5db441a`, versão Python `0.28.0`, schema mais recente no código `34`. Rodada de correção local: versão `0.28.4`, schema `35`.

Este documento registra o diagnóstico, as correções aplicadas nesta rodada e o trabalho restante. A revisão usou leitura do código, testes existentes e reproduções isoladas; não executou comandos em alvos operacionais nem modificou o workspace operacional da Kali.

Status desta rodada: **implementado localmente** para o hotfix do terminal, ciclo de vida/filas, cancelamento durável, worker contextual, helper, SSE e estado SFTP. A validação de instalação no Linux, namespaces, Ligolo real e ownership systemd continua pendente até uma execução na Kali ou em laboratório reproduzível.

## 1. Resultado principal

O erro `terminal_operation_failed: O tamanho do terminal precisa estar entre 1 e 500` é reproduzível no frontend distribuído. Não depende de credencial SSH incorreta nem de um comando digitado pelo operador.

O painel do terminal não tem uma altura efetivamente limitada. O `ResizeObserver` observa o mesmo contêiner cujo tamanho muda em consequência de `fit.fit()`. Padding, bordas e dimensionamento automático formam um ciclo: o terminal aumenta, o contêiner aumenta, o observador mede novamente, o terminal aumenta novamente.

O frontend transmite essas dimensões ao motor sem limite ou deduplicação. Quando as linhas ultrapassam 500, o motor rejeita a operação. O aviso é escrito dentro do terminal, misturando uma falha da interface com a saída do processo.

### Evidência reproduzida

Foi carregado o bundle atual de `ctfws/frontend/dist` em Edge headless, com viewport de 1440 × 900 e uma API/WebSocket de teste em memória. O processo remoto foi substituído pelo transporte de teste; HTML, CSS, React e xterm vieram do frontend real.

| Medida | Reprodução antes da correção |
| --- | --- |
| Navegador | Edge 153.0.4234.32 |
| Primeiras dimensões enviadas | 96 × 35, 96 × 36, 96 × 37 |
| Últimas dimensões medidas | 96 × 569, 96 × 570, 96 × 571 |
| Pedidos de resize | 537 em aproximadamente 3,78 segundos |
| Altura final do contêiner | 8.583 px |
| Altura da página | 9.065 px |
| Ação do operador durante o crescimento | Nenhuma |
| Backend real recebendo 96 × 571 em teste isolado | Retornou exatamente o código e a mensagem relatados |

Após a correção, o mesmo probe sobre o bundle compilado enviou **1 resize válido (96 × 32)**, manteve o host com **490 px** de altura e a página com **972 px**, sem erros de navegador. Ao desconectar apenas a visualização, a interface mostrou “Visualização desconectada” e reconectou com `after_sequence=0`, reconstruindo o histórico retido.

O número de frames e o tempo variam por máquina. O critério de reprodução é o crescimento contínuo sem intervenção e a ultrapassagem do limite.

Referências no código: `frontend/src/main.tsx:986`, `frontend/src/main.tsx:1043`, `frontend/src/styles.css:1`, `ctfws/services/terminals.py:362` e `ctfws/web.py:2050`.

Há uma distinção importante entre corrigir o ciclo e apenas limitar a dimensão enviada: limitar somente no servidor pode deixar navegador e PTY com geometrias diferentes. A correção precisa estabilizar o layout e aplicar o mesmo tamanho válido nas duas pontas.

## 2. Alcance e limites da revisão

- A Kali em `192.168.1.139:22` não respondeu à tentativa SSH com timeout de seis segundos. Portanto, não foi possível verificar seus logs recentes, binários instalados, permissões efetivas ou estado atual dos terminais nesta revisão.
- Os nove testes existentes selecionados por `terminal or read_only` passaram: `9 passed, 33 deselected`. Eles não cobrem o ciclo de layout que reproduziu o defeito.
- As reproduções adicionais do backend usaram banco temporário e processos simulados, sem abrir shells reais. Demonstram as decisões do código nas condições descritas; não substituem os testes Linux de PTY e transporte real.
- Os problemas de helper, permissões e Ligolo abaixo foram encontrados na inspeção do código e dos arquivos de instalação. Seus efeitos em systemd, rede e processos precisam dos testes Linux especificados neste plano.
- O teste Ligolo relatado na entrega anterior mostrou preparação do transporte e tentativa de `curl` com timeout. Isso não comprova acesso HTTP bem-sucedido, DNS contextual, isolamento entre redes sobrepostas ou recuperação de falhas.
- O objetivo de aceite é eliminar os defeitos reproduzidos e impedir sua regressão nos cenários definidos. Uma suíte passando não permite prometer ausência de qualquer erro futuro.

Scripts locais usados na revisão, fora do produto e ignorados pelo Git:

```powershell
python ._validation/review-20260913/probe_frontend.py
python ._validation/review-20260913/probe_backend.py
python -m pytest -q tests/test_guided_workspace.py -k 'terminal or read_only'
```

O probe de frontend foi convertido em `tests/test_frontend_terminal.py`; os scripts locais continuam apenas como ferramentas auxiliares de diagnóstico e não são requisito da suíte.

## 3. Achados priorizados

Classificação: **reproduzido** significa observado nos experimentos desta revisão; **código** significa comportamento identificado por leitura, ainda sem demonstração completa na instalação. P0 bloqueia a publicação da área afetada; P1 compromete uso normal, integridade ou recuperação; P2 prejudica usabilidade ou operação prolongada.

| ID | Prioridade | Achado e consequência | Evidência |
| --- | --- | --- | --- |
| R01 | P0 | Resize cresce continuamente, ultrapassa 500 e gera uma sequência de erros. | Reproduzido; `main.tsx:986`, `styles.css:1`. |
| R02 | P1 | Entrada usa frames de texto e o servidor interpreta texto iniciado por `{` como controle. Uma colagem `{"hello":"Conduit"}` não chegou ao processo; o mesmo conteúdo em binário chegou integralmente. | Reproduzido; `main.tsx:961`, `web.py:2035`. |
| R03 | P1 | O cursor avança ao receber os metadados, antes de o xterm consumir os bytes. Desmontar a visualização cria um terminal vazio, mas reconecta usando o cursor anterior, omitindo o histórico já descartado do navegador. | Código e URLs reproduzidas: `after_sequence=0` → `after_sequence=123`; `main.tsx:915`, `main.tsx:1003`. |
| R04 | P1 | `get()` e `list()` chamam reconciliação que remove runtimes. No teste, uma consulta removeu o runtime com EOF ainda falso; `is_drained()` em seguida lançou `EntityNotFoundError`. | Reproduzido; `terminals.py:92`, `terminals.py:461`; caminho do WebSocket em `web.py:2113`. |
| R05 | P1 | Sem visualizadores, encher a fila padrão bloqueia o produtor; com visualizadores, um observador lento pode bloquear os demais, pois a distribuição espera cada fila sequencialmente. | Bloqueio sem visualizadores reproduzido; distribuição serial identificada no código; `terminals.py:503`, `terminals.py:571`, `terminals.py:619`. |
| R06 | P1 | Não há confirmação de consumo da saída pelo navegador. Filas limitadas no Python não limitam, por si só, a fila de escrita do xterm. | Código; `main.tsx:1031`, `web.py:2101`. |
| R07 | P1 | Desconexão usa mensagem genérica e não inicia recuperação. O frontend pode acumular entrada depois de perder a conexão ou sem ter controle; não há ações visuais completas de controle. | Código; `main.tsx:973`, `main.tsx:1033`. |
| R08 | P2 | Desconectar apenas a visualização mostra “Sessão não está ativa”. | Reproduzido; `main.tsx:907`. |
| R09 | P1 | Encerramento pode remover o runtime e fechar o descritor antes da drenagem final. Timeout ao fechar SSH é suprimido. O PTY local usa `openpty` e nova sessão, mas não configura explicitamente terminal controlador. | Código; `terminals.py:141`, `terminals.py:393`, `terminals.py:608`. |
| R10 | P1 | Cancelar uma tarefa retorna `cancelled` imediatamente mesmo com trabalho em andamento. O caminho síncrono pode atravessar a espera por capacidade sem revalidar o cancelamento. | Cancelamento prematuro reproduzido; `tasks.py:147`, `tasks.py:171`. |
| R11 | P1 | O cliente do helper espera 5 s, enquanto uma ferramenta aceita até 3.600 s. O servidor atende um cliente por vez, prendendo consultas e limpeza durante uma execução longa. | Código; `routed_socket.py:31`, `routed_socket.py:258`, `routed_helper.py:420`. |
| R12 | P1 | Resposta do helper limitada a 64 KiB versus saída do worker de até 4 MiB. `communicate()` acumula antes de truncar; o limite de resposta não constitui limite de memória. | Código; `routed_socket.py:29`, `routed_socket.py:307`, `routed_worker.py`, `routed_helper.py:420`. |
| R13 | P1 | Parar Ligolo remove a sessão do registro antes de confirmar a limpeza. Uma falha pode perder os handles para nova tentativa; o fallback pode marcar limpeza concluída sem confirmar o lado remoto. | Código; `contexts.py:133`, `contexts.py:788`. |
| R14 | P1 | O início do Ligolo trata `Exception`, mas não o cancelamento assíncrono; há esperas de encerramento sem prazo. O agente usa caminho temporário previsível e não é relido para comprovar hash antes da execução. | Código; `ligolo_runtime.py:199`, `ligolo_runtime.py:261`, `ligolo_runtime.py:301`, `ligolo_runtime.py:361`. |
| R15 | P1 | Conexões anunciam capacidades fixas; não há monitor contínuo de perda do transporte SSH registrado neste gerenciador. `dns-context` é anunciado, mas o worker só entra no namespace de rede e não configura resolvedor isolado. | Código; `ssh.py:59`, `ssh.py:95`, `contexts.py:327`, `routed_worker.py`. |
| R16 | P0 | O instalador atribui `/opt/ctfws`, venv e configurações ao usuário `ctfws`; o serviço privilegiado executa código desse venv. A propriedade dos artefatos não estabelece a separação de confiança anunciada entre motor e helper. | Código; `deploy/install-linux.sh:17`, `:149`, `:206`; `deploy/ctfws-namespace-helper.service:11`. Validar permissões efetivas no Linux antes de reabilitar esta área como confiável. |
| R17 | P1 | O frontend mantém resultados SFTP antigos ao mudar a conexão. Uma ação pode usar o caminho da máquina anterior com o perfil novo. Também oferece “Baixar” para qualquer entrada, inclusive diretórios. | Código; `main.tsx:862`, `main.tsx:869`, `main.tsx:890`. |
| R18 | P2 | SSE não reconecta nem reapresenta `Last-Event-ID`. Cada evento dispara várias listagens completas em paralelo; falha de um painel impede atualizar todos. | Código; `main.tsx:74`, `main.tsx:145`. |
| R19 | P1 | Iniciar/parar contexto e transferir via botões principais ainda aguardam a operação inteira no HTTP. A confirmação é um booleano, sem revisão imutável e expiração vinculadas à execução. | Código; `web.py:1212`, `web.py:1524`; falta de contratos de planos no backend atual. |

Outros pontos a tratar junto aos achados: nome de namespace baseado apenas em IDs numéricos; escolha de executável contextual validada pelo basename; ausência de diagnóstico correlacionado nas mensagens WebSocket; retornos de criação de terminal com campos de disponibilidade diferentes das consultas posteriores.

Estado após esta rodada: R01 e R08 corrigidos no bundle e cobertos por smoke test de navegador; R02, R03, R04, R05, R06 e R09 corrigidos na fronteira WebSocket/motor e cobertos pelos testes de terminal; R10, R11 e R12 corrigidos com cancelamento durável, timeout por operação e captura limitada; R16 corrigido no instalador para manter código e manifesto root-owned; R17 e R18 corrigidos no frontend/SFTP. R07, R13–R15 e R19 ainda exigem as entregas de reconexão completa, Ligolo isolado, planos imutáveis e validação Linux descritas abaixo.

## 4. Ordem de execução

1. **E0 — Baseline e regressões:** registrar as reproduções e corrigir a matriz de requisitos.
2. **E1 — Hotfix do resize:** resolver R01 e R08, sem alteração de schema.
3. **E2 — Fronteira motor/helper:** resolver R16 antes da próxima publicação com helper habilitado. Pode ser desenvolvido paralelamente a E1.
4. **E3 — Terminal confiável:** protocolo, controle, histórico, EOF, reconexão e PTY; R02–R09.
5. **E4 — Tarefas e execução contextual:** cancelamento real, protocolo de jobs do worker, memória e resultados; R10–R12 e parte de R19.
6. **E5 — Ligolo e capacidades verdadeiras:** retomada, dependências, isolamento, preparação e limpeza; R13–R15.
7. **E6 — Operação guiada e arquivos:** reduzir ações ambíguas, corrigir contexto selecionado, SSE e revisões de planos; R17–R19.
8. **E7 — Publicação verificável:** laboratório, navegador, wheel, migrações, instalação e teste prolongado.

A correção pequena do terminal pode ser publicada assim que seu aceite passar. Não deve receber o rótulo de conclusão das demais entregas. Mapa avançado, novas ferramentas e rotinas visuais ficam depois dos bloqueadores de estabilidade; não substituem os reparos encontrados.

## 5. E0 — Baseline e regressões

### Trabalho

- Versionar a reprodução do resize usando o frontend real com API controlada e registrar uma variante com motor real.
- Transformar as reproduções de JSON descartado, remoção prematura do runtime, fila sem observadores e cancelamento prematuro em testes que falhem antes da correção.
- Adicionar Playwright ao desenvolvimento, com dependências travadas no lock e comandos `test:e2e` e `test:ui`. Instalar o navegador no CI de maneira explícita.
- Separar testes de layout/protocolo com API simulada dos testes com OpenSSH real. Ambos são necessários, por razões diferentes.
- Rever `docs/requirements-matrix.md`: usar as colunas requisito, implementado, validado em laboratório, validado na instalação, evidência e limitações. Reabrir estados de terminal, cancelamento e Ligolo afetados por esta revisão.
- Registrar versão do pacote, revisão Git e hash do frontend nos relatórios de teste. Não usar a contagem histórica de 129 testes como prova dos fluxos novos.

### Aceite

As reproduções falham pelo comportamento esperado na base atual, ficam verdes após seus reparos e mantêm identificador estável. Nenhum banco ou alvo do usuário participa do CI.

## 6. E1 — Hotfix do resize e da visualização

### Layout

1. Criar uma área de terminal com altura derivada do espaço disponível da tela, sem depender da altura produzida pelo próprio xterm.
2. Usar `min-height: 0` e regras de overflow nos itens flex/grid relevantes. Em tela pequena, permitir rolagem da página sem deixar o xterm dimensionar indefinidamente o painel.
3. Colocar padding e borda no wrapper externo. O elemento medido pelo FitAddon deve representar somente a área útil do terminal.
4. Usar a mesma estrutura para visualização simples e divisão, sem reconstruir a árvore inteira do terminal ao alternar o layout.

### Cálculo e protocolo

1. Medir por `proposeDimensions()` e validar inteiros finitos; ignorar medições quando o contêiner estiver desconectado, oculto ou sem área útil.
2. O contrato inicial do servidor permanece 1–500 linhas e colunas. O frontend respeita também o mínimo de duas colunas do xterm. O motor anuncia limites no `ready`.
3. Aplicar a dimensão efetiva no xterm e no PTY. Não corrigir somente os argumentos enviados ao backend.
4. Agendar no máximo um cálculo por frame e transmitir apenas quando as dimensões efetivas mudarem. Esperar `ready` e controle concedido antes do envio.
5. Refazer a medida após carregamento de fontes, troca de layout e retorno de aba visível. Suspender o observador durante desmontagem e cancelar callbacks pendentes.
6. Retornar `terminal_resize_invalid` com limites e diagnóstico, mantendo a sessão utilizável. Não inserir repetidamente o erro no fluxo ANSI; mostrar aviso discreto no painel com detalhes recolhidos.
7. Separar os estados “visualização desconectada”, “reconectando visualização”, “processo encerrado” e “sessão perdida após reinício”.

### Aceite

- Abrir o frontend e deixá-lo parado por 60 segundos: dimensão e altura permanecem estáveis; após estabilização não há novos frames de resize sem mudança de geometria.
- Testar viewport 320 × 568, 768 × 1024, 1440 × 900 e 3840 × 2160; zoom entre 50% e 200%, sidebar, abas, divisão e mudança de fonte.
- Todo resize enviado pela interface contém dimensão válida e diferente da última confirmada/enviada.
- Injetar manualmente 0, -1, 501, `null`, texto, booleano e dimensão fracionária: erro controlado, sem fechar a conexão nem alterar a última dimensão válida.
- Desconectar a visualização mantém o processo e apresenta o estado correto. Reconectar não abre outro processo.

Arquivos principais: `frontend/src/main.tsx`, `frontend/src/styles.css`, `ctfws/web.py`, `ctfws/services/terminals.py`. Extrair `TerminalViewport` e o hook de resize para arquivos próprios torna os cenários testáveis sem reescrever o restante do dashboard.

## 7. E2 — Separação efetiva entre motor e helper

### Instalação e execução

- Tornar root-owned e não graváveis por `ctfws` todos os executáveis, módulos Python, venv, releases, manifesto de compatibilidade e configuração privilegiada.
- Manter `ctfws` proprietário somente dos dados operacionais necessários. Auditar cada diretório pai e links simbólicos; uma folha root-owned dentro de diretório substituível não é proteção suficiente.
- Executar o helper com Python em modo isolado, caminho absoluto e diretório de trabalho confiável. Não importar módulos do workspace operacional nem de `PYTHONPATH` controlável pelo motor.
- Fazer o helper aceitar identificadores de ferramentas e resolver caminhos absolutos a partir de um catálogo confiável. Verificar somente o último componente do caminho não garante que seja o binário autorizado.
- Atribuir ao helper um registro próprio de recursos criados. Validar identidade de workspace, contexto e execução antes de alterar ou encerrar qualquer recurso.
- Validar identidade do par no socket Unix e permissões por UID/GID; limitar requisições e concorrência.
- Proteger abertura de configurações e logs contra troca por symlinks entre validação e uso. Criar arquivos relativamente a diretórios já abertos, com verificação de tipo e exclusividade.
- Registrar quais capacidades são necessárias em cada processo e testar a redução de UID/GID/capacidades antes de executar ferramentas.

### Aceite

Em instalação limpa e em upgrade, `ctfws` não consegue alterar os artefatos executados pelo helper. Requisições com recurso de outro workspace, binário fora do catálogo, caminho trocado ou identidade de processo incompatível são recusadas. Os testes comprovam as permissões sem executar uma elevação de privilégios.

## 8. E3 — Terminal, protocolo e ciclo de vida

### Entrada e controle

- Negociar explicitamente uma versão de protocolo. Manter v1 como adaptador de compatibilidade; o novo cliente não utiliza heurística de texto iniciado por JSON.
- Enviar toda entrada do xterm em binário UTF-8; JSON tipado fica reservado a controle. Colagens literais com `action`, `data`, chaves, aspas e quebras de linha chegam intactas.
- Ter um único controlador por terminal. Mostrar identidade ou nome reconhecível do controlador e botões “Solicitar controle” e “Liberar controle”. Tomada administrativa exige autorização do recurso e deixa evento de auditoria.
- Autorizar entrada e resize no servidor a cada ação, inclusive após revogação ou mudança de papel. Não confiar apenas no booleano mantido pelo cliente.
- Limitar a fila inicial de entrada a 128 KiB medidos em bytes. Esvaziá-la somente após controle concedido. Após desconexão estabelecida, bloquear entrada e não reenviar teclas com entrega incerta.
- Ao receber `ready` ou mudança de controle, atualizar `disableStdin`, foco e dimensão. O modo observador não transmite resize.

### Saída e confirmação de consumo

- Identificar cada fluxo por terminal e geração; usar sequência monotônica e tamanho em bytes. Não reaproveitar cursor de outro terminal ou geração.
- Avançar o cursor confirmado e enviar ACK somente no callback de consumo de `terminal.write(bytes, callback)`. Esse callback confirma processamento pelo xterm; não deve ser confundido com comprovação de pintura de pixels na tela.
- Registrar limite alto/baixo de dados não confirmados, inicialmente 256 KiB/64 KiB por visualização, configurável e medido em testes.
- O ACK só pode confirmar dados realmente enviados àquela visualização e geração. ACK adiantado, duplicado e atrasado não liberam créditos indevidos.
- Manter histórico único limitado a 4 MiB por terminal, com cursores de assinantes. Limitar também metadados, filas de transporte e memória do navegador.
- Continuar lendo o processo sem visualizadores e girar o histórico limitado; informar a lacuna ao reassociar. Evitar a fila padrão sem consumidor que bloqueia atualmente.
- Um observador atrasado recebe aviso de lacuna ou é desconectado com `viewer_too_slow`, conforme política; não bloqueia outros observadores. O controlador tem controle de fluxo sem impedir o canal de entrada/Ctrl+C.
- Tratar Unicode e sequências ANSI cortadas por chunks. Na reconstrução após descarte, usar reset/replay consistente ou snapshot de tela em memória, sem repetir comandos do shell.

### Persistência da visualização e reconexão

- Manter instâncias xterm por terminal ao alternar abas ou dividir o painel. Dispor instâncias quando o usuário fechar a visualização ou exceder um limite explícito de recursos.
- Quando a instância for nova, reconstruir a saída disponível desde o início retido ou um snapshot antes de usar cursor incremental.
- Implementar estados `connecting`, `attached`, `reconnecting`, `detached`, `exited`, `session_lost` e `auth_required` para a visualização; o estado do processo continua separado.
- Reconectar somente a visualização de runtime existente, em tentativas de 1, 2, 4 e 8 segundos, com janela total de 30 segundos. Ao esgotar, apresentar tentativa manual com causa.
- `4401` abre autenticação; `4403` explica falta de permissão; `4409` oferece novo terminal e, se necessário, formulário de credencial temporária. Fechamento normal não mostra aviso alarmista.

### EOF, processos e limpeza

- Remover efeitos destrutivos de `GET/list`. A transição de runtime será responsabilidade do gerenciador operacional, não da leitura de estado.
- Separar processo em execução, EOF lido, código de saída conhecido, bytes entregues e bytes confirmados por observador.
- Publicar `exit` individualmente após a última sequência daquela visualização ser consumida. Um observador lento não adia indefinidamente o encerramento apresentado aos demais.
- Reter histórico volátil por período configurável, inicialmente cinco minutos após saída, sujeito ao limite total. Não ativar transcrições em disco.
- Encerrar com verificação de identidade e prazo; aguardar confirmação e drenagem. Se houver permissão negada ou timeout, mostrar encerramento não confirmado e permitir diagnóstico.
- Implementar auxiliar Linux para `setsid`, associação ao terminal controlador, grupo de processos, execução e resize. Evitar `preexec_fn` em processo multithread.
- Gerenciar shutdown do motor também para PTYs locais, hoje ausentes da limpeza explícita do lifespan; definir e testar a política de filhos e grupos.

### Aceite

Dois canais SSH independentes compartilham uma conexão. JSON, UTF-8, Ctrl+C, Tab, setas, colagem multilinha e aplicativo de tela inteira funcionam. Testar 100 trocas de abas, refresh, desconexão breve, duas visualizações e saída final de processo rápido. Rodar saída intensa seguida de Ctrl+C e medir retorno em até um segundo no laboratório local de referência.

## 9. E4 — Tarefas e protocolo do worker

### Cancelamento verdadeiro

- Persistir `cancel_requested_at` separado do resultado final. O botão muda para “Cancelamento solicitado”, depois “Limpando recursos” e finalmente “Cancelado” ou “Limpeza pendente”.
- Revalidar cancelamento depois de obter vaga no limite global de quatro tarefas, antes de iniciar qualquer etapa e entre blocos de transferência.
- Dar à operação um token de cancelamento cooperativo e registro dos recursos de sua tentativa. Cancelar somente os recursos pertencentes à tentativa ou referências que ela possua.
- Não declarar cancelamento concluído com base apenas em `Future.cancel()` ou `Task.cancel()`. Threads executando continuam até cooperar ou até o processo gerenciado encerrar.
- Tratar `CancelledError` explicitamente em operações assíncronas; concluir limpeza limitada por prazo antes de propagar o cancelamento.
- Persistir transição e evento na mesma transação. Reinício interrompe tentativas incompletas, sem repetição automática.

### Protocolo de execução

- Separar pedidos rápidos de controle de jobs longos. `worker-run` deve retornar um identificador rapidamente; progresso, cancelamento e saída usam consultas/stream próprios.
- O helper privilegiado prepara o contexto e lança um worker conhecido. Ferramentas executam sem privilégio, fora do loop de atendimento administrativo.
- Aplicar concorrência limitada, por contexto e global. Consultar saúde e parar outro contexto não pode esperar uma ferramenta terminar.
- Manter envelopes pequenos, inicialmente até 64 KiB, e transferir stdout/stderr em blocos, não dentro de um único JSON de até 4 MiB.
- Trocar `communicate()` ilimitado por leitura incremental com limite de memória. Informar bytes descartados e `truncated=true`, preservando código de saída e motivo de término.
- Definir prazos separados de conexão ao socket, confirmação do pedido, execução, ausência de progresso e limpeza. Um timeout do cliente não é confirmação de encerramento remoto.

### Aceite

Uma ferramenta de 10 e outra de 60 segundos completam sem bater no timeout administrativo de 5 s. Saídas de 63, 65 e 1.024 KiB funcionam com fragmentação ou truncamento explicitamente informado. Uma consulta de saúde e um cancelamento continuam responsivos durante a execução. Cancelar tarefa enfileirada não permite que ela inicie depois.

## 10. E5 — Ligolo, isolamento e recuperação

- Persistir o manifesto de recursos por etapa, assim que cada recurso for criado. Incluir identidade estável de workspace, contexto, tentativa, conexão, arquivo remoto, proxy e agente.
- Usar UUID persistente para identidade de workspace; IDs iguais em bancos distintos não podem produzir o mesmo namespace ou diretório remoto.
- Preparar agente em diretório temporário exclusivo e restrito; verificar arquitetura, tamanho e hash por releitura SFTP antes de executar. Não substituir um arquivo preexistente fora da tentativa.
- Não remover a sessão de `_routed_sessions` antes de confirmar o stop. Manter handles e pendências necessários para repetir apenas a limpeza faltante.
- Verificar resultado da remoção remota e estado do processo; `check=False` seguido de ausência de exceção não comprova que o arquivo foi removido.
- Colocar prazo em encerramento de agente, listener, proxy e namespace. Persistir limpeza não confirmada e mostrar qual recurso falta.
- Monitorar conexão SSH e proxy depois do início. Queda de salto ou processo invalida imediatamente capacidades e provas dependentes.
- Provar SFTP e tipos de encaminhamento antes de anunciar capacidade. Senha temporária ausente deve abrir a etapa de autenticação e depois continuar a ação pendente.
- Implementar DNS do contexto com resolvedor configurado e isolamento compatível. Somente `setns` de rede não troca `/etc/resolv.conf`; não anunciar `dns-context` antes do teste correspondente.
- Revisar o enlace de controle: o veth atual cria endereços e rotas conectadas no host. Para cumprir o contrato de não alterar rotas globais, usar worker e ponte por socket Unix. Se o enlace temporário for mantido durante transição, identificá-lo, verificar conflitos e provar sua limpeza; não declarar ausência absoluta de alterações no host.
- Manter Ligolo 0.9.1 fixado enquanto seu contrato for o suportado. Verificar artefato, checksum e API; valor de variável de ambiente não é prova de compatibilidade.

### Prova de acesso

“Transporte pronto” significa proxy, agente, TUN e rotas preparados. “Destino verificado” exige resposta de um serviço de teste conhecido, com contexto, protocolo, endereço, porta e horário. Guardar TTL de cinco minutos e invalidar ao perder dependência. Um timeout de `curl` é falha daquela prova, ainda que a preparação tenha sido bem-sucedida.

### Aceite

Laboratório com dois contextos contendo o mesmo CIDR e servidores que devolvem identificadores diferentes. Cada acesso deve receber o identificador esperado. Testar DNS contextual, encaminhamento remoto proibido, arquivo remoto preexistente, cancelamento em cada etapa, queda do salto e stop com permissão negada. Parar um contexto preserva o outro. Rotas e DNS globais têm comparação antes/depois conforme o desenho escolhido.

## 11. E6 — Botões, arquivos, atividade e planos

### Arquivos

- Vincular cada listagem à conexão, geração e diretório usados na consulta. Ao trocar a conexão, limpar seleção/listagem antiga e cancelar ou ignorar respostas atrasadas.
- Para diretórios: abrir ao clicar e mostrar breadcrumbs. Para arquivos regulares: habilitar download. Links e tipos especiais têm tratamento explícito.
- Capturar origem e destino ao iniciar uma transferência; alterações posteriores no seletor não mudam uma operação já criada.
- Usar jobs assíncronos para transferências dos botões principais. Mostrar tamanho, bytes, progresso, cancelamento, política de conflito e pendências.
- Separar “hash calculado” de “integridade verificada”. Atualizar listagens após conclusão sem apagar erros relevantes.
- Tratar dupla submissão e arrastar arquivos durante upload em andamento. Falha parcial deve atualizar a lista dos arquivos já concluídos.

### Estado e diagnósticos

- Criar cliente de API único que preserve `code`, mensagem, recurso, etapa, `retryable`, `request_id` e `diagnostic_id`.
- Mensagens do Conduit ficam em status do painel, com “Ver detalhes” e “Copiar diagnóstico”. A saída do processo continua identificável.
- Retornar disponibilidade operacional de forma consistente em criação, listagem e consulta de terminais.
- Indicar saúde do motor, conexão com eventos e transporte remoto separadamente. Não exibir “Motor local ativo” como texto fixo independentemente da consulta.
- Reconectar SSE com cursor; tratar linhas CRLF e frames divididos entre chunks. Se houver lacuna de eventos, reconciliar o estado completo uma vez.
- Agrupar eventos próximos e atualizar apenas recursos afetados. Usar uma atualização em andamento por recurso, descartando respostas antigas.
- Listar tarefas e coletas por resumo paginado; carregar stdout/stderr sob demanda. Cada conclusão oferece “Ver resultado”.

### Revisão e idempotência

- `POST /operation-plans` produz plano com revisão, resumo legível, recursos, dependências e expiração inicial de cinco minutos.
- Executar somente a revisão exibida; mudança de conexão, rede, destino ou versão invalida a revisão e retorna conflito acionável.
- Operações demoradas retornam `202`, `job_id` e recurso. O frontend acompanha resultado e limpeza por eventos.
- Chaves de idempotência vinculadas a workspace, ator, operação e conteúdo. Duplo clique retorna a mesma tentativa; alteração de conteúdo com a mesma chave retorna `409`.
- Mostrar onde cada ação executa: remoto, motor ou contexto nomeado. Desabilitar ações indisponíveis com requisito específico.

### Aceite

Trocar de A para B enquanto a listagem SFTP de A demora nunca mostra arquivos de A como arquivos de B. Diretório não é enviado a download. Duplo clique não cria duas transferências nem dois contextos. A perda de SSE recupera os estados sem repetir uma operação. A mensagem de erro permanece ligada à operação correta.

## 12. Dados, migração e compatibilidade

- E1 não exige migração de banco. Alterações de frontend e backend devem ser distribuídas juntas em wheel com hash de assets.
- A evolução persistente seguinte parte do schema 34 e só recebe novo número após implementar e testar sua migração; não atualizar versão preventivamente.
- Acrescentar, quando exigido pela entrega, motivo de encerramento e diagnóstico de terminal; solicitação de cancelamento e resultado de limpeza; UUID de workspace; tentativas/recursos operacionais; planos e revisões.
- Cursores de visualização, filas, descritores e controladores em memória não são restaurados como recursos ativos. Evidências existentes e IDs de registros permanecem preservados.
- Fazer backup consistente antes da migração e registrar versão, checksum e caminho. Testar também bancos sem registros e bancos com registros históricos incompletos.
- Rollback envolve código e cópia compatível do banco; não executar uma versão antiga sobre um schema novo sem compatibilidade demonstrada.
- Protocolos v1 e v2 têm testes de contrato durante transição. O cliente identifica protocolo incompatível e pede atualização de página, em vez de interpretar mensagens de controle como saída.

## 13. Matriz de testes que bloqueia a publicação

| ID | Cenário | Resultado obrigatório |
| --- | --- | --- |
| T01 | Terminal parado por 60 s | Altura estável, sem tempestade de resize. |
| T02 | Zoom, fonte, telas estreitas, divisão e aba oculta | Apenas dimensões válidas, consistentes com o PTY. |
| T03 | Resize inválido, fracionário ou não autorizado | Erro tipado; conexão e tamanho anterior preservados. |
| T04 | Colagem JSON, UTF-8, texto iniciado por `{`, várias linhas | Bytes recebidos correspondem à entrada. |
| T05 | Desconectar somente visualização | Processo continua; mensagem e botão corretos. |
| T06 | Alternar abas/divisão 100 vezes | Histórico e contexto corretos; sem cursores cruzados. |
| T07 | Consumidor xterm lento e ACK atrasado | Janela respeitada; memória limitada; entrada responsiva. |
| T08 | Dois observadores, um persistentemente lento | Observador saudável recebe saída; lento tem motivo explícito. |
| T09 | Saída intensa sem visualizadores | Produtor segue política definida e histórico gira com lacuna visível. |
| T10 | Processo rápido com saída final e GET concorrente | Saída chega antes de `exit`; consulta não remove o runtime. |
| T11 | Ctrl+C, resize e programa de tela inteira no Linux | Sinais e terminal controlador corretos. |
| T12 | Refresh, breve queda, logout, expiração e revogação | Recuperação por causa; nenhum replay incerto de entrada. |
| T13 | Cancelamento enfileirado, executando e durante limpeza | Estado final somente após confirmação; sem início tardio. |
| T14 | Worker por 10/60 s e saída maior que 64 KiB | Sem timeout administrativo indevido nem resposta inválida. |
| T15 | Helper ocupado e cancelamento de outro contexto | Controle permanece responsivo e recursos não se misturam. |
| T16 | Artefatos privilegiados em instalação limpa/upgrade | Usuário do motor não consegue modificá-los. |
| T17 | Ligolo falha ou cancela em cada etapa | Recursos registrados; limpeza reversa ou pendência rastreável. |
| T18 | Dois CIDRs sobrepostos, HTTP e DNS conhecidos | Identificador correto por contexto; isolamento demonstrado. |
| T19 | Queda do salto SSH ou proxy | Dependentes degradados e provas invalidadas. |
| T20 | Troca de perfil SFTP com resposta atrasada | Arquivos e ações continuam vinculados à máquina correta. |
| T21 | Transferência Unicode, conflito, interrupção e quota | Resultado verdadeiro; arquivo parcial não aparece finalizado. |
| T22 | SSE interrompido e retomado com lacuna | Reconciliação completa, sem duplicar operações. |
| T23 | Plano expirado/alterado e duplo clique | Conflito ou replay idempotente; nunca execução divergente. |
| T24 | Reinício, migração e restauração | História preservada; nenhuma conexão ou comando reativado automaticamente. |

## 14. E7 — Condições de publicação e entrega

1. Executar unitários, integração, Ruff, Black, mypy, tipagem TypeScript e build do frontend.
2. Executar Playwright sobre a wheel que será distribuída, e não apenas contra o servidor Vite de desenvolvimento. Preservar trace e screenshot de falha sem segredos.
3. Rodar laboratório Linux reproduzível com OpenSSH, salto intermediário e serviços conhecidos. Testes de PTY e namespace ignorados no Windows precisam passar neste job obrigatório do CI.
4. Testar instalação e upgrade com o usuário systemd sem acesso ao checkout; garantir ownership e isolamento da E2 após reinstalação.
5. Fazer teste prolongado inicial de 30 minutos com 20 terminais, dois observadores de um mesmo terminal, saída intensa, quatro tarefas concorrentes e eventos frequentes. Medir memória, filas, latência de entrada e crescimento de recursos. É uma meta de validação, não capacidade comprovada.
6. Exigir zero exceções não tratadas nos cenários da matriz; crescimento de memória proporcional aos limites configurados, sem deriva contínua após estabilização.
7. Validar na Kali quando estiver disponível: versão/hash instalados, login, duas sessões SSH, terminal parado, Ctrl+C, mudança de abas, inspeção, transferência e acesso a serviço conhecido. Não usar alvos operacionais para teste de carga.
8. Registrar por requisito o commit, comando/teste, resultado, ambiente e evidência. A etapa só muda para “validada na instalação” após esse fluxo.
9. Publicar release versionada, instrução de atualização, sessões afetadas, backup e procedimento de recuperação. O frontend mostra versão do motor e revisão do build para identificar assets antigos.

O hotfix do resize tem conclusão própria. A estabilização completa exige todos os bloqueadores pertinentes acima; uma demonstração isolada de SSH ou Ligolo não encerra a revisão.

## 15. Referências técnicas consultadas

- [xterm.js — Flow Control](https://xtermjs.org/docs/guides/flowcontrol/): confirma que `write()` é assíncrono e descreve o uso de callbacks/ACK para controle de fluxo através de WebSocket. Fundamenta E3; não comprova o comportamento do Conduit.
- [MDN — ResizeObserver, Observation Errors](https://developer.mozilla.org/en-US/docs/Web/API/ResizeObserver#observation_errors): descreve ciclos entre observação e alteração de layout. Fundamenta o diagnóstico do resize; a reprodução local é a evidência específica do projeto.
- Código instalado de `frontend/node_modules/@xterm/addon-fit/src/FitAddon.ts`: versão usada nesta reprodução mede o elemento pai e redimensiona o terminal; não aplica o limite de 500 do motor.
