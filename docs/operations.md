# Operação diária

## Primeiro uso

O motor deve rodar na Kali ou em um servidor Linux dedicado. O navegador do Windows só controla
o workspace. Crie o workspace, execute `ctfws doctor` e confirme que `ssh`, `sftp` e Python estão
disponíveis. Para acessar a interface sem expor a API na rede:

```bash
conduit --workspace ~/ctf/pivot-lab start --host 127.0.0.1 --port 8765
# Em outro terminal do Windows:
ssh -L 8765:127.0.0.1:8765 kali@SERVIDOR_LINUX
```

Abra `http://127.0.0.1:8765` no Windows. O build React/xterm é servido quando está presente;
instalações mínimas usam o dashboard compatível embutido.

Quando `deploy/install-linux.sh` for usado, o instalador cria o workspace padrão e gera o código
de bootstrap em `/etc/ctfws/ctfws.env`. O código aparece somente na saída daquela instalação;
guarde-o com segurança, faça o primeiro login e crie as contas locais antes de compartilhar o
acesso. Uma reinstalação preserva o arquivo de ambiente existente e não troca o código sem uma
ação administrativa explícita.

O instalador pode ser chamado a partir de um checkout no home do operador: ele instala um wheel
na virtualenv de produção, sem deixar o serviço dependente desse diretório. Se o serviço já estiver
ativo, ele é reiniciado para carregar o release novo.

Na tela de login, cole o valor de `CTFWS_BOOTSTRAP_TOKEN` no campo **Código de bootstrap**.
Esse acesso é um administrador inicial para provisionamento; ele não cria uma conta local
automaticamente. Depois de entrar, abra **Configurações → Contas**, informe usuário, senha com
no mínimo 12 caracteres e papel, e clique em **Criar conta**. Escolha `Operador` para quem pode
executar operações autorizadas, `Observador` para consulta e `Administrador` somente quando a
gestão de contas for necessária. Não existe autorregistro público: permitiria que qualquer pessoa
com acesso à interface criasse um operador. O código de bootstrap e as senhas nunca devem ser
enviados para o chat ou colocados em comandos e logs.

## Fluxo guiado

1. Em **Conectar máquina**, cole um comando SSH. O parser aceita somente destino, porta, chave,
   known-hosts, `-J`/`ProxyJump` e opções de conexão reconhecidas. Saltos são associados a
   perfis salvos por host/usuário/porta; um salto ambíguo ou ausente precisa ser cadastrado antes
   de salvar o destino. `ProxyCommand`, `LocalCommand`, `RemoteCommand` e texto executável são recusados.
2. Valide o comando e escolha salvar ou **Conectar e inspecionar**. A senha informada no assistente
   é usada somente na tentativa e apagada da interface; use SSH agent, chave local ou segredo
   temporário. O perfil mostra `disconnected`, `connecting`, `ready`,
   `degraded` ou `error`.
   O método pode ser SSH agent/chave, senha temporária ou passphrase temporária da chave. Para
   perfis com `auth_ref=vault:...`, `auth_method=password` envia o segredo ao campo de senha do
   AsyncSSH e `auth_method=key_passphrase` envia-o apenas ao desbloqueio da chave; nenhum dos
   dois é persistido no workspace.
   Erros de conexão carregam uma categoria estável para orientar a correção: `host_key_unknown`
   exige revisão explícita da identidade do servidor, `host_key_changed` exige investigar a
   alteração da chave, `credential_rejected` indica falha de autenticação e `host_unavailable`
   indica indisponibilidade, timeout ou falha de resolução. Mensagens não classificadas usam
   `ssh_error`; nenhum desses estados é convertido em sucesso.
3. Abra quantos terminais forem necessários pelo mesmo perfil. Cada terminal tem seu canal, PTY,
   resize, UTF-8, ANSI, Ctrl+C, abas/visualização e controle de entrada exclusivo. A API v2
   entrega sequência de saída e cursor de retomada; se o buffer limitado não cobrir o cursor,
   a interface informa a lacuna.
   No dashboard, o canal remoto é aberto pelo `AsyncSSHConnectionManager` compartilhado; isso
   permite reutilizar a mesma conexão para terminais e SFTP sem criar um processo `ssh` por ação.
   Ocultar a aba ou desconectar sua visualização não encerra o processo; use **Encerrar** somente
   quando quiser parar o terminal. Terminais nascem privados. Um operador precisa usar a ação
   **Renomear**, **Buscar na saída** e **Dividir tela** alteram apenas a organização/visualização,
   não o processo. A divisão usa uma segunda visualização somente leitura; ela não duplica uma
   shell ou um fluxo TCP.
   Use **Compartilhar visualização** para permitir que observadores assinem o WebSocket; tornar privado
   novamente revoga novas visualizações, sem interromper o processo. Compartilhar não concede
   controle de entrada: o operador que já detém o controle continua sendo o único a escrever.
   O extra web instala também o backend `websockets` do Uvicorn; sem ele, a página HTTP abre,
   mas o upgrade WebSocket do terminal não funciona.
