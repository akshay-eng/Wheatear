/* Migration wizard — the same questions the CLI asks, six steps:
   source → credentials → discover & select → target → LLM → review & run. */

import {
  ArrowLeft, ArrowRight, Check, Eye, EyeOff, Info, KeyRound,
  Loader2, Sparkles,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Btn, Choice, Field, Pill } from "../components/ui.jsx";
import {
  CONNECT_SCRIPT, DEMO_AGENTS, LLM_PROVIDERS, PIPELINE_STAGES, PLATFORMS,
} from "../lib/catalog.js";
import { buildReportMd, collectAnswers } from "../lib/report.js";
import { clockStamp, safeSlug, sleep, uid } from "../lib/utils.js";
import { go, useStore } from "../store.jsx";

const DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";
const STEP_ORDER = ["source", "creds", "discover", "target", "llm", "review"];

function initialWizard() {
  // ?wiz=<step> jumps straight into a pre-filled step (design/demo aid)
  const qp = new URLSearchParams(location.search);
  const jump = qp.get("wiz");
  const base = {
    step: 0,
    source: null,
    creds: { workspaceId: DEFAULT_WORKSPACE_ID },
    discovered: null,
    selected: [],
    target: null,
    llm: { provider: "anthropic", env: "ANTHROPIC_API_KEY" },
  };
  if (!jump) return base;
  const idx = Math.max(0, STEP_ORDER.indexOf(jump));
  base.source = "orchestrate";
  base.creds = {
    instanceUrl: "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df32…767e",
    apiKey: "demo",
    workspaceId: DEFAULT_WORKSPACE_ID,
  };
  if (idx >= 2) {
    base.discovered = DEMO_AGENTS.orchestrate.map((a) => ({ ...a }));
    base.selected = base.discovered.slice(0, 3).map((a) => a.name);
  }
  if (idx >= 4) base.target = "copilot-studio";
  base.step = idx;
  return base;
}

export default function Wizard({ ws }) {
  const [wiz, setWiz] = useState(initialWizard);
  const paneRef = useRef(null);

  const patch = (p) => setWiz((w) => ({ ...w, ...p }));
  const exportOnly = wiz.target === "export-only";

  const steps = [
    { id: "source", label: "Source platform" },
    { id: "creds", label: "Credentials" },
    { id: "discover", label: "Discover & select" },
    { id: "target", label: "Target" },
    { id: "llm", label: exportOnly ? "Translation — skipped" : "Translation LLM" },
    { id: "review", label: "Review & run" },
  ];

  useEffect(() => {
    const h = paneRef.current?.querySelector("h2");
    if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: false }); }
  }, [wiz.step]);

  const back = () => {
    let prev = wiz.step - 1;
    if (STEP_ORDER[prev] === "llm" && exportOnly) prev -= 1;
    patch({ step: Math.max(0, prev) });
  };

  const stepId = STEP_ORDER[wiz.step];
  const stepProps = { wiz, patch, back };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>New migration</h1>
          <p className="sub">Same six questions the CLI wizard asks — nothing is stored except your answers.</p>
        </div>
      </div>
      <div className="wizard">
        <div className="steps" aria-label="Wizard progress">
          {steps.map((s, i) => (
            <div key={s.id} className="step" data-state={i < wiz.step ? "done" : i === wiz.step ? "current" : "todo"}>
              <span className="s-num">{i < wiz.step ? <Check size={11} /> : i + 1}</span>
              <span className="s-label">{s.label}</span>
            </div>
          ))}
        </div>
        <div className="wizard-pane step-enter" ref={paneRef} key={wiz.step}>
          {stepId === "source" && <StepSource {...stepProps} />}
          {stepId === "creds" && <StepCreds {...stepProps} />}
          {stepId === "discover" && <StepDiscover {...stepProps} />}
          {stepId === "target" && <StepTarget {...stepProps} />}
          {stepId === "llm" && <StepLlm {...stepProps} />}
          {stepId === "review" && <StepReview {...stepProps} ws={ws} />}
        </div>
      </div>
    </>
  );
}

/* ---- shared step footer -------------------------------------------------- */

function Foot({ back, nextLabel = "Continue", nextOk = true, onNext, backOk = true }) {
  return (
    <div className="wizard-foot">
      {backOk && <Btn variant="ghost" onClick={back}><ArrowLeft size={15} /> Back</Btn>}
      <div className="spacer" />
      <Btn variant="primary" disabled={!nextOk} onClick={onNext}>
        {nextLabel} <ArrowRight size={15} />
      </Btn>
    </div>
  );
}

