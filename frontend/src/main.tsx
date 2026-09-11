import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";
import { createRoot } from "react-dom/client";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import "./styles.css";

type View = "overview" | "machines" | "terminals" | "tunnels" | "files" | "tools" | "activity" | "settings";
type Json = Record<string, unknown>;
type Profile = Json & { id: number; name: string; host: string; user: string; port: number; state: string };
type TerminalRow = Json & { id: number; name?: string; context_label: string; status: string; sharing?: string; connection_id?: number; runtime_available?: boolean; reconnectable?: boolean; availability_reason?: string | null };
type CredentialKind = "password" | "key_passphrase";
type HostKeyRequest = { connectionId: number; password?: string; credentialKind?: CredentialKind; hostKey: Json };

const api = "/api/v1";
const apiV2 = "/api/v2";
const tokenKey = "ctfws.auth.token";
const viewKey = "ctfws.ui.view";
const viewNames: View[] = ["overview", "machines", "terminals", "tunnels", "files", "tools", "activity", "settings"];
type ApiFailure = Error & { status?: number };

function initialView(): View {
  const saved = localStorage.getItem(viewKey) as View | null;
  return saved && viewNames.includes(saved) ? saved : "overview";
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  return requestAt<T>(api, path, options);
}

async function requestV2<T>(path: string, options?: RequestInit): Promise<T> {
  return requestAt<T>(apiV2, path, options);
}

async function requestAt<T>(base: string, path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  const token = localStorage.getItem(tokenKey);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(base + path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const payload = body as Json;
    const detail = typeof payload.message === "string"
      ? payload.message
      : typeof payload.detail === "string" ? payload.detail : response.statusText;
    const failure = new Error(detail) as ApiFailure;
    failure.status = response.status;
    throw failure;
  }
  return body as T;
}