4. Clique em **Inspecionar**. A tarefa retorna `202`, progride em `/api/v2/.../jobs` e registra
   snapshots, comandos, proveniência e resultados parciais. Um timeout não remove uma observação
   anterior. Abra **Atividade** para acompanhar a atualização automática; cada job de inspeção
   pode ser expandido para consultar as etapas, o comando fixo, o tamanho, os erros e a saída
   bruta de cada coleta. O histórico também fica disponível em `/api/v1/collections` ou em
   `/api/v1/collections/{collection_id}` (e nos equivalentes v2), sempre limitado ao workspace.
   Se a inspeção falhar depois de iniciar, a coleta parcial continua consultável e não é apagada.
5. O mapa apresenta candidatos e caminhos prontos. Para um serviço, escolha **Tunnelar**. O
   sistema mostra conexão, destino, porta local, dependências e comando antes de criar o plano.
   A porta `0` pede uma porta efêmera ao sistema; o motor mantém o socket reservado durante a
   revisão, impede colisões entre forwards e contextos do mesmo workspace e revalida a porta
   exata imediatamente antes do listener real. Após um restart, a reserva antiga não é assumida:
   o start faz uma nova verificação e falha explicitamente se a porta estiver ocupada.
6. Inicie somente o plano revisado. A API exige `{"confirm": true}` no início de túneis,
   contextos e namespaces; a interface envia essa confirmação somente após a revisão. `active`
   significa processo e listener observados; `degraded`
   significa que alguma parte ainda não foi confirmada; `error` nunca é apresentado como sucesso.
   No dashboard, forwards SSH usam o listener da conexão AsyncSSH compartilhada e são fechados
   pelo motor antes da conexão ser encerrada; forwards de outros adapters continuam processos
   externos com identidade verificada. `POST /api/v2/workspaces/{id}/tunnels/preview` permite
   revisar o comando sem criar o recurso. O catálogo em `/capabilities` informa os papéis
   (`client`, `server`, `control`), o local permitido (`motor`, `remote`, `context`), binários
   detectados e limitações de cada adapter.
7. Em Arquivos, use `loot/inbox` para ferramentas e evidências. Com AsyncSSH disponível, a
   transferência usa SFTP compartilhado, streaming, arquivo temporário, finalização atômica,
   limite padrão de 1 GiB (ajustável por `CTFWS_MAX_FILE_BYTES`), hash incremental e releitura remota para verificar a integridade; sem esse
   transporte, o fallback SFTP de processo continua disponível. A área local aceita arrastar
   arquivos e seleção múltipla; **Enviar** usa um destino remoto explícito. Em conflito, a política
   escolhida é **cancelar**, **manter ambos** (sufixo numérico) ou **substituir explicitamente**;
   a mesma decisão vale para uploads locais, SFTP e ferramentas. Enviar uma ferramenta não a
   executa.
8. Em **Ferramentas**, cadastre um caminho existente no motor Kali. O catálogo calcula SHA-256,
   registra arquitetura/versão e permite enviar explicitamente para uma conexão SSH por
   `POST /api/v2/workspaces/{id}/tools/{tool_id}/transfer/{connection_id}`. A operação retorna
   `202` e `job_id`, revalida o hash antes do envio e não executa a ferramenta. Se o arquivo mudou,
   o job falha e exige novo cadastro.
9. Em **Atividade**, consulte jobs e auditoria com ator, ação, recurso e resultado; a auditoria
   não registra senhas, chaves privadas ou tokens.

## Caminhos e verificações

O mapa separa inferência de prova. Uma sessão SSH ativa pode explicar um caminho, mas não verifica
uma sub-rede inteira. Use `ctfws path verify ID` ou a ação equivalente da API para testar um único
`endereço:porta` TCP explicitamente declarado. A prova guarda contexto, latência, resultado,
horário e expira em cinco minutos. Timeout, permissão negada e ferramenta ausente não são
interpretados como remoção de host.