/* ---- step 1: source ------------------------------------------------------ */

function StepSource({ wiz, patch, back }) {
  const opts = [
    { id: "orchestrate", desc: "Connect over the REST API with an IBM Cloud API key." },
    { id: "copilot-studio", desc: "Connect to Dataverse with a Microsoft Entra sign-in." },
  ];
  return (
    <>
      <h2>Where are your agents today?</h2>
      <p className="lede">Wheatear connects to the source platform read-only and never modifies it.</p>
      <div className="choice-list" role="radiogroup" aria-label="Source platform">
        {opts.map((o) => (
          <Choice
            key={o.id}
            checked={wiz.source === o.id}
            glyph={PLATFORMS[o.id].Glyph}
            title={PLATFORMS[o.id].label}
            desc={o.desc}
            onPick={() => patch(
              wiz.source === o.id ? {} : { source: o.id, discovered: null, selected: [] })}
          />
        ))}
      </div>
      <Foot back={back} backOk={false} nextOk={!!wiz.source} onNext={() => patch({ step: 1 })} />
    </>
  );
}

/* ---- step 2: credentials -------------------------------------------------- */

function StepCreds({ wiz, patch, back }) {
  const isOrch = wiz.source === "orchestrate";
  const c = wiz.creds;
  const [url, setUrl] = useState(isOrch ? c.instanceUrl || "" : c.envUrl || "");
  const [key, setKey] = useState(c.apiKey || "");
  const [wsid, setWsid] = useState(c.workspaceId ?? DEFAULT_WORKSPACE_ID);
  const [tenant, setTenant] = useState(c.tenantId || "");
  const [showKey, setShowKey] = useState(false);
  const [err, setErr] = useState("");

  const next = () => {
    if (!/^https:\/\/.+/.test(url.trim())) {
      setErr("Enter a full https:// URL.");
      return;
    }
    patch({
      creds: isOrch
        ? { instanceUrl: url.trim(), apiKey: key, workspaceId: wsid.trim() }
        : { envUrl: url.trim(), tenantId: tenant.trim() },
      step: 2,
      discovered: null,
      selected: [],
    });
  };

  return (
    <>
      <h2>{isOrch ? "Source Orchestrate credentials" : "Copilot Studio environment"}</h2>
      <p className="lede">
        {isOrch
          ? "Find these under your instance's Settings in cloud.ibm.com."
          : "Find these in the Power Platform admin center and Microsoft Entra."}
      </p>

      <Field
        label={isOrch ? "Service instance URL" : "Environment URL (Dataverse)"}
        htmlFor="f-url"
        error={err}
        hint={isOrch ? undefined : "Power Platform admin center → Environments → your environment → Environment URL."}
        hintIcon={isOrch ? undefined : Info}
      >
        <input
          id="f-url" className="input mono" autoComplete="off" spellCheck={false}
          placeholder={isOrch
            ? "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/…"
            : "https://org1a2b3c4d.crm.dynamics.com"}
          aria-invalid={!!err}
          value={url}
          onChange={(e) => { setUrl(e.target.value); setErr(""); }}
        />
      </Field>

      {isOrch ? (
        <>
          <Field
            label="IBM Cloud API key" htmlFor="f-key"
            hint="Held in memory for this session only — the CLI stores it in your OS keychain, never on disk."
            hintIcon={KeyRound}
          >
            <div className="input-group">
              <input
                id="f-key" className="input mono" autoComplete="off"
                type={showKey ? "text" : "password"}
                placeholder="••••••••••••••••"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <button
                type="button" className="icon-btn trail"
                aria-label={showKey ? "Hide key" : "Show key"}
                onClick={() => setShowKey(!showKey)}
              >
                {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </Field>
          <Field
            label="Workspace ID" htmlFor="f-wsid"
            hint="The all-zeros…0001 ID is the default workspace on every instance."
            hintIcon={Info}
          >
            <input
              id="f-wsid" className="input mono" spellCheck={false}
              value={wsid}
              onChange={(e) => setWsid(e.target.value)}
            />
          </Field>
        </>
      ) : (
        <>
          <Field
            label="Tenant ID" htmlFor="f-tenant"
            hint="Azure portal → Microsoft Entra ID → Overview."
            hintIcon={Info}
          >
            <input
              id="f-tenant" className="input mono" spellCheck={false}
              placeholder="72f988bf-86f1-41af-91ab-2d7cd011db47"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
            />
          </Field>
          <p className="hint" style={{ marginBottom: 18 }}>
            <KeyRound size={13} />
            <span>No secret needed — connecting opens a device-code sign-in with your Microsoft account.</span>
          </p>
        </>
      )}
      <Foot back={back} nextLabel="Connect" onNext={next} />
    </>
  );
}

/* ---- step 3: connect + discover + select ---------------------------------- */

function StepDiscover({ wiz, patch, back }) {
  const [lines, setLines] = useState(() =>
    wiz.discovered
      ? (CONNECT_SCRIPT[wiz.source] || []).map((s, i) => ({ id: i, state: "ok", msg: s.done, t: clockStamp() }))
      : []);
  const ran = useRef(false);

  useEffect(() => {
    if (wiz.discovered || ran.current) return;
    ran.current = true;
    let live = true;
    (async () => {
      const script = CONNECT_SCRIPT[wiz.source] || [];
      for (let i = 0; i < script.length; i++) {
        if (!live) return;
        setLines((l) => [...l, { id: i, state: "run", msg: script[i].msg, t: clockStamp() }]);
        await sleep(500 + Math.random() * 500);
        if (!live) return;
        setLines((l) => l.map((x) => (x.id === i ? { ...x, state: "ok", msg: script[i].done } : x)));
      }
      if (live) patch({ discovered: DEMO_AGENTS[wiz.source].map((a) => ({ ...a })) });
    })();
    return () => { live = false; };
  }, []);

  const agents = wiz.discovered || [];
  const selected = new Set(wiz.selected);
  const toggle = (name) => {
    const s = new Set(selected);
    if (s.has(name)) s.delete(name); else s.add(name);
    patch({ selected: [...s] });
  };
  const allPicked = selected.size === agents.length && agents.length > 0;

  return (
    <>
      <h2>
        Discover agents{" "}
        <span style={{ verticalAlign: 2, marginLeft: 6 }}><Pill tone="slate">demo data</Pill></span>
      </h2>
      <p className="lede">Connecting read-only and listing what lives in the source instance.</p>

      <div className="log-well" aria-live="polite">
        {lines.map((l) => (
          <div key={l.id} className="log-line" data-state={l.state}>
            <span className="t">{l.t}</span>
            <span className="status-glyph">
              {l.state === "run" ? <Loader2 size={13} className="spin" /> : <Check size={13} className="check-pop" />}
            </span>
            <span className="msg">{l.msg}</span>
          </div>
        ))}
      </div>

      {agents.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <div className="hstack" style={{ marginBottom: 10 }}>
            <strong style={{ fontSize: "var(--text-base)" }}>Select agents to migrate</strong>
            <Pill>{selected.size} of {agents.length}</Pill>
            <div className="spacer" />
            <Btn variant="ghost" sm onClick={() => patch({ selected: allPicked ? [] : agents.map((a) => a.name) })}>
              {allPicked ? "Clear all" : "Select all"}
            </Btn>
          </div>
          <div className="rows">
            {agents.map((a, i) => (
              <button
                key={a.name}
                className="row clickable check-row stagger-in"
                role="checkbox"
                aria-checked={selected.has(a.name)}
                style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
                onClick={() => toggle(a.name)}
              >
                <span className="checkbox"><Check size={12} /></span>
                <span className="r-body">
                  <span className="r-title mono">{a.name}</span>
                  <span className="r-meta">{a.desc}</span>
                </span>
                <span className="r-trail"><Pill tone="slate">{a.kind}</Pill></span>
              </button>
            ))}
          </div>
        </div>
      )}
      <Foot back={back} nextOk={selected.size > 0} onNext={() => patch({ step: 3 })} />
    </>
  );
}