function App() {
  const [view, setView] = useState<View>(initialView);
  const [summary, setSummary] = useState<Json>({});
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [terminals, setTerminals] = useState<TerminalRow[]>([]);
  const [paths, setPaths] = useState<Json[]>([]);
  const [hosts, setHosts] = useState<Json[]>([]);
  const [notice, setNotice] = useState("Motor online");
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [terminalId, setTerminalId] = useState<number | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [workspaceId, setWorkspaceId] = useState<number | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Json[]>([]);
  const [role, setRole] = useState<string | null>(null);
  const [hostKeyRequest, setHostKeyRequest] = useState<HostKeyRequest | null>(null);
  const refreshRef = useRef<() => Promise<void>>(async () => undefined);

  const refresh = async () => {
    try {
      const [nextSummary, nextProfiles, nextTerminals, nextPaths, nextHosts, session] = await Promise.all([
        request<Json>("/summary"),
        request<Profile[]>("/connections"),
        request<TerminalRow[]>("/terminals"),
        request<Json[]>("/paths"),
        request<Json[]>("/hosts"),
        requestV2<Json>("/auth/session"),
      ]);
      setSummary(nextSummary);
      setProfiles(nextProfiles);
      setTerminals(nextTerminals);
      setPaths(nextPaths);
      setHosts(nextHosts);
      setRole(session.authenticated ? String(session.role ?? "") : null);
      setNotice("Motor online");
    } catch (error) {
      if ((error as ApiFailure).status === 401) {
        setRole(null);
        setNeedsLogin(true);
      }
      setNotice(`Atenção: ${error instanceof Error ? error.message : String(error)}`);
    }
  };
  refreshRef.current = refresh;

  useEffect(() => {
    if (view === "settings" && role !== "admin") setView("overview");
  }, [role, view]);

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    localStorage.setItem(viewKey, view);
  }, [view]);

  useEffect(() => {
    const query = searchQuery.trim();
    if (query.length < 2) {
      setSearchResults([]);
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void request<Json[]>(`/search?query=${encodeURIComponent(query)}`)
        .then(results => { if (!cancelled) setSearchResults(results.slice(0, 12)); })
        .catch(() => { if (!cancelled) setSearchResults([]); });
    }, 220);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [searchQuery]);

  const openSearchResult = (kind: string) => {
    setSearchQuery("");
    setSearchResults([]);
    setView(kind === "Host" || kind === "Network" ? "machines" : kind === "Command" ? "terminals" : "activity");
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(value => !value);
      }
      if (event.key === "Escape") setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const subscribe = async () => {
      try {
        const headers = new Headers();
        const token = localStorage.getItem(tokenKey);
        if (token) headers.set("Authorization", `Bearer ${token}`);
        const workspaces = await fetch("/api/v2/workspaces", { headers, signal: controller.signal });
        if (!workspaces.ok) return;
        const rows = await workspaces.json() as Json[];
        const workspaceId = Number(rows[0]?.id);
        if (!workspaceId) return;
        setWorkspaceId(workspaceId);
        const response = await fetch(`/api/v2/workspaces/${workspaceId}/events`, { headers, signal: controller.signal });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            if (frame.includes("data:")) void refreshRef.current();
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) console.debug("SSE indisponível", error);
      }
    };
    void subscribe();
    return () => controller.abort();
  }, [needsLogin]);

  const openTerminal = async (connectionId?: number) => {
    try {
      const row = await request<TerminalRow>("/terminals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: `${connectionId ? "ssh" : "local"}-${Date.now()}`,
          kind: connectionId ? "ssh" : "local",
          ...(connectionId ? { connection_id: connectionId } : {}),
        }),
      });
      setTerminalId(row.id);
      setView("terminals");
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };

  const inspect = async (connectionId: number) => {
    try {
      const job = await request<Json>(`/connections/${connectionId}/inspect`, { method: "POST" });
      setNotice(`Inspeção iniciada como tarefa #${String(job.job_id)}.`);
      window.dispatchEvent(new Event("ctfws:activity-refresh"));
      setView("activity");
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };

  const closeTerminal = async (id: number) => {
    try {
      await request(`/terminals/${id}`, { method: "DELETE" });
      setTerminalId(null);
      setNotice(`Terminal #${id} encerrado.`);
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };

  const renameTerminal = async (id: number, name: string) => {
    try {
      await request<TerminalRow>(`/terminals/${id}/rename`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      setNotice("Terminal renomeado.");
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };

  const shareTerminal = async (id: number, shared: boolean) => {
    try {
      await request<TerminalRow>(`/terminals/${id}/share`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ shared }),
      });
      setNotice(shared ? "Visualização do terminal compartilhada." : "Visualização do terminal tornou-se privada.");
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };

  return <div className="app-shell">
    {needsLogin && <LoginPanel onLoggedIn={() => { setNeedsLogin(false); void refresh(); }} />}
    {hostKeyRequest && <HostKeyPrompt request={hostKeyRequest} onCancel={() => setHostKeyRequest(null)} onConfirm={acceptHostKey} />}
    <header className="topbar">
      <div className="brand"><span className="brand-mark">⌁</span><span>Conduit</span></div>
      <div className="global-search"><input aria-label="Buscar no workspace" value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder="Buscar máquinas, redes, notas…" />{searchQuery.trim().length >= 2 && <div className="search-results" role="listbox">{searchResults.length ? searchResults.map((result, index) => <button type="button" className="search-result" key={`${String(result.kind)}-${String(result.label)}-${index}`} onClick={() => openSearchResult(String(result.kind))}><span className="search-kind">{String(result.kind)}</span><span><strong>#{String(result.label)}</strong><small>{String(result.detail)}</small></span></button>) : <div className="search-empty">Nenhum resultado</div>}</div>}</div><div className="top-status"><span className="status-dot" />{notice}</div>
    </header>
    <div className="body-layout">
      <aside className="sidebar">
        <div className="workspace-label">WORKSPACE</div>
        <nav>{([
          ["overview", "Visão geral", "◈"], ["machines", "Máquinas", "▣"],
          ["terminals", "Terminais", "⌘"], ["tunnels", "Túneis e contextos", "⇄"],
          ["files", "Arquivos e evidências", "▧"], ["tools", "Ferramentas", "⚒"], ["activity", "Atividade", "◷"],
          ...(role === "admin" ? [["settings", "Configurações", "⚙"]] : []),
        ] as [View, string, string][]).map(([key, label, icon]) =>
          <button key={key} className={view === key ? "nav-item active" : "nav-item"} onClick={() => setView(key)}>
            <span>{icon}</span>{label}
          </button>)}
        </nav>
        <div className="sidebar-bottom"><div className="engine-card"><span className="status-dot" /> Motor local ativo<div className="muted">Retomada manual disponível</div></div></div>
      </aside>
      <main className="main-content">
        <div className="page-heading"><div><div className="eyebrow">OPERAÇÃO AUTORIZADA</div><h1>{view === "overview" ? "Visão geral" : viewLabel(view)}</h1><p>Organize infraestrutura, acesso e evidências com estados verificáveis.</p></div><div className="heading-actions"><button className="button secondary" onClick={() => void refresh()}>Atualizar</button><button className="button primary" onClick={() => setConnectionOpen(true)}>+ Conectar máquina</button></div></div>
        {connectionOpen && <ConnectionPanel onClose={() => setConnectionOpen(false)} onSaved={(id, password, inspectAfterSave, credentialKind) => { setConnectionOpen(false); void refresh(); if (inspectAfterSave) void testAndInspect(id, password, credentialKind); }} />}
        {view === "overview" && <Overview summary={summary} profiles={profiles} terminals={terminals} paths={paths} onConnect={() => setConnectionOpen(true)} onTerminal={(id) => void openTerminal(id)} onInspect={(id) => void inspect(id)} onLocal={() => void openTerminal()} />}
        {view === "machines" && <Machines hosts={hosts} profiles={profiles} onTerminal={(id) => void openTerminal(id)} onInspect={(id) => void inspect(id)} />}
        {view === "terminals" && <TerminalView terminals={terminals} selected={terminalId} workspaceId={workspaceId} onSelect={setTerminalId} onLocal={() => void openTerminal()} onNewRemote={(id) => void openTerminal(id)} onClose={(id) => void closeTerminal(id)} onShare={(id, shared) => void shareTerminal(id, shared)} onRename={(id, name) => void renameTerminal(id, name)} />}
        {view === "tunnels" && <><ContextPlanner profiles={profiles} /><Tunnels paths={paths} profiles={profiles} /></>}
        {view === "files" && <Files profiles={profiles} setNotice={setNotice} />}
        {view === "tools" && <Tools profiles={profiles} setNotice={setNotice} />}
        {view === "activity" && <Activity />}
        {view === "settings" && role === "admin" && <AccountManagement setNotice={setNotice} onLogout={logout} />}
      </main>
    </div>
    {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} onNavigate={(nextView) => { setView(nextView); setPaletteOpen(false); }} onConnect={() => { setConnectionOpen(true); setPaletteOpen(false); }} onRefresh={() => { setPaletteOpen(false); void refresh(); }} />}
  </div>;

  async function testAndInspect(connectionId: number, password?: string, credentialKind?: "password" | "key_passphrase") {
    try {
      const tested = await request<Json>(`/connections/${connectionId}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(password ? { password, ...(credentialKind ? { kind: credentialKind } : {}) } : {}),
      });
      if (String(tested.state) !== "ready") {
        const hostKey = tested.host_key;
        if (hostKey && typeof hostKey === "object") {
          setHostKeyRequest({
            connectionId,
            password,
            credentialKind,
            hostKey: hostKey as Json,
          });
          setNotice("Aguardando confirmação da chave do host.");
          return;
        }
        setNotice(`Conexão não validada: ${String(tested.error ?? tested.state)}`);
        return;
      }
      const job = await request<Json>(`/connections/${connectionId}/inspect`, { method: "POST" });
      setNotice(`Conectado. Inspeção iniciada como tarefa #${String(job.job_id)}.`);
      window.dispatchEvent(new Event("ctfws:activity-refresh"));
      setView("activity");
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  }

  async function acceptHostKey() {
    if (!hostKeyRequest) return;
    const current = hostKeyRequest;
    try {
      await request<Json>(`/connections/${current.connectionId}/trust-host-key`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fingerprint: current.hostKey.fingerprint }),
      });
      setHostKeyRequest(null);
      setNotice("Chave do host confiada. Revalidando a conexão…");
      await testAndInspect(current.connectionId, current.password, current.credentialKind);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    }
  }

  async function logout() {
    try { await requestV2<Json>("/auth/logout", { method: "POST" }); } catch { /* session may already be gone */ }
    localStorage.removeItem(tokenKey);
    setRole(null);
    setView("overview");
    setNeedsLogin(true);
  }
}

function viewLabel(view: View) { return ({ overview: "Visão geral", machines: "Máquinas", terminals: "Terminais", tunnels: "Túneis e contextos", files: "Arquivos e evidências", tools: "Ferramentas", activity: "Atividade", settings: "Configurações" })[view]; }

function Overview({ summary, profiles, terminals, paths, onConnect, onTerminal, onInspect, onLocal }: { summary: Json; profiles: Profile[]; terminals: TerminalRow[]; paths: Json[]; onConnect: () => void; onTerminal: (id: number) => void; onInspect: (id: number) => void; onLocal: () => void }) {
  return <>
    <section className="metrics">{([["máquinas", summary.hosts ?? 0], ["conexões", summary.connections ?? 0], ["terminais ativos", summary.active_terminals ?? 0], ["túneis", summary.forwards ?? 0]] as [string, unknown][]).map(([label, value]) => <div className="metric-card" key={label}><div className="metric-label">{label}</div><div className="metric-value">{String(value)}</div></div>)}</section>
    <div className="content-grid"><section className="panel span-two"><PanelHeader title="Comece por aqui" subtitle="O caminho guiado do workspace" /><div className="quick-actions"><button onClick={onConnect}><b>1</b><span><strong>Conecte uma máquina</strong><small>Cole seu SSH e valide a identidade</small></span><i>→</i></button><button onClick={() => profiles[0] && onInspect(profiles[0].id)} disabled={!profiles.length}><b>2</b><span><strong>Inspecione a rede</strong><small>Interfaces, rotas, vizinhos e serviços</small></span><i>→</i></button><button onClick={() => profiles[0] && onTerminal(profiles[0].id)} disabled={!profiles.length}><b>3</b><span><strong>Abra um terminal</strong><small>Crie abas independentes sem repetir o SSH</small></span><i>→</i></button></div></section>
      <section className="panel"><PanelHeader title="Conexões" action={<span className="pill">{profiles.length} salvas</span>} />{profiles.length ? profiles.map(profile => <div className="list-row" key={profile.id}><div className="row-icon">⌁</div><div className="row-main"><strong>{profile.name}</strong><small>{profile.user}@{profile.host}:{profile.port}</small>{typeof profile.last_error === "string" && <small className="connection-error">{profile.last_error}</small>}</div><span className={`state ${profile.state}`}>{profile.state}</span><button className="icon-button" title="Abrir terminal" onClick={() => onTerminal(profile.id)}>↗</button></div>) : <Empty text="Nenhuma conexão salva" />}</section>
      <section className="panel"><PanelHeader title="Terminais recentes" action={<button className="text-button" onClick={onLocal}>+ Novo</button>} />{terminals.length ? terminals.slice(-5).reverse().map(row => <div className="list-row" key={row.id}><div className="row-icon terminal-icon">⌘</div><div className="row-main"><strong>{row.context_label}</strong><small>Terminal #{row.id}</small></div><span className={`state ${row.status}`}>{row.status}</span></div>) : <Empty text="Abra seu primeiro terminal" />}</section>
      <section className="panel span-two"><PanelHeader title="Mapa de acesso" subtitle="Inferências separadas de verificações reais" action={<span className="pill">{paths.length} caminhos</span>} />{paths.length ? <div className="path-list">{paths.slice(0, 8).map(path => <div className="path-row" key={String(path.id)}><span className={`path-state ${String(path.state)}`}>{String(path.state)}</span><strong>{String(path.target_address)}{path.target_port ? `:${String(path.target_port)}` : ""}</strong><span className="muted">{String(path.reason ?? "")}</span></div>)}</div> : <Empty text="Inspecione uma máquina para montar o mapa" />}</section>
    </div>
  </>;
}

function Machines({ hosts, profiles, onTerminal, onInspect }: { hosts: Json[]; profiles: Profile[]; onTerminal: (id: number) => void; onInspect: (id: number) => void }) { return <div className="content-grid"><section className="panel span-two"><PanelHeader title="Máquinas observadas" subtitle="Escopo e proveniência permanecem visíveis" />{hosts.length ? hosts.map(host => <div className="machine-card" key={String(host.id)}><div className="machine-avatar">{String(host.name ?? "?").slice(0, 1).toUpperCase()}</div><div className="row-main"><strong>{String(host.name)}</strong><small>{String(host.ip)} · {String(host.os ?? "sistema não identificado")}</small></div><span className={`state ${String(host.status)}`}>{String(host.status)}</span>{profiles.filter(p => p.host_id === host.id).map(profile => <><button className="button small secondary" onClick={() => onInspect(profile.id)}>Inspecionar</button><button className="button small primary" onClick={() => onTerminal(profile.id)}>Terminal</button></>)}</div>) : <Empty text="Nenhuma máquina observada ainda" />}</section></div>; }

function Tunnels({ paths, profiles }: { paths: Json[]; profiles: Profile[] }) {
  const [selected, setSelected] = useState<Json | null>(paths.find(path => path.target_port) ?? null);
  const [localPort, setLocalPort] = useState(0);
  const [plan, setPlan] = useState<Json | null>(null);
  const [message, setMessage] = useState("");
  const [contexts, setContexts] = useState<Json[]>([]);
  const [forwards, setForwards] = useState<Json[]>([]);
  const [namespacePlan, setNamespacePlan] = useState<Json | null>(null);

  const loadResources = () => {
    void Promise.all([request<Json[]>("/contexts"), request<Json[]>("/forwards")])
      .then(([nextContexts, nextForwards]) => {
        setContexts(nextContexts);
        setForwards(nextForwards);
      })
      .catch(() => {
        setContexts([]);
        setForwards([]);
      });
  };

  useEffect(() => { loadResources(); }, []);
  useEffect(() => {
    const refresh = () => loadResources();
    window.addEventListener("ctfws:resources-changed", refresh);
    return () => window.removeEventListener("ctfws:resources-changed", refresh);
  }, []);

  const choose = (path: Json) => {
    setSelected(path);
    setPlan(null);
    setMessage("");
  };

  const createPlan = async () => {
    if (!selected || !selected.target_port) return;
    const hop = Number((selected.hop_host_ids as number[] | undefined)?.[0] ?? selected.target_host_id);
    const profile = profiles.find(item =>
      (selected.hop_host_ids as number[] | undefined)?.includes(Number(item.host_id))
    ) ?? profiles[0];
    try {
      const created = await request<Json>("/forwards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: `tunnel-${String(selected.target_address)}-${String(selected.target_port)}`,
          via_host_id: hop,
          connection_id: profile?.id,
          kind: "local",
          local_address: "127.0.0.1",
          local_port: localPort || 0,
          target_address: selected.target_address,
          target_port: selected.target_port,
          tool: "ssh",
        }),
      });
      setPlan(created);
      setForwards(current => [...current, created]);
      setMessage(`Plano #${String(created.id)} criado. Revise conexão, destino e execução.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const startPlan = async () => {
    if (!plan) return;
    try {
      const started = await request<Json>(`/forwards/${String(plan.id)}/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) });
      setPlan(started);
      setForwards(current => current.map(item => item.id === started.id ? started : item));
      setMessage(`Estado real: ${String(started.status)} · ${String(started.health ?? "sem diagnóstico")}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const checkForward = async (forward: Json) => {
    try {
      const checked = await request<Json>(`/forwards/${String(forward.id)}/check`, { method: "POST" });
      setForwards(current => current.map(item => item.id === checked.id ? checked : item));
      if (plan?.id === checked.id) setPlan(checked);
      setMessage(`Verificação do túnel #${String(checked.id)}: ${String(checked.status)} · ${String(checked.health ?? "sem diagnóstico")}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const toggleForward = async (forward: Json) => {
    const active = ["active", "starting", "degraded"].includes(String(forward.status));
    if (!active) {
      setPlan(forward);
      setSelected(null);
      setMessage(`Plano #${String(forward.id)} carregado. Confirme os dados antes de iniciar.`);
      return;
    }
    try {
      const updated = await request<Json>(`/forwards/${String(forward.id)}/stop`, { method: "POST" });
      setForwards(current => current.map(item => item.id === updated.id ? updated : item));
      if (plan?.id === updated.id) setPlan(updated);
      setMessage(`Túnel #${String(updated.id)}: ${String(updated.status)} · ${String(updated.health ?? "sem diagnóstico")}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const toggleContext = async (context: Json) => {
    const action = String(context.status) === "active" ? "stop" : "start";
    try {
      const requestOptions = action === "start" ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) } : { method: "POST" };
      const updated = await request<Json>(`/contexts/${String(context.id)}/${action}`, requestOptions);
      setContexts(current => current.map(item => item.id === context.id ? updated : item));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const reviewNamespace = async (context: Json) => {
    try {
      const operations = await request<Json[]>(`/contexts/${String(context.id)}/namespace-plan`);
      setNamespacePlan({ context_id: context.id, context_name: context.name, operations });
      setMessage("Plano de namespace carregado; revise antes de preparar.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const namespaceAction = async (context: Json, action: "prepare" | "remove") => {
    try {
      const requestOptions = action === "prepare" ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) } : { method: "POST" };
      const updated = await request<Json>(`/contexts/${String(context.id)}/namespace/${action}`, requestOptions);
      setContexts(current => current.map(item => item.id === updated.id ? updated : item));
      setNamespacePlan(null);
      setMessage(action === "prepare" ? "Namespace preparado; o transporte continua parado." : "Namespace removido.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const review = plan ?? selected;
  const reviewHop = Number(
    (review?.hop_host_ids as number[] | undefined)?.[0]
      ?? review?.target_host_id
      ?? review?.via_host_id
      ?? 0
  );
  const reviewProfile = profiles.find(profile => Number(profile.id) === Number(review?.connection_id));

  return <div className="content-grid">
    <section className="panel span-two">
      <PanelHeader title="Túneis existentes" subtitle="Iniciar exige revisão; verificar confirma o estado observado pelo motor" />
      {forwards.length ? forwards.map(forward => <div className="list-row" key={String(forward.id)}>
        <div className="row-icon">⇄</div>
        <div className="row-main">
          <strong>{String(forward.name)}</strong>
          <small>{String(forward.local_address)}:{String(forward.local_port)} → {String(forward.target_address ?? "SOCKS")}:{String(forward.target_port ?? "*")} · {String(forward.health ?? "sem verificação")}</small>
        </div>
        <span className={`state ${String(forward.status)}`}>{String(forward.status)}</span>
        {["active", "starting", "degraded"].includes(String(forward.status)) && <button className="button small secondary" onClick={() => void checkForward(forward)}>Verificar</button>}
        <button className="button small secondary" onClick={() => void toggleForward(forward)}>{["active", "starting", "degraded"].includes(String(forward.status)) ? "Parar" : "Revisar e iniciar"}</button>
      </div>) : <Empty text="Nenhum túnel planejado" />}
    </section>
    <section className="panel">
      <PanelHeader title="Caminhos disponíveis" subtitle="A inferência não é uma prova de acesso" />
      {paths.length ? paths.map(path => <button className={selected?.id === path.id ? "terminal-tab selected" : "terminal-tab"} key={String(path.id)} onClick={() => choose(path)}>
        <span className={`path-state ${String(path.state)}`}>{String(path.state)}</span>
        <span><strong>{String(path.target_address)}{path.target_port ? `:${String(path.target_port)}` : ""}</strong><small>{String(path.reason ?? "")}</small></span>
      </button>) : <Empty text="Inspecione uma máquina para criar caminhos" />}
    </section>
    <section className="panel">
      <PanelHeader title="Revisar acesso" subtitle="O túnel só inicia após esta revisão" />
      {review?.target_port ? <>
        <div className="review-grid">
          <div><span>DESTINO</span><strong>{String(review.target_address)}:{String(review.target_port)}</strong></div>
          <div><span>CONEXÃO</span><strong>{reviewProfile ? `${reviewProfile.user}@${reviewProfile.host}:${reviewProfile.port}` : `Perfil #${String(review.connection_id ?? "não associado")}`}</strong></div>
          <div><span>EXECUÇÃO</span><strong>Motor Kali / SSH</strong></div>
          <div><span>DEPENDÊNCIA</span><strong>Host #{String(reviewHop || "não informado")}</strong></div>
          <div><span>LISTENER</span><strong>{String(review.local_address ?? "127.0.0.1")}:{String(review.local_port ?? "automática")}</strong></div>
        </div>
        {!plan && <label>Porta local (0 escolhe uma disponível)<input type="number" min="0" max="65535" value={localPort} onChange={event => setLocalPort(Number(event.target.value))} /></label>}
        <div className="panel-actions">
          {!plan && <button className="button primary" onClick={() => void createPlan()}>Criar plano revisável</button>}
          {plan && <button className="button primary" onClick={() => void startPlan()} disabled={["active", "starting"].includes(String(plan.status))}>Confirmar e iniciar #{String(plan.id)}</button>}
          {plan && <button className="button secondary" onClick={() => { setPlan(null); setMessage("Revisão cancelada."); }}>Cancelar revisão</button>}
        </div>
        {plan && <pre className="plan-preview">{JSON.stringify({ command: plan.command, command_argv: plan.command_argv, status: plan.status, health: plan.health, listener: plan.listener_state, destination: plan.destination_state }, null, 2)}</pre>}
        {message && <div className="notice-message">{message}</div>}
      </> : <div className="empty large"><div className="empty-icon">⇄</div><strong>Selecione um serviço com porta</strong><span>Caminhos de rede sem uma porta específica não são apresentados como um túnel.</span></div>}
    </section>
    <section className="panel span-two">
      <PanelHeader title="Contextos de rede" subtitle="SOCKS e roteamento permanecem explícitos e independentes" />
      {contexts.length ? contexts.map(context => <div className="list-row" key={String(context.id)}>
        <div className="row-icon">⇄</div>
        <div className="row-main"><strong>{String(context.name)}</strong><small>{String(context.transport)} · {String(context.network_cidrs ?? "sem redes")}</small></div>
        <span className={`state ${String(context.status)}`}>{String(context.status)}</span>
        <button className="button small secondary" onClick={() => void toggleContext(context)}>{String(context.status) === "active" ? "Parar" : "Iniciar"}</button>
        {String(context.transport) === "routed" && (Array.isArray(context.resource_manifest) && context.resource_manifest.length ? <button className="button small secondary" onClick={() => void namespaceAction(context, "remove")}>Remover namespace</button> : <button className="button small secondary" onClick={() => void reviewNamespace(context)}>Revisar namespace</button>)}
      </div>) : <Empty text="Nenhum contexto planejado" />}
    </section>
    {namespacePlan && <section className="panel span-two">
      <PanelHeader title={`Preparar namespace · ${String(namespacePlan.context_name)}`} subtitle="A revisão não executa nada até a confirmação" />
      <pre className="plan-preview">{JSON.stringify(namespacePlan.operations, null, 2)}</pre>
      <div className="panel-actions"><button className="button primary" onClick={() => { const context = contexts.find(item => item.id === namespacePlan.context_id); if (context) void namespaceAction(context, "prepare"); }}>Preparar namespace</button><button className="button secondary" onClick={() => setNamespacePlan(null)}>Cancelar</button></div>
    </section>}
    <ContextLauncherPanel contexts={contexts} setMessage={setMessage} />
  </div>;
}

function ContextLauncherPanel({ contexts, setMessage }: { contexts: Json[]; setMessage: (value: string) => void }) {
  const active = contexts.filter(context => ["socks", "routed"].includes(String(context.transport)) && String(context.status) === "active");
  const [contextId, setContextId] = useState(Number(active[0]?.id ?? 0));
  const [program, setProgram] = useState("curl");
  const [argumentsText, setArgumentsText] = useState("");
  const [launcher, setLauncher] = useState("environment");
  const [plan, setPlan] = useState<Json | null>(null);
  const selectedContext = active.find(context => Number(context.id) === contextId);
  useEffect(() => { if (!active.some(context => Number(context.id) === contextId)) setContextId(Number(active[0]?.id ?? 0)); }, [contexts, contextId]);
  useEffect(() => { setLauncher(selectedContext?.transport === "routed" ? "namespace" : "environment"); }, [selectedContext?.id, selectedContext?.transport]);
  const generate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!contextId) { setMessage("Inicie um contexto SOCKS antes de gerar o launcher."); return; }
    try {
      const result = await request<Json>(`/contexts/${contextId}/launcher`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ program, arguments: argumentsText.split(/\r?\n/).map(item => item.trim()).filter(Boolean), launcher }) });
      setPlan(result);
      setMessage("Launcher gerado; o programa ainda não foi executado.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  };
  const copyCommand = async () => { if (!plan?.command) return; await navigator.clipboard?.writeText(String(plan.command)); setMessage("Comando copiado para a área de transferência."); };
  return <section className="panel span-two"><PanelHeader title="Usar ferramentas em contexto" subtitle="Gere um launcher por contexto; a aplicação escolhida continua sob controle do operador." />{active.length ? <form className="form-grid" onSubmit={event => void generate(event)}><label>Contexto ativo<select value={contextId} onChange={event => setContextId(Number(event.target.value))}>{active.map(context => <option key={String(context.id)} value={Number(context.id)}>{String(context.name)} · {String(context.transport)}{context.local_port ? ` · ${String(context.local_address)}:${String(context.local_port)}` : ""}</option>)}</select></label><label>Modo<select value={launcher} onChange={event => setLauncher(event.target.value)}>{selectedContext?.transport === "routed" ? <option value="namespace">Namespace roteado</option> : <><option value="environment">Variáveis SOCKS5H</option><option value="proxychains">proxychains-ng</option></>}</select></label><label>Programa<input value={program} onChange={event => setProgram(event.target.value)} placeholder="curl, nmap, wget…" required /></label><label>Argumentos (um por linha)<textarea value={argumentsText} onChange={event => setArgumentsText(event.target.value)} placeholder="--proxy\nsocks5h://127.0.0.1:19090\nhttp://destino" rows={4} /></label><div className="panel-actions"><button className="button primary" type="submit">Gerar launcher</button>{typeof plan?.command === "string" && <button className="button secondary" type="button" onClick={() => void copyCommand()}>Copiar comando</button>}</div>{plan && <pre className="plan-preview">{JSON.stringify({ command: plan.command, environment: plan.environment, config_path: plan.config_path, limitations: plan.limitations }, null, 2)}</pre>}</form> : <Empty text="Inicie um contexto SOCKS ou roteado para gerar launchers" />}</section>;
}

function ContextPlanner({ profiles }: { profiles: Profile[] }) {
  const [name, setName] = useState("contexto-socks");
  const [transport, setTransport] = useState("socks");
  const [connectionId, setConnectionId] = useState(profiles[0]?.id ?? 0);
  const [networks, setNetworks] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => { if (!connectionId && profiles[0]) setConnectionId(profiles[0].id); }, [profiles, connectionId]);
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (transport === "socks" && !connectionId) { setMessage("Um contexto SOCKS exige uma conexão SSH."); return; }
    try {
      const context = await request<Json>("/contexts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, transport, connection_id: connectionId || null, network_cidrs: networks.split(",").map(item => item.trim()).filter(Boolean) }) });
      setMessage(`Contexto #${String(context.id)} planejado. Revise e inicie na lista abaixo.`); window.dispatchEvent(new Event("ctfws:resources-changed"));
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  };
  return <section className="panel span-two"><PanelHeader title="Novo contexto de rede" subtitle="Planeje um SOCKS ou contexto roteado; iniciar continua sendo uma ação separada." /><form className="form-grid" onSubmit={(event) => void create(event)}><label>Nome<input value={name} onChange={event => setName(event.target.value)} required /></label><label>Transporte<select value={transport} onChange={event => setTransport(event.target.value)}><option value="socks">SOCKS via SSH</option><option value="routed">Roteado (Ligolo-ng experimental)</option></select></label><label>Conexão SSH{profiles.length ? <select value={connectionId} onChange={event => setConnectionId(Number(event.target.value))}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.user}@{profile.host}</option>)}</select> : <input value="Nenhuma conexão salva" disabled />}</label><label>Redes (CIDRs separados por vírgula)<input value={networks} onChange={event => setNetworks(event.target.value)} placeholder="10.10.0.0/16, 172.16.20.0/24" /></label><div className="panel-actions"><button className="button primary" type="submit">Planejar contexto</button>{message && <span className="notice-message inline-notice">{message}</span>}</div></form></section>;
}