## SOCKS e contextos roteados

Um contexto SOCKS gera um plano SSH `-D` associado ao perfil e mostra que a resolução e o tráfego
dependem da aplicação. SOCKS/proxychains não é isolamento universal: o suporte é para TCP de
binários dinamicamente ligados. UDP, binários estáticos e protocolos que ignoram proxy exigem
outra capacidade.

Com o contexto ativo, `POST /api/v2/workspaces/{id}/contexts/{context_id}/launcher` gera um
launcher revisável. O modo `environment` retorna `ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY` e um
`argv` estruturado; o modo `proxychains` grava uma configuração sem segredos no diretório
operacional do contexto. Depois da revisão, `POST .../contexts/{context_id}/execute` executa a
mesma lista de argumentos como uma tarefa assíncrona, sem shell livre, com timeout e saída limitada
para a área de Atividade. Copiar o comando continua disponível, mas não é necessário no fluxo
normal. UDP, ICMP e binários estaticamente ligados não são encaminhados automaticamente.

Contextos roteados permanecem indisponíveis até o helper de namespace, o manifesto de versão do
adaptador e os testes de contrato estarem instalados. O Conduit fixa inicialmente o Ligolo-ng
`0.9.1`, usando os artefatos oficiais e seus checksums publicados. O manifesto precisa declarar
`contract = "ctfws-routed-context-v1"`, fixar `version`, caminhos dos dois binários e seus
SHA-256; `CTFWS_LIGOLO_VERSION` apenas identifica a instalação e não substitui a verificação dos
arquivos. O helper expõe um plano de aplicação e um
plano de limpeza reverso (`.../namespace-plan` e `.../namespace-cleanup-plan`) e valida o lote
inteiro antes de executar qualquer operação privilegiada. Ele aceita somente operações tipadas
do workspace; a rota padrão e o DNS global da Kali não são substituídos.

Quando o serviço `ctfws-namespace-helper.service` estiver explicitamente habilitado, o motor usa
`CTFWS_NAMESPACE_SOCKET=/run/ctfws/namespace-helper.sock`. O helper root-owned aceita apenas
`create`, `route-add`, `route-delete` e `delete` em lotes JSON versionados, confirma o workspace e
o contexto antes de chamar `ip`, registra o manifesto retornado e expõe o caminho de limpeza:

```bash
sudo systemctl enable --now ctfws-namespace-helper.service
curl -X POST 'http://127.0.0.1:8765/api/v2/workspaces/1/contexts/3/namespace/prepare?device=ctfws0'
curl -X POST 'http://127.0.0.1:8765/api/v2/workspaces/1/contexts/3/namespace/remove'
```

Preparar o namespace não inicia Ligolo, não altera a rota padrão e não marca o contexto como
ativo. O diagnóstico mostra separadamente a presença do helper, a versão, os checksums e os
binários. Um contexto só é oferecido como ativo quando transporte, namespace e destino tiverem
provas próprias; a instalação não habilita o helper automaticamente.

## Reinício e retomada

O restart não reconecta SSH, não repete comandos e não inicia túneis. O banco preserva layout,
planos, eventos, evidências e estados. Consulte:

```bash
ctfws --workspace ~/ctf/pivot-lab resume-plan
ctfws --workspace ~/ctf/pivot-lab resume
```

Na API, `GET /api/v2/workspaces/{id}/resume` apresenta o plano e `POST` aplica apenas os IDs
selecionados. Terminais que estavam rodando ficam interrompidos até o operador abrir um novo
terminal; uma sessão tmux só pode ser reassociada quando esse modo tiver sido escolhido.
No dashboard, **Atividade → Retomar ambiente** transforma esse plano em caixas de seleção e só
envia os recursos marcados após o clique do operador.

Cada terminal, forward e contexto registra a identidade efêmera do motor que o possui enquanto
está ativo. Essa mesma identidade aparece no lock do motor; após encerramento confirmado ela é
removida. Depois de um restart, o plano continua apenas como retomada manual e não é assumido
silenciosamente por outra instância.

## Equipe e segurança

`CTFWS_AUTH_REQUIRED=1` protege a API com o bootstrap definido em `CTFWS_BOOTSTRAP_TOKEN`:

```bash
curl -s http://127.0.0.1:8765/api/v2/auth/login \
  -H 'content-type: application/json' \
  -d '{"bootstrap_token":"VALOR_FORA_DO_LOG"}'
```