/* ---- step 4: target -------------------------------------------------------- */

function StepTarget({ wiz, patch, back }) {
  const other = { orchestrate: "copilot-studio", "copilot-studio": "orchestrate" }[wiz.source];
  const opts = [
    { id: "export-only", desc: "No migration — save each agent's full export YAML to a local folder.", ok: true },
    { id: other, desc: "Full pipeline: map, translate, validate, write an import-ready package.", ok: true },
    { id: "vertex-ai", desc: "Coming soon", ok: false },
    { id: "bedrock", desc: "Coming soon", ok: false },
    { id: "openai-assistants", desc: "Coming soon", ok: false },
    { id: "n8n", desc: "Coming soon", ok: false },
  ];
  const n = wiz.selected.length;
  return (
    <>
      <h2>Migrate {n} agent{n === 1 ? "" : "s"} to which platform?</h2>
      <p className="lede">Raw export skips translation entirely — nothing leaves your machine.</p>
      <div className="choice-list" role="radiogroup" aria-label="Target platform">
        {opts.map((o) => (
          <Choice
            key={o.id}
            checked={wiz.target === o.id}
            glyph={PLATFORMS[o.id].Glyph}
            title={PLATFORMS[o.id].label}
            desc={o.desc}
            disabled={!o.ok}
            badge={o.ok ? null : <Pill>soon</Pill>}
            onPick={() => patch({ target: o.id })}
          />
        ))}
      </div>
      <Foot
        back={back}
        nextOk={!!wiz.target}
        onNext={() => patch({ step: wiz.target === "export-only" ? 5 : 4 })}
      />
    </>
  );
}