function Tools({ profiles, setNotice }: { profiles: Profile[]; setNotice: (value: string) => void }) {
  const [tools, setTools] = useState<Json[]>([]);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [architecture, setArchitecture] = useState("unknown");
  const [version, setVersion] = useState("");
  const [remoteProfile, setRemoteProfile] = useState(profiles[0]?.id ?? 0);
  const [remoteDirectory, setRemoteDirectory] = useState("/tmp/ctfws-tools");
  const [conflict, setConflict] = useState("cancel");
  const load = () => void request<Json[]>("/tools").then(setTools).catch(() => setTools([]));
  useEffect(load, []);
  useEffect(() => { if (!remoteProfile && profiles[0]) setRemoteProfile(profiles[0].id); }, [profiles, remoteProfile]);

  const register = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await request<Json>("/tools", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, path, architecture, version: version || null }),
      });
      setName(""); setPath(""); setVersion(""); setNotice("Ferramenta cadastrada com hash SHA-256."); load();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };
  const transfer = async (tool: Json) => {
    if (!remoteProfile) { setNotice("Salve uma conexão SSH antes de enviar uma ferramenta."); return; }
    const target = `${remoteDirectory.replace(/\/$/, "")}/${String(tool.name)}`;
    try {
      const job = await request<Json>(`/tools/${String(tool.id)}/transfer/${remoteProfile}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remote_path: target, conflict }),
      });
      setNotice(`Transferência enfileirada como tarefa #${String(job.job_id)}.`);
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };
  return <div className="content-grid"><section className="panel span-two"><PanelHeader title="Catálogo de ferramentas" subtitle="O motor calcula o hash; cadastrar ou transferir nunca executa o arquivo." /><form className="form-grid" onSubmit={(event) => void register(event)}><label>Nome<input value={name} onChange={event => setName(event.target.value)} placeholder="ncat-static" required /></label><label>Caminho no motor Kali<input value={path} onChange={event => setPath(event.target.value)} placeholder="/opt/tools/ncat" required /></label><label>Arquitetura<input value={architecture} onChange={event => setArchitecture(event.target.value)} placeholder="x86_64" /></label><label>Versão<input value={version} onChange={event => setVersion(event.target.value)} placeholder="opcional" /></label><div className="panel-actions"><button className="button primary" type="submit">Cadastrar e calcular hash</button></div></form></section><section className="panel span-two"><PanelHeader title="Enviar para máquina remota" subtitle="Selecione a conexão e o diretório; cada envio gera um job auditável." />{profiles.length ? <div className="form-grid"><label>Conexão<select value={remoteProfile} onChange={event => setRemoteProfile(Number(event.target.value))}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.user}@{profile.host}</option>)}</select></label><label>Diretório remoto<input value={remoteDirectory} onChange={event => setRemoteDirectory(event.target.value)} /></label><label>Conflito de destino<select value={conflict} onChange={event => setConflict(event.target.value)}><option value="cancel">Cancelar e pedir decisão</option><option value="keep_both">Manter ambos</option><option value="replace">Substituir explicitamente</option></select></label></div> : <Empty text="Salve uma conexão SSH para enviar ferramentas" />}{tools.length ? <div className="file-list">{tools.map(tool => <div className="list-row" key={String(tool.id)}><div className="row-icon">⚒</div><div className="row-main"><strong>{String(tool.name)}</strong><small>{String(tool.architecture)} · {String(tool.version ?? "versão não informada")} · SHA-256 {String(tool.sha256).slice(0, 16)}…</small></div>{profiles.length && <button className="button small secondary" onClick={() => void transfer(tool)}>Enviar sem executar</button>}</div>)}</div> : <Empty text="Nenhuma ferramenta cadastrada" />}</section></div>;
}