Use o bearer retornado em HTTP, SSE e WebSocket. Sessões SSE/WebSocket são revalidadas durante a
execução; logout, revogação ou perda de membership encerra o canal quando a próxima verificação
ocorre. O cofre é opt-in; `POST /api/v2/auth/secrets`
retorna somente `auth_ref`, guarda AES-GCM em `~/.config/ctfws` e mantém a chave mestra fora do
workspace. Ao associar uma referência `vault:` ao perfil, o motor resolve a credencial somente
em memória para a conexão AsyncSSH; perfis nunca retornam o conteúdo do segredo. A versão atual implementa um motor por
workspace; contas locais persistentes e validação OIDC funcionam no motor único, mas quotas por
membro, associação entre workspaces e isolamento entre organizações precisam de um gateway
empresarial antes de serem considerados produção multi-tenant.

## Chaves de host SSH

Conexões novas não aceitam uma chave desconhecida silenciosamente. O Conduit interrompe o teste,
mostra o tipo de chave e a fingerprint SHA-256 e oferece **Confiar e continuar**. Compare a
fingerprint com uma fonte confiável do laboratório antes de confirmar. A chave confirmada é gravada
no `known_hosts` do usuário do motor (ou no arquivo explicitamente informado no perfil), e o teste
é repetido com a senha temporária ainda mantida somente em memória. Se a chave mudar depois de
ser confiada, a conexão continua falhando como `host_key_changed`; remova ou revise a entrada
manualmente após investigar a causa. Recusar a confirmação deixa o perfil sem conexão.

Para integrar um IdP OIDC no motor, configure `CTFWS_OIDC_ISSUER`,
`CTFWS_OIDC_AUDIENCE` e, opcionalmente, `CTFWS_OIDC_JWKS_URL`. O bearer JWT precisa ser assinado
por uma chave RSA publicada pelo IdP e conter `iss`, `aud`, `sub` e `exp`. Por segurança, tokens
sem mapeamento de grupo começam como `observer`; configure `CTFWS_OIDC_ADMIN_GROUP`,
`CTFWS_OIDC_OPERATOR_GROUP` ou `CTFWS_OIDC_OBSERVER_GROUP` para atribuir papéis. O token pode ser
enviado no cabeçalho `Authorization` ou trocado por uma sessão local em
`POST /api/v2/auth/login` com `{"oidc_token":"..."}`; ele nunca é persistido pelo workspace.

## Backup, restore e diagnóstico

```bash
ctfws --workspace ~/ctf/pivot-lab backup --full
ctfws --workspace ~/ctf/pivot-lab restore backup.zip --destination ~/ctf/restored-lab
ctfws --workspace ~/ctf/pivot-lab doctor
```

O backup completo contém banco consistente, notas, evidências, relatórios, loot, logs e manifesto
SHA-256; não contém a chave mestra do cofre. Restore padrão usa uma pasta nova e valida todos os
hashes. PIDs, handles, conexões e listeners nunca são restaurados como ativos. Contas locais e
papéis `admin`/`operator`/`observer` são persistidos apenas como hashes de senha; tokens OIDC
também não são persistidos. A associação de membros pode ser exigida no motor único com
`CTFWS_REQUIRE_MEMBERSHIP=1`; o bootstrap admin provisiona os primeiros membros em
`/api/v2/workspaces/{id}/members`, e o papel efetivo nunca supera o papel global do ator. O
gateway multi-motor continua fora desta instalação.

### Quota e retenção

`CTFWS_MAX_WORKSPACE_BYTES` limita o armazenamento total de arquivos regulares do workspace;
uploads locais e downloads SFTP verificam a quota durante o streaming, antes da finalização
atômica. `CTFWS_RETENTION_DAYS` habilita a identificação de logs antigos e arquivos temporários
`.ctfws-*.part`. `GET /api/v2/workspaces/{id}/maintenance` mostra uso e candidatos; somente
`POST /api/v2/workspaces/{id}/maintenance/prune` remove esses candidatos. Diretórios `evidence`
e `loot` nunca são removidos pela retenção.

### Forwards legados

Desde a migração do schema 32, mantida no schema 33, um forward novo persiste `command_argv_json`
e o motor chama o executável diretamente com essa lista de argumentos, sem shell. Forwards migrados de versões anteriores
mantêm o texto original para auditoria, mas não são executados automaticamente; recrie o plano
pela interface ou pela CLI depois de revisar destino, porta, transporte e local de execução.