/* ---- step 5: LLM ------------------------------------------------------------ */

function StepLlm({ wiz, patch, back }) {
  const [env, setEnv] = useState(wiz.llm.env);
  return (
    <>
      <h2>Which LLM should translate instructions?</h2>
      <p className="lede">Only the Translate stage calls a model — mapping and validation are deterministic.</p>
      <div className="choice-list" role="radiogroup" aria-label="LLM provider">
        {LLM_PROVIDERS.map((p) => (
          <Choice
            key={p.id}
            checked={wiz.llm.provider === p.id}
            glyph={Sparkles}
            title={p.label}
            badge={p.recommended ? <Pill tone="accent">recommended</Pill> : null}
            desc={p.model}
            descMono
            onPick={() => { patch({ llm: { provider: p.id, env: p.env } }); setEnv(p.env); }}
          />
        ))}
      </div>
      <div style={{ marginTop: 18 }}>
        <Field
          label="API key environment variable" htmlFor="f-env"
          hint="The key is read from this variable at run time — never stored by Wheatear."
          hintIcon={KeyRound}
        >
          <input
            id="f-env" className="input mono" spellCheck={false}
            value={env}
            onChange={(e) => setEnv(e.target.value)}
          />
        </Field>
      </div>
      <Foot back={back} onNext={() => patch({ llm: { ...wiz.llm, env: env.trim() || wiz.llm.env }, step: 5 })} />
    </>
  );
}

/* ---- step 6: review + simulated run ----------------------------------------- */