function Activity() {
  const [jobs, setJobs] = useState<Json[]>([]);
  const [audit, setAudit] = useState<Json[]>([]);
  const [collections, setCollections] = useState<Json[]>([]);
  const [resumePlan, setResumePlan] = useState<Json[]>([]);
  const [selectedResume, setSelectedResume] = useState<string[]>([]);
  const [resumeMessage, setResumeMessage] = useState("");
  const resumeInitialized = useRef(false);
  const load = useCallback(async () => {
    try {
      const [nextJobs, nextAudit, nextResume, nextCollections] = await Promise.all([
        request<Json[]>("/jobs"),
        request<Json[]>("/audit"),
        request<Json[]>("/resume"),
        request<Json[]>("/collections?limit=30"),
      ]);
      setJobs(nextJobs);
      setAudit(nextAudit);
      setCollections(nextCollections);
      setResumePlan(nextResume);
      const references = nextResume.map(item => String(item.resource_ref ?? ""));
      setSelectedResume(current => {
        if (!resumeInitialized.current) {
          resumeInitialized.current = true;
          return references;
        }
        return current.filter(reference => references.includes(reference));
      });
    } catch {
      setJobs([]); setAudit([]); setCollections([]); setResumePlan([]);
    }
  }, []);
  useEffect(() => {
    void load();
    const refreshActivity = () => void load();
    window.addEventListener("ctfws:activity-refresh", refreshActivity);
    const timer = window.setInterval(() => void load(), 1000);
    return () => {
      window.removeEventListener("ctfws:activity-refresh", refreshActivity);
      window.clearInterval(timer);
    };
  }, [load]);
  const applyResume = async () => {
    try {
      const result = await request<Json[]>("/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resource_refs: selectedResume }),
      });
      setResumeMessage(`${result.length} recurso(s) processado(s). Nenhum comando foi repetido automaticamente.`);
      void load();
    } catch (error) { setResumeMessage(error instanceof Error ? error.message : String(error)); }
  };
  const toggleResume = (reference: string) => setSelectedResume(current => current.includes(reference) ? current.filter(item => item !== reference) : [...current, reference]);
  return <div className="content-grid"><section className="panel span-two"><PanelHeader title="Retomar ambiente" subtitle="A retomada exige uma escolha explícita e não reenvia comandos de terminal" />{resumePlan.length ? <><div className="resume-list">{resumePlan.map(item => { const reference = String(item.resource_ref ?? ""); return <label className="resume-item" key={reference}><input type="checkbox" checked={selectedResume.includes(reference)} onChange={() => toggleResume(reference)} /><span><strong>{String(item.name)}</strong><small>{reference} · {String(item.action)} · {String(item.reason)}</small></span></label>; })}</div><div className="panel-actions"><button className="button primary" disabled={!selectedResume.length} onClick={() => void applyResume()}>Retomar selecionados</button>{resumeMessage && <span className="notice-message inline-notice">{resumeMessage}</span>}</div></> : <Empty text="Nenhum recurso precisa de retomada" />}</section><section className="panel span-two"><PanelHeader title="Jobs" subtitle="O motor atualiza o andamento automaticamente; abra uma tarefa para ver o resultado." />{jobs.length ? jobs.map(job => { const result = asRecord(job.result); const collectionId = Number(result.collection_id ?? 0); const collection = collections.find(item => Number(item.id) === collectionId); return <div className="job-card" key={String(job.id)}><div className="list-row"><div className="row-icon">◷</div><div className="row-main"><strong>{String(job.kind)}</strong><small>Tarefa #{String(job.id)} · {String(job.current_step ?? "aguardando")}</small></div><span className={`state ${String(job.status)}`}>{String(job.status)} · {String(job.progress)}%</span></div>{typeof job.error_message === "string" && <div className="error-message job-error">{String(job.error_code ?? "erro")}: {String(job.error_message)}</div>}{collection && <InspectionDetails collection={collection} />}{!collection && Object.keys(result).length > 0 && <details className="job-details"><summary>Ver resultado da tarefa</summary><pre className="inspection-output">{JSON.stringify(result, null, 2)}</pre></details>}</div>; }) : <Empty text="Nenhuma tarefa recente" />}</section><section className="panel span-two"><PanelHeader title="Histórico de inspeções" subtitle="Saídas brutas, etapas e snapshot ficam preservados por coleta." />{collections.length ? collections.slice(0, 12).map(collection => <InspectionDetails collection={collection} key={String(collection.id)} />) : <Empty text="Nenhuma inspeção registrada" />}</section><section className="panel span-two"><PanelHeader title="Auditoria" subtitle="Quem solicitou cada mutação e qual foi o resultado" />{audit.length ? audit.slice(0, 30).map(item => <div className="list-row" key={String(item.id)}><div className="row-icon">◉</div><div className="row-main"><strong>{String(item.action)}</strong><small>{String(item.actor)} · {String(item.resource_type)} · {String(item.created_at)}</small></div><span className={`state ${String(item.result)}`}>{String(item.result)}</span></div>) : <Empty text="Nenhum evento de auditoria" />}</section></div>;
}

function asRecord(value: unknown): Json {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Json : {};
}

function InspectionDetails({ collection }: { collection: Json }) {
  const result = asRecord(collection.result);
  const steps = asRecord(result.steps);
  const outputs = asRecord(result.outputs);
  const failedSteps = Array.isArray(result.failed_steps) ? result.failed_steps.map(String) : [];
  return <details className="job-details"><summary>Coleta #{String(collection.id)} · {String(collection.status)} · {String(collection.updated_at)}</summary><div className="inspection-summary"><span>Host <strong>{String(result.host_name ?? collection.host_id ?? "não identificado")}</strong></span><span>Snapshot <strong>#{String(collection.snapshot_id ?? result.snapshot_id ?? "—")}</strong></span><span>Etapas <strong>{String(collection.completed_steps)}/{String(collection.total_steps)}</strong></span><span>Falhas <strong>{String(failedSteps.length)}</strong></span></div>{failedSteps.length > 0 && <div className="error-message">Etapas com falha: {failedSteps.join(", ")}</div>}<div className="inspection-steps">{Object.entries(steps).map(([name, detail]) => { const step = asRecord(detail); return <div className="inspection-step" key={name}><span className={`state ${String(step.status)}`}>{String(step.status)}</span><span><strong>{name}</strong><small>{String(step.command ?? "comando fixo")} · {String(step.bytes ?? 0)} bytes{step.error ? ` · ${String(step.error)}` : ""}</small></span></div>; })}</div>{Object.entries(outputs).map(([name, output]) => <details className="output-block" key={name}><summary>Saída: {name}</summary><pre className="inspection-output">{String(output)}</pre></details>)}</details>;
}

function HostKeyPrompt({ request, onCancel, onConfirm }: { request: HostKeyRequest; onCancel: () => void; onConfirm: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const hostKey = request.hostKey;
  const confirm = async () => {
    setBusy(true);
    try { await onConfirm(); } finally { setBusy(false); }
  };
  return <div className="login-overlay"><section className="panel login-panel host-key-prompt" role="dialog" aria-modal="true" aria-labelledby="host-key-title"><div className="eyebrow">VERIFICAÇÃO DE IDENTIDADE</div><h2 id="host-key-title">Servidor SSH desconhecido</h2><p>O servidor respondeu com uma chave que ainda não está na lista de confiança do Conduit.</p><div className="host-key-details"><div><span>Destino</span><strong>{String(hostKey.host ?? hostKey.address)}:{String(hostKey.port ?? 22)}</strong></div><div><span>Tipo de chave</span><strong>{String(hostKey.algorithm ?? "desconhecido")}</strong></div><div><span>Fingerprint SHA-256</span><code>{String(hostKey.fingerprint ?? "não disponível")}</code></div></div><p className="host-key-warning">Compare esta fingerprint com uma fonte confiável do ambiente antes de continuar. Confiar na chave sem conferir pode permitir um ataque de intermediário.</p><div className="panel-actions"><button className="button secondary" disabled={busy} onClick={onCancel}>Não confiar</button><button className="button primary" disabled={busy || !hostKey.fingerprint} onClick={() => void confirm()}>{busy ? "Revalidando…" : "Confiar e continuar"}</button></div></section></div>;
}

function AccountManagement({ setNotice, onLogout }: { setNotice: (value: string) => void; onLogout: () => Promise<void> }) {
  const [accounts, setAccounts] = useState<Json[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [role, setRole] = useState("operator");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { setAccounts(await requestV2<Json[]>("/auth/accounts")); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  useEffect(() => { void load(); }, []);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    const normalizedUsername = username.trim();
    if (!normalizedUsername) { setError("Informe um usuário."); return; }
    if (password.length < 12) { setError("A senha precisa ter pelo menos 12 caracteres."); return; }
    if (password !== confirmation) { setError("A confirmação da senha não corresponde."); return; }
    setBusy(true);
    try {
      await requestV2<Json>("/auth/accounts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: normalizedUsername, password, role }) });
      setUsername(""); setPassword(""); setConfirmation(""); setRole("operator");
      setNotice(`Conta ${normalizedUsername} criada com sucesso.`);
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  return <div className="content-grid"><section className="panel"><PanelHeader title="Contas locais" subtitle="Somente administradores podem criar contas. Senhas nunca são exibidas." />{accounts.length ? accounts.map(account => <div className="list-row" key={String(account.username)}><div className="row-icon">●</div><div className="row-main"><strong>{String(account.username)}</strong><small>Conta local</small></div><span className="state ready">{String(account.role)}</span></div>) : <Empty text="Nenhuma conta local criada" />}<div className="panel-actions"><button className="button secondary" onClick={() => void onLogout()}>Sair</button></div></section><section className="panel"><PanelHeader title="Criar conta" subtitle="Use uma senha forte com pelo menos 12 caracteres." /><form className="form-grid account-form" onSubmit={(event) => void submit(event)}><label>Usuário<input value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" placeholder="operador" required /></label><label>Papel<select value={role} onChange={event => setRole(event.target.value)}><option value="operator">Operador</option><option value="observer">Observador</option><option value="admin">Administrador</option></select></label><label>Senha<input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="new-password" minLength={12} required /></label><label>Confirmar senha<input type="password" value={confirmation} onChange={event => setConfirmation(event.target.value)} autoComplete="new-password" minLength={12} required /></label><div className="panel-actions"><button className="button primary" type="submit" disabled={busy}>{busy ? "Criando…" : "Criar conta"}</button></div></form>{error && <div className="error-message">{error}</div>}</section></div>;
}

function LoginPanel({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [bootstrap, setBootstrap] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [oidcToken, setOidcToken] = useState("");
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    void fetch("/api/v2/auth/oidc").then((response) => response.json()).then((body: Json) => {
      setOidcEnabled(Boolean(body.enabled));
    }).catch(() => undefined);
  }, []);
  const login = async (payload: Json) => {
    setError("");
    const response = await fetch("/api/v2/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(String((body as Json).detail ?? response.statusText));
    localStorage.setItem(tokenKey, String((body as Json).token));
    onLoggedIn();
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await login(
        bootstrap ? { bootstrap_token: bootstrap } : oidcToken ? { oidc_token: oidcToken } : { username, password },
      );
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="login-overlay"><section className="panel login-panel"><div className="eyebrow">SESSÃO PROTEGIDA</div><h2>Entrar no Conduit</h2><p>Use uma conta local, o código de bootstrap do primeiro acesso ou o token emitido pelo IdP.</p><div className="login-help"><strong>Primeiro acesso?</strong><span>Use o código mostrado pelo instalador no campo de bootstrap. Depois de entrar, abra <b>Configurações → Contas</b> para criar os usuários da equipe.</span></div><form onSubmit={(event) => void submit(event)}><label>Código de bootstrap<input value={bootstrap} onChange={(event) => setBootstrap(event.target.value)} placeholder="opcional" autoComplete="one-time-code" /></label><div className="login-separator">ou conta local</div><label>Usuário<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label><label>Senha<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>{oidcEnabled && <><div className="login-separator">ou OIDC</div><label>Token emitido pelo IdP<input value={oidcToken} onChange={(event) => setOidcToken(event.target.value)} placeholder="JWT temporário" /></label></>}<button className="button primary" type="submit">Entrar</button></form>{error && <div className="error-message">{error}</div>}</section></div>;
}

function Files({ profiles, setNotice }: { profiles: Profile[]; setNotice: (value: string) => void }) {
  const [files, setFiles] = useState<Json[]>([]);
  const [remoteFiles, setRemoteFiles] = useState<Json[]>([]);
  const [remoteProfile, setRemoteProfile] = useState<number>(profiles[0]?.id ?? 0);
  const [remotePath, setRemotePath] = useState(".");
  const [conflict, setConflict] = useState("cancel");
  const [uploading, setUploading] = useState(false);
  const load = () => void request<Json[]>("/files?relative=loot/inbox").then(setFiles).catch(() => setFiles([]));
  useEffect(load, []);
  useEffect(() => { if (!remoteProfile && profiles[0]) setRemoteProfile(profiles[0].id); }, [profiles, remoteProfile]);
  const uploadFiles = async (selectedFiles: FileList | File[]) => {
    const items = Array.from(selectedFiles);
    if (!items.length) return;
    setUploading(true);
    let completed = 0;
    try {
      for (const file of items) {
        const form = new FormData(); form.append("file", file);
        const response = await fetch(`${api}/files/upload?destination=loot/inbox&conflict=${encodeURIComponent(conflict)}`, { method: "POST", body: form, headers: (() => { const headers = new Headers(); const token = localStorage.getItem(tokenKey); if (token) headers.set("Authorization", `Bearer ${token}`); return headers; })() });
        const body = await response.json();
        if (!response.ok) throw new Error(String(body.detail ?? response.statusText));
        completed += 1;
        setNotice(`Upload ${completed}/${items.length}: ${file.name}`);
      }
      setNotice(`${completed} arquivo(s) enviado(s) e registrado(s).`);
      load();
    } catch (error) {
      setNotice(`Upload interrompido após ${completed}/${items.length}: ${error instanceof Error ? error.message : String(error)}`);
    } finally { setUploading(false); }
  };
  const registerEvidence = async (file: Json) => {
    const headers = new Headers({ "Content-Type": "application/json" }); const token = localStorage.getItem(tokenKey); if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${api}/evidence`, {
      method: "POST", headers,
      body: JSON.stringify({ type: "file", description: `Arquivo ${String(file.name)}`, path: String(file.path) }),
    });
    const body = await response.json();
    setNotice(response.ok ? `Evidência #${String(body.id)} registrada.` : String(body.detail));
  };
  const browseRemote = async () => {
    if (!remoteProfile) return;
    try {
      const result = await request<Json[]>(`/connections/${remoteProfile}/files?remote_path=${encodeURIComponent(remotePath)}`);
      setRemoteFiles(result); setNotice(`Máquina remota: ${result.length} entradas encontradas.`);
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };
  const downloadRemote = async (file: Json) => {
    if (!remoteProfile) return;
    try {
      const result = await request<Json>(`/connections/${remoteProfile}/download`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remote_path: String(file.path), local_path: `loot/inbox/${String(file.name)}`, conflict }),
      });
      setNotice(`Download concluído: ${String(result.sha256 ?? "verificação pendente")}`); load();
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };
  const uploadRemote = async (file: Json) => {
    if (!remoteProfile) return;
    const remoteTarget = `${remotePath.replace(/\/$/, "")}/${String(file.name)}`;
    try {
      const result = await request<Json>(`/connections/${remoteProfile}/upload`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ local_path: String(file.path), remote_path: remoteTarget, conflict }),
      });
      setNotice(`Upload remoto concluído: ${String(result.sha256 ?? "verificação pendente")}`);
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
  };
  return <div className="content-grid"><section className="panel"><PanelHeader title="Workspace / Kali" subtitle="Arquivos locais do motor" /><label>Conflito de destino<select value={conflict} onChange={event => setConflict(event.target.value)}><option value="cancel">Cancelar e pedir decisão</option><option value="keep_both">Manter ambos</option><option value="replace">Substituir explicitamente</option></select></label><label className="drop-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void uploadFiles(event.dataTransfer.files); }}><input type="file" multiple disabled={uploading} onChange={(event) => { if (event.target.files) void uploadFiles(event.target.files); event.target.value = ""; }} /><span className="drop-icon">↑</span><strong>{uploading ? "Enviando arquivos..." : "Arraste ou selecione arquivos"}</strong><small>Seleção múltipla, hash SHA-256 e finalização atômica.</small></label><div className="file-list">{files.map(file => <div className="list-row" key={String(file.path)}><div className="row-icon">▧</div><div className="row-main"><strong>{String(file.name)}</strong><small>{String(file.path)}</small></div><span className="muted">{String(file.size ?? "-")} bytes</span><button className="button small secondary" onClick={() => void registerEvidence(file)}>Registrar evidência</button>{remoteProfile > 0 && <button className="button small secondary" onClick={() => void uploadRemote(file)}>Enviar</button>}</div>)}</div></section><section className="panel"><PanelHeader title="Máquina remota" subtitle="Navegação via SFTP, sem shell remoto" />{profiles.length ? <><label>Conexão<select value={remoteProfile} onChange={event => setRemoteProfile(Number(event.target.value))}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.user}@{profile.host}</option>)}</select></label><label>Caminho remoto<input value={remotePath} onChange={event => setRemotePath(event.target.value)} onKeyDown={event => { if (event.key === "Enter") void browseRemote(); }} /></label><p className="muted">A política de conflito selecionada à esquerda também vale para downloads e uploads SFTP.</p><button className="button primary" onClick={() => void browseRemote()}>Navegar</button><div className="file-list">{remoteFiles.map(file => <div className="list-row" key={String(file.path)}><div className="row-icon">⇣</div><div className="row-main"><strong>{String(file.name)}</strong><small>{String(file.path)}</small></div><button className="button small secondary" onClick={() => void downloadRemote(file)}>Baixar</button></div>)}</div></> : <Empty text="Salve uma conexão SSH para navegar" />}</section></div>;
}

function TerminalView({ terminals, selected, workspaceId, onSelect, onLocal, onNewRemote, onClose, onShare, onRename }: { terminals: TerminalRow[]; selected: number | null; workspaceId: number | null; onSelect: (id: number | null) => void; onLocal: () => void; onNewRemote: (connectionId: number) => void; onClose: (id: number) => void; onShare: (id: number, shared: boolean) => void; onRename: (id: number, name: string) => void }) {
  const [attached, setAttached] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [splitId, setSplitId] = useState<number | null>(null);
  const lastSequences = useRef<Record<number, number>>({});
  useEffect(() => { if (selected) { setAttached(true); setSearchTerm(""); setRenameOpen(false); setSplitId(null); } }, [selected]);
  const row = terminals.find(item => item.id === selected);
  const splitRow = terminals.find(item => item.id === splitId);
  const splitCandidates = terminals.filter(item => item.id !== selected);
  const terminalAvailable = Boolean(row && ["active", "starting"].includes(row.status) && row.runtime_available !== false);
  const recordSequence = useCallback((sequence: number) => { if (selected) lastSequences.current[selected] = sequence; }, [selected]);
  const submitRename = () => { if (selected && renameValue.trim()) { onRename(selected, renameValue.trim()); setRenameOpen(false); } };
  const terminalViews = selected && attached && terminalAvailable ? <>{splitId && splitRow ? <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "10px", minHeight: "450px" }}><XtermTerminal terminalId={selected} workspaceId={workspaceId} afterSequence={lastSequences.current[selected] ?? 0} searchTerm={searchTerm} onSequence={recordSequence} /><XtermTerminal terminalId={splitId} workspaceId={workspaceId} afterSequence={lastSequences.current[splitId] ?? 0} searchTerm={searchTerm} readOnly onSequence={sequence => { lastSequences.current[splitId] = sequence; }} /></div> : <XtermTerminal terminalId={selected} workspaceId={workspaceId} afterSequence={lastSequences.current[selected] ?? 0} searchTerm={searchTerm} onSequence={recordSequence} />}</> : selected ? <div className="empty large"><div className="empty-icon">◌</div><strong>{row?.status === "closed" ? "Terminal encerrado" : "Sessão não está ativa"}</strong><span>{String(row?.availability_reason ?? "O motor não possui esta sessão em execução.")}</span>{row?.reconnectable && typeof row.connection_id === "number" && <button className="button primary" onClick={() => onNewRemote(row.connection_id as number)}>Abrir novo terminal nesta máquina</button>}</div> : <div className="empty large"><div className="empty-icon">⌘</div><strong>Abra um terminal para começar</strong></div>;
  return <div className="terminal-layout"><section className="panel terminal-tabs"><PanelHeader title="Terminais" action={<button className="text-button" onClick={onLocal}>+ Novo terminal</button>} />{terminals.map(item => <button className={selected === item.id ? "terminal-tab selected" : "terminal-tab"} key={item.id} onClick={() => onSelect(item.id)}><span>⌘</span><span><strong>{item.name ?? item.context_label}</strong><small>{item.context_label} · #{item.id} · {item.status} · {item.sharing === "shared" ? "compartilhado" : "privado"}</small></span></button>)}{!terminals.length && <Empty text="Nenhum terminal aberto" />}</section><section className="panel terminal-panel"><PanelHeader title={selected ? (row?.name ? String(row.name) : `Terminal #${selected}`) : "Selecione um terminal"} subtitle={row ? `${row.context_label} · ${row.sharing === "shared" ? "visualização compartilhada" : "visualização privada"} · saída não é gravada em disco por padrão` : "Escolha um terminal na lista"} action={selected ? <div className="panel-actions terminal-actions"><button className="button small secondary" onClick={() => onSelect(null)}>Ocultar</button><button className="button small secondary" onClick={() => setAttached(value => !value)}>{attached ? "Desconectar visualização" : "Reconectar visualização"}</button>{splitCandidates.length > 0 && <><button className="button small secondary" onClick={() => setSplitId(splitId ? null : splitCandidates[0].id)}>{splitId ? "Fechar divisão" : "Dividir tela"}</button>{!splitId && <select aria-label="Segundo terminal da divisão" value="" onChange={event => setSplitId(Number(event.target.value))}><option value="">Escolher segundo terminal…</option>{splitCandidates.map(item => <option key={item.id} value={item.id}>{item.name ?? item.context_label} · #{item.id}</option>)}</select>}</>}{row && <button className="button small secondary" onClick={() => { setRenameValue(String(row.name ?? "")); setRenameOpen(true); }}>{"Renomear"}</button>}{row && <button className="button small secondary" onClick={() => onShare(selected, row.sharing !== "shared")}>{row.sharing === "shared" ? "Tornar privado" : "Compartilhar visualização"}</button>}<button className="button small" onClick={() => onClose(selected)}>Encerrar</button></div> : undefined} />{selected && <div className="terminal-toolbar">{renameOpen && <><input aria-label="Novo nome do terminal" value={renameValue} onChange={event => setRenameValue(event.target.value)} onKeyDown={event => { if (event.key === "Enter") submitRename(); if (event.key === "Escape") setRenameOpen(false); }} /><button className="button small primary" onClick={submitRename}>Salvar nome</button><button className="button small secondary" onClick={() => setRenameOpen(false)}>Cancelar</button></>}<input aria-label="Buscar na saída" className="terminal-search" value={searchTerm} onChange={event => setSearchTerm(event.target.value)} placeholder="Buscar na saída…" /></div>}{terminalViews}</section></div>;
}

 function XtermTerminal({ terminalId, workspaceId, afterSequence, searchTerm, readOnly = false, onSequence }: { terminalId: number; workspaceId: number | null; afterSequence: number; searchTerm: string; readOnly?: boolean; onSequence: (sequence: number) => void }) {
   const ref = useRef<HTMLDivElement>(null);
   const terminalRef = useRef<Terminal | null>(null);
   const onSequenceRef = useRef(onSequence);
   const initialSequenceRef = useRef(afterSequence);

   useEffect(() => {
     onSequenceRef.current = onSequence;
   }, [onSequence]);

   useEffect(() => {
     if (!ref.current) return;
     const terminal = new Terminal({
       convertEol: true,
       cursorBlink: !readOnly,
       disableStdin: readOnly,
       fontSize: 13,
       theme: { background: "#050912", foreground: "#c7f9dd" },
     });
     terminalRef.current = terminal;
     const fit = new FitAddon();
     terminal.loadAddon(fit);
     terminal.open(ref.current);
     fit.fit();
     const host = ref.current;
     const protocol = location.protocol === "https:" ? "wss:" : "ws:";
     const token = localStorage.getItem(tokenKey);
     const basePath = workspaceId
       ? `/api/v2/workspaces/${workspaceId}/terminals/${terminalId}/stream`
       : `${api}/terminals/${terminalId}/stream`;
     const streamPath = `${basePath}?after_sequence=${initialSequenceRef.current}${readOnly ? "&readonly=1" : ""}`;
     const socket = new WebSocket(
       `${protocol}//${location.host}${streamPath}`,
       token ? ["ctfws", token] : undefined,
     );
     socket.binaryType = "arraybuffer";
     const pendingInput: string[] = [];
     let pendingInputLength = 0;
     let disposed = false;
     let flushTimer: number | null = null;
     let controlReady = workspaceId === null;
     let hasControl = !readOnly;
     const maxPendingInput = 128 * 1024;
     const maxSocketBuffer = 1024 * 1024;
     const writeNotice = (message: string) => terminal.write(`\r\n\x1b[33m[Conduit] ${message}\x1b[0m\r\n`);
     const flushInput = () => {
       if (socket.readyState !== WebSocket.OPEN || !controlReady || !hasControl) return;
       while (pendingInput.length && socket.bufferedAmount < maxSocketBuffer) {
         const data = pendingInput[0];
         if (!data) { pendingInput.shift(); continue; }
         try { socket.send(data); }
         catch { break; }
         pendingInput.shift();
         pendingInputLength -= data.length;
       }
       if (pendingInput.length && flushTimer === null) {
         flushTimer = window.setTimeout(() => {
           flushTimer = null;
           flushInput();
         }, 25);
       }
     };
     const sendInput = (data: string) => {
       if (socket.readyState === WebSocket.OPEN && controlReady && hasControl && socket.bufferedAmount < maxSocketBuffer) {
         try { socket.send(data); } catch { pendingInput.push(data); pendingInputLength += data.length; }
         flushInput();
         return;
       }
       if (pendingInputLength + data.length > maxPendingInput) {
         writeNotice("A conexão ainda está iniciando; entrada excedente foi descartada.");
         return;
       }
       pendingInput.push(data);
       pendingInputLength += data.length;
     };
     const resize = () => {
       fit.fit();
       if (socket.readyState === WebSocket.OPEN && (!workspaceId || hasControl)) {
         try {
           socket.send(JSON.stringify({ action: "resize", columns: terminal.cols, rows: terminal.rows }));
         } catch { writeNotice("Não foi possível atualizar o tamanho do terminal."); }
       }
     };
     socket.onopen = () => {
       terminal.focus();
       if (!workspaceId) flushInput();
       resize();
     };
     socket.onmessage = event => {
       if (typeof event.data === "string") {
         try {
           const metadata = JSON.parse(event.data) as Json;
           if (typeof metadata.sequence === "number") onSequenceRef.current(metadata.sequence);
           if (metadata.gap) terminal.write("\r\n[aviso] A visualização retomou com uma lacuna de saída.\r\n");
           if (metadata.type === "ready") {
             controlReady = true;
             hasControl = metadata.control === true && !readOnly;
             flushInput();
           } else if (metadata.type === "control") {
             hasControl = metadata.granted === true;
             flushInput();
           } else if (metadata.type === "error" && metadata.code === "control_held") {
             hasControl = false;
           }
           if (metadata.type === "error") {
             const code = String(metadata.code ?? "terminal_error");
             const message = typeof metadata.message === "string" ? metadata.message :
               code === "control_held" ? "Outro observador controla a entrada deste terminal." :
               "A operação do terminal falhou.";
             writeNotice(`${code}: ${message}`);
           } else if (metadata.type === "exit") {
             writeNotice(`O processo terminou${metadata.code !== null && metadata.code !== undefined ? ` (código ${String(metadata.code)})` : ""}.`);
           } else if (metadata.type === "ready" && metadata.control === false && !readOnly) {
             writeNotice("Visualização conectada sem controle de entrada.");
           }
         } catch {
           writeNotice("Recebida uma mensagem inválida do motor.");
         }
         return;
       }
       if (event.data instanceof ArrayBuffer) terminal.write(new Uint8Array(event.data));
     };
     socket.onerror = () => {
       if (!disposed) writeNotice("Não foi possível manter a conexão do terminal.");
     };
     socket.onclose = () => {
       if (!disposed) writeNotice("A visualização do terminal foi desconectada.");
     };
     const inputSubscription = !readOnly ? terminal.onData(sendInput) : null;
     const focus = () => terminal.focus();
     host.addEventListener("click", focus);
     window.addEventListener("resize", resize);
     const resizeObserver = new ResizeObserver(resize);
     resizeObserver.observe(host);
     resize();
     return () => {
       disposed = true;
       window.removeEventListener("resize", resize);
       resizeObserver.disconnect();
       host.removeEventListener("click", focus);
       inputSubscription?.dispose();
       if (flushTimer !== null) window.clearTimeout(flushTimer);
       pendingInput.length = 0;
       socket.close();
       terminal.dispose();
       terminalRef.current = null;
     };
   }, [terminalId, workspaceId, readOnly]);

   useEffect(() => {
     const terminal = terminalRef.current;
     const needle = searchTerm.trim().toLocaleLowerCase();
     if (!terminal || !needle) return;
     for (let index = 0; index < terminal.buffer.active.length; index += 1) {
       const line = terminal.buffer.active.getLine(index)?.translateToString() ?? "";
       if (line.toLocaleLowerCase().includes(needle)) {
         terminal.scrollToLine(index);
         break;
       }
     }
   }, [searchTerm]);

   return <div className="xterm-host" ref={ref} />;
 }

function ConnectionPanel({ onClose, onSaved }: { onClose: () => void; onSaved: (id: number, password?: string, inspectAfterSave?: boolean, credentialKind?: "password" | "key_passphrase") => void }) { const [name, setName] = useState(""); const [ssh, setSsh] = useState("ssh kali@10.0.0.5 -p 22"); const [temporaryPassword, setTemporaryPassword] = useState(""); const [authMethod, setAuthMethod] = useState("agent_or_key"); const [parsed, setParsed] = useState<Json | null>(null); const [error, setError] = useState(""); const jumpTargets = Array.isArray(parsed?.jump_targets) ? parsed.jump_targets as Json[] : []; const unresolvedJumps = Array.isArray(parsed?.unresolved_jump_targets) ? parsed.unresolved_jump_targets as Json[] : []; const credentialKind = authMethod === "key_passphrase" ? "key_passphrase" as const : "password" as const; const parse = async () => { try { setError(""); setParsed(await request<Json>("/connections/parse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ssh }) })); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }; const save = async (inspectAfterSave: boolean) => { if (!parsed || unresolvedJumps.length) return; try { const created = await request<Json>("/connections", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, host: parsed.host, user: parsed.user, port: parsed.port, identity_file: parsed.identity_file, known_hosts_file: parsed.known_hosts_file, auth_method: authMethod, jump_profile_ids: Array.isArray(parsed.jump_profile_ids) ? parsed.jump_profile_ids : [] }) }); const password = temporaryPassword; setTemporaryPassword(""); onSaved(Number(created.id), password || undefined, inspectAfterSave, password ? credentialKind : undefined); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }; const canSave = Boolean(parsed && name && !unresolvedJumps.length); return <section className="panel connection-panel"><div className="panel-heading"><div><h2>Conectar e inspecionar</h2><p>O comando é interpretado como dados; não executamos texto colado.</p></div><button className="icon-button" onClick={onClose}>×</button></div><div className="form-grid"><label>Nome da conexão<input value={name} onChange={event => setName(event.target.value)} placeholder="jump-01" /></label><label>Comando SSH<input value={ssh} onChange={event => setSsh(event.target.value)} /></label></div><div className="form-grid"><label>Método de autenticação<select value={authMethod} onChange={event => setAuthMethod(event.target.value)}><option value="agent_or_key">SSH agent ou chave sem passphrase</option><option value="password">Senha SSH temporária</option><option value="key_passphrase">Passphrase da chave temporária</option></select></label><label>Credencial temporária (não salva)<input type="password" value={temporaryPassword} onChange={event => setTemporaryPassword(event.target.value)} placeholder={authMethod === "key_passphrase" ? "passphrase da chave, usada só nesta tentativa" : "opcional; use agent/chave por padrão"} autoComplete="new-password" /></label></div><div className="panel-actions"><button className="button secondary" onClick={() => void parse()}>Validar comando</button>{parsed && <div className="parsed-preview"><span>Destino identificado</span><strong>{String(parsed.user)}@{String(parsed.host)}:{String(parsed.port)}</strong>{jumpTargets.length > 0 && <small>Saltos: {jumpTargets.map(item => `${String(item.user ?? "")}${item.user ? "@" : ""}${String(item.host)}:${String(item.port)}`).join(", ")}</small>}{unresolvedJumps.length > 0 && <small className="error-message">Cadastre os saltos ProxyJump antes de salvar.</small>}</div>}<button className="button secondary" disabled={!canSave} onClick={() => void save(false)}>Salvar perfil</button><button className="button primary" disabled={!canSave} onClick={() => void save(true)}>Conectar e inspecionar</button></div>{error && <div className="error-message">{error}</div>}</section>; }

function CommandPalette({ onClose, onNavigate, onConnect, onRefresh }: { onClose: () => void; onNavigate: (view: View) => void; onConnect: () => void; onRefresh: () => void }) {
  const actions: { label: string; shortcut?: string; run: () => void }[] = [
    { label: "Conectar e inspecionar máquina", run: onConnect },
    { label: "Abrir visão geral", run: () => onNavigate("overview") },
    { label: "Abrir máquinas", run: () => onNavigate("machines") },
    { label: "Abrir terminais", run: () => onNavigate("terminals") },
    { label: "Abrir túneis e contextos", run: () => onNavigate("tunnels") },
    { label: "Abrir arquivos e evidências", run: () => onNavigate("files") },
    { label: "Abrir ferramentas", run: () => onNavigate("tools") },
    { label: "Abrir atividade", run: () => onNavigate("activity") },
    { label: "Atualizar workspace", shortcut: "R", run: onRefresh },
  ];
  return <div className="palette-backdrop" role="presentation" onMouseDown={onClose}><section className="palette panel" role="dialog" aria-modal="true" aria-label="Command palette" onMouseDown={event => event.stopPropagation()}><div className="eyebrow">ATALHOS</div><h2>O que você quer fazer?</h2><div className="palette-list">{actions.map(action => <button key={action.label} className="palette-item" onClick={action.run}><span>{action.label}</span>{action.shortcut && <kbd>{action.shortcut}</kbd>}</button>)}</div><button className="text-button palette-close" onClick={onClose}>Esc para fechar</button></section></div>;
}

function PanelHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) { return <div className="panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>; }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div>; }

createRoot(document.getElementById("root")!).render(<App />);
