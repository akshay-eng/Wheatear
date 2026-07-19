/* App state: workspaces/projects in localStorage, toasts, theme, hash router. */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { buildReportMd } from "./lib/report.js";
import { uid } from "./lib/utils.js";

const STORE_KEY = "wheatear.console.v1";

/* ---------- persistence -------------------------------------------------- */

function loadInitial() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) { /* fresh start */ }
  const fresh = { workspaces: [] };
  seedIfRequested(fresh);
  return fresh;
}

/* ?demo seeds sample data so every screen has content to show. */
function seedIfRequested(state) {
  const qp = new URLSearchParams(location.search);
  if (!qp.has("demo") || state.workspaces.length) return;
  const ws = { id: "demows", name: "Platform Exit Q3", createdAt: Date.now() - 86400000 * 3, projects: [] };
  ws.projects.push({
    id: "p1", name: "watsonx Orchestrate → Copilot Studio", source: "orchestrate", target: "copilot-studio",
    sourceRef: "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df32…767e",
    workspaceId: "00000000-0000-0000-0000-000000000001",
    llm: { provider: "anthropic", env: "ANTHROPIC_API_KEY" },
    status: "attention", createdAt: Date.now() - 3600000 * 5,
    agents: [
      { agent: "Incident_Managementagent_0044s7", kind: "native", status: "review", artifact: "copilot-studio-migration/Incident_Managementagent_0044s7/agent.ir.yaml", stages: ["extract", "map", "translate", "validate", "export"], notes: ["2 connection references need credentials before import", "1 collaborator reference could not be auto-mapped"] },
      { agent: "Turbonomic_Alert_Handler_4400Gc", kind: "native", status: "done", artifact: "copilot-studio-migration/Turbonomic_Alert_Handler_4400Gc/agent.ir.yaml", stages: ["extract", "map", "translate", "validate", "export"], notes: [] },
      { agent: "Payroll_FAQ_Agent", kind: "native", status: "done", artifact: "copilot-studio-migration/Payroll_FAQ_Agent/agent.ir.yaml", stages: ["extract", "map", "translate", "validate", "export"], notes: [] },
    ],
    answers: [
      { q: "Where are your agents today?", a: "IBM watsonx Orchestrate" },
      { q: "Service instance URL", a: "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df32…767e" },
      { q: "IBM Cloud API key", a: "(saved in OS keychain — not recorded)" },
      { q: "Workspace ID", a: "00000000-0000-0000-0000-000000000001" },
      { q: "Which agents should be migrated?", a: "Incident_Managementagent_0044s7, Turbonomic_Alert_Handler_4400Gc, Payroll_FAQ_Agent" },
      { q: "Migrate to which platform?", a: "Microsoft Copilot Studio" },
      { q: "Which LLM translates instructions?", a: "Anthropic Claude (claude-sonnet-5)" },
      { q: "API key environment variable", a: "ANTHROPIC_API_KEY" },
      { q: "Project name", a: "watsonx Orchestrate → Copilot Studio" },
    ],
  });
  ws.projects.push({
    id: "p2", name: "Raw export — incident agents", source: "orchestrate", target: "export-only",
    sourceRef: "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df32…767e",
    workspaceId: "00000000-0000-0000-0000-000000000001", llm: null,
    status: "complete", createdAt: Date.now() - 86400000,
    agents: [
      { agent: "Incident_Managementagent_0044s7", kind: "native", status: "done", artifact: "orchestrate-exports/Incident_Managementagent_0044s7/agent.yaml", stages: ["export"], notes: [] },
      { agent: "Network_Diagnostics_Agent", kind: "native", status: "done", artifact: "orchestrate-exports/Network_Diagnostics_Agent/agent.yaml", stages: ["export"], notes: [] },
    ],
    answers: [
      { q: "Where are your agents today?", a: "IBM watsonx Orchestrate" },
      { q: "Service instance URL", a: "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df32…767e" },
      { q: "IBM Cloud API key", a: "(saved in OS keychain — not recorded)" },
      { q: "Workspace ID", a: "00000000-0000-0000-0000-000000000001" },
      { q: "Which agents should be migrated?", a: "Incident_Managementagent_0044s7, Network_Diagnostics_Agent" },
      { q: "Migrate to which platform?", a: "Export raw YAML to folder" },
      { q: "Which LLM translates instructions?", a: "Skipped — raw export uses no LLM" },
      { q: "Project name", a: "Raw export — incident agents" },
    ],
  });
  for (const p of ws.projects) {
    p.startedAt = p.createdAt;
    p.finishedAt = p.createdAt + 14000;
    p.report = buildReportMd(p, ws.name);
  }
  state.workspaces.push(ws);
}

/* ---------- store context ------------------------------------------------ */

const StoreCtx = createContext(null);

export function StoreProvider({ children }) {
  const [state, setState] = useState(loadInitial);
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* quota */ }
  }, [state]);

  const toast = useCallback((msg, kind = "ok") => {
    const id = uid();
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.map((x) => (x.id === id ? { ...x, leaving: true } : x))), 3000);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3260);
  }, []);

  const actions = {
    createWorkspace(name) {
      const w = { id: uid(), name, createdAt: Date.now(), projects: [] };
      setState((s) => ({ ...s, workspaces: [w, ...s.workspaces] }));
      return w;
    },
    deleteWorkspace(id) {
      setState((s) => ({ ...s, workspaces: s.workspaces.filter((w) => w.id !== id) }));
    },
    addProject(wsId, project) {
      setState((s) => ({
        ...s,
        workspaces: s.workspaces.map((w) =>
          w.id === wsId ? { ...w, projects: [project, ...w.projects] } : w),
      }));
    },
    deleteProject(wsId, pId) {
      setState((s) => ({
        ...s,
        workspaces: s.workspaces.map((w) =>
          w.id === wsId ? { ...w, projects: w.projects.filter((p) => p.id !== pId) } : w),
      }));
    },
  };

  return (
    <StoreCtx.Provider value={{ state, actions, toast, toasts }}>
      {children}
    </StoreCtx.Provider>
  );
}

export function useStore() {
  return useContext(StoreCtx);
}

/* ---------- theme --------------------------------------------------------- */

export function useTheme() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next = t === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("wheatear.theme", next); } catch (e) { /* private mode */ }
      return next;
    });
  }, []);
  return [theme, toggle];
}

/* ---------- hash router ---------------------------------------------------- */

export function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").filter(Boolean);
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "new") return { view: "wizard", wsId: parts[1] };
    if (parts[2] === "p" && parts[3]) return { view: "project", wsId: parts[1], pId: parts[3] };
    return { view: "workspace", wsId: parts[1] };
  }
  return { view: "home" };
}

export function useHashRoute() {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const on = () => setRoute(parseRoute());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function go(hash) { location.hash = hash; }