function StepReview({ wiz, back, ws }) {
  const { actions, toast } = useStore();
  const exportOnly = wiz.target === "export-only";
  const provider = LLM_PROVIDERS.find((p) => p.id === wiz.llm.provider);
  const defaultName = `${PLATFORMS[wiz.source].short} → ${PLATFORMS[wiz.target]?.short || "export"}`;

  const [name, setName] = useState(defaultName);
  const [phase, setPhase] = useState("form"); // form | running | done
  const [lines, setLines] = useState([]);
  const [project, setProject] = useState(null);
  const ran = useRef(false);
  const lineId = useRef(0);
  const wellRef = useRef(null);

  useEffect(() => {
    if (wellRef.current) wellRef.current.scrollTop = wellRef.current.scrollHeight;
  }, [lines]);

  const run = async () => {
    if (ran.current) return;
    ran.current = true;
    setPhase("running");

    const projectName = name.trim() || defaultName;
    const agents = [...wiz.selected];
    const stages = exportOnly ? PIPELINE_STAGES.export : PIPELINE_STAGES.full;
    const startedAt = Date.now();
    const results = [];
    const push = (entry) => {
      const id = lineId.current++;
      setLines((l) => [...l, { id, t: clockStamp(), ...entry }]);
      return id;
    };

    for (let i = 0; i < agents.length; i++) {
      const agent = agents[i];
      push({ state: "head", msg: `agent ${i + 1}/${agents.length} — ${agent}` });
      for (const s of stages) {
        const id = push({ state: "run", msg: `  ${s.msg}` });
        await sleep(320 + Math.random() * 420);
        setLines((l) => l.map((x) => (x.id === id ? { ...x, state: "ok", msg: `  ${s.msg} — ${s.done}` } : x)));
      }
      const needsReview = !exportOnly && i === 0;
      results.push({
        agent,
        kind: (wiz.discovered || []).find((a) => a.name === agent)?.kind || "native",
        status: needsReview ? "review" : "done",
        artifact: exportOnly
          ? `orchestrate-exports/${safeSlug(agent)}/agent.yaml`
          : `${wiz.target}-migration/${safeSlug(agent)}/agent.ir.yaml`,
        stages: stages.map((s) => s.msg),
        notes: needsReview
          ? ["2 connection references need credentials before import", "1 collaborator reference could not be auto-mapped"]
          : [],
      });
    }

    const anyReview = results.some((r) => r.status === "review");
    const proj = {
      id: uid(),
      name: projectName,
      source: wiz.source,
      target: wiz.target,
      sourceRef: wiz.creds.instanceUrl || wiz.creds.envUrl || "",
      workspaceId: wiz.creds.workspaceId || null,
      llm: exportOnly ? null : { ...wiz.llm },
      agents: results,
      status: anyReview ? "attention" : "complete",
      createdAt: Date.now(),
      startedAt,
      finishedAt: Date.now(),
    };
    proj.answers = collectAnswers({ ...wiz, selected: new Set(wiz.selected) }, projectName);
    proj.report = buildReportMd(proj, ws.name);
    actions.addProject(ws.id, proj);
    setProject(proj);

    push({
      state: "ok",
      msg: anyReview
        ? `finished — ${results.length} processed, ${results.filter((r) => r.status === "review").length} need review`
        : `finished — all ${results.length} agent${results.length === 1 ? "" : "s"} clean`,
    });
    setPhase("done");
    toast(`Project “${projectName}” saved`);
  };

  if (phase !== "form") {
    return (
      <>
        <h2>
          {exportOnly ? "Exporting raw YAML" : "Running migration"}{" "}
          <span style={{ verticalAlign: 2, marginLeft: 6 }}><Pill tone="slate">demo run</Pill></span>
        </h2>
        <p className="lede">
          {wiz.selected.length} agent{wiz.selected.length === 1 ? "" : "s"} ·{" "}
          {exportOnly ? "REST export only, no translation" : "extract → map → translate → validate → export"}
        </p>
        <div className="log-well" ref={wellRef} aria-live="polite" style={{ maxHeight: 420, overflowY: "auto" }}>
          {lines.map((l) => (
            <div key={l.id} className="log-line" data-state={l.state === "head" ? undefined : l.state}>
              <span className="t">{l.t}</span>
              {l.state !== "head" && (
                <span className="status-glyph">
                  {l.state === "run" ? <Loader2 size={13} className="spin" /> : <Check size={13} className="check-pop" />}
                </span>
              )}
              <span className={`msg${l.state === "head" ? " head" : ""}`}>{l.msg}</span>
            </div>
          ))}
        </div>
        {phase === "done" && project && (
          <div className="wizard-foot">
            <div className="spacer" />
            <Btn variant="primary" onClick={() => go(`#/w/${ws.id}/p/${project.id}`)}>
              View project <ArrowRight size={15} />
            </Btn>
          </div>
        )}
      </>
    );
  }

  return (
    <>
      <h2>Review &amp; run</h2>
      <p className="lede">Everything below is what the pipeline will do — nothing has run yet.</p>
      <div className="card panel" style={{ marginBottom: 20 }}>
        <dl className="summary">
          <dt>Source</dt><dd>{PLATFORMS[wiz.source].label}</dd>
          <dt>{wiz.source === "orchestrate" ? "Instance" : "Environment"}</dt>
          <dd className="mono">{wiz.creds.instanceUrl || wiz.creds.envUrl || "—"}</dd>
          {wiz.source === "orchestrate" && <><dt>Workspace</dt><dd className="mono">{wiz.creds.workspaceId}</dd></>}
          <dt>Agents</dt><dd>{wiz.selected.length} selected</dd>
          <dt>Target</dt><dd>{PLATFORMS[wiz.target].label}</dd>
          {exportOnly ? (
            <><dt>Output</dt><dd className="mono">./orchestrate-exports/&lt;agent&gt;/agent.yaml</dd></>
          ) : (
            <><dt>Translate LLM</dt>
              <dd>{provider.label} · <span className="mono">{provider.model}</span> (key from <span className="mono">{wiz.llm.env}</span>)</dd></>
          )}
        </dl>
      </div>
      <Field label="Project name" htmlFor="f-proj">
        <input
          id="f-proj" className="input" maxLength={60}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <div className="wizard-foot">
        <Btn variant="ghost" onClick={back}><ArrowLeft size={15} /> Back</Btn>
        <div className="spacer" />
        <Btn variant="primary" onClick={run}>
          {exportOnly ? "Export now" : "Run migration"} <ArrowRight size={15} />
        </Btn>
      </div>
    </>
  );
}
