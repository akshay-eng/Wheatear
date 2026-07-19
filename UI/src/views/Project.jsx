/* Project detail: summary, agents, files (report + answers), Q&A. */

import {
  AlertTriangle, ArrowLeft, Check, Download, Eye, FileText,
} from "lucide-react";
import { useState } from "react";
import { ArmedDelete, Btn, Corridor, Pill, StatusPill } from "../components/ui.jsx";
import { LLM_PROVIDERS, PLATFORMS } from "../lib/catalog.js";
import { buildAnswersYaml } from "../lib/report.js";
import { downloadText, safeSlug, timeAgo } from "../lib/utils.js";
import { go, useStore } from "../store.jsx";

export default function Project({ ws, project: p }) {
  const { actions, toast } = useStore();
  const [peek, setPeek] = useState(null); // null | "report" | "answers"

  const provider = p.llm ? LLM_PROVIDERS.find((x) => x.id === p.llm.provider) : null;
  const reviewAgents = p.agents.filter((a) => a.notes?.length);
  const slug = safeSlug(p.name, "migration").toLowerCase();

  const dlReport = () => downloadText(`${slug}-report.md`, p.report);
  const dlAnswers = () => downloadText(`${slug}-answers.yaml`, buildAnswersYaml(p));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{p.name}</h1>
          <p className="sub" style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Corridor project={p} /> · {timeAgo(p.createdAt)}
          </p>
        </div>
        <div className="spacer" />
        <StatusPill project={p} />
      </div>

      <div className="card panel" style={{ marginBottom: 20 }}>
        <dl className="summary">
          <dt>Source</dt><dd>{PLATFORMS[p.source]?.label || p.source}</dd>
          <dt>{p.source === "orchestrate" ? "Instance" : "Environment"}</dt>
          <dd className="mono">{p.sourceRef || "—"}</dd>
          {p.workspaceId && <><dt>Workspace</dt><dd className="mono">{p.workspaceId}</dd></>}
          <dt>Target</dt><dd>{PLATFORMS[p.target]?.label || p.target}</dd>
          {provider && (
            <><dt>Translate LLM</dt><dd>{provider.label} · <span className="mono">{provider.model}</span></dd></>
          )}
        </dl>
      </div>

      <h2 className="section-title">Agents · {p.agents.length}</h2>
      <div className="rows" style={{ marginBottom: 24 }}>
        {p.agents.map((a) => (
          <div key={a.agent} className="row">
            <span className="r-body">
              <span className="r-title mono">{a.agent}</span>
              <span className="r-meta mono">{a.artifact}</span>
            </span>
            <span className="r-trail">
              {a.status === "review" ? (
                <Pill tone="warn"><AlertTriangle size={12} /> Needs review</Pill>
              ) : (
                <Pill tone="ok"><Check size={12} /> {p.target === "export-only" ? "Exported" : "Migrated"}</Pill>
              )}
            </span>
          </div>
        ))}
      </div>

      {p.report && (
        <>
          <h2 className="section-title">Files</h2>
          <div className="rows" style={{ marginBottom: 12 }}>
            <div className="row">
              <span className="r-icon"><FileText size={16} /></span>
              <span className="r-body">
                <span className="r-title mono">migration-report.md</span>
                <span className="r-meta">Comprehensive run report — corridor, Q&amp;A, per-agent results, follow-ups</span>
              </span>
              <span className="r-trail">
                <button className="icon-btn" aria-label="Preview report" aria-expanded={peek === "report"}
                  onClick={() => setPeek(peek === "report" ? null : "report")}><Eye size={15} /></button>
                <button className="icon-btn" aria-label="Download migration-report.md" onClick={dlReport}><Download size={15} /></button>
              </span>
            </div>
            <div className="row">
              <span className="r-icon"><FileText size={16} /></span>
              <span className="r-body">
                <span className="r-title mono">answers.yaml</span>
                <span className="r-meta">Every wizard question and the answer you gave — secrets excluded</span>
              </span>
              <span className="r-trail">
                <button className="icon-btn" aria-label="Preview answers" aria-expanded={peek === "answers"}
                  onClick={() => setPeek(peek === "answers" ? null : "answers")}><Eye size={15} /></button>
                <button className="icon-btn" aria-label="Download answers.yaml" onClick={dlAnswers}><Download size={15} /></button>
              </span>
            </div>
          </div>
          {peek && (
            <pre className="log-well file-peek">
              {peek === "report" ? p.report : buildAnswersYaml(p)}
            </pre>
          )}
        </>
      )}

      {p.answers?.length > 0 && (
        <>
          <h2 className="section-title">Questions &amp; answers</h2>
          <div className="card panel" style={{ marginBottom: 24 }}>
            <dl className="summary">
              {p.answers.map((x, i) => (
                <span key={i} style={{ display: "contents" }}>
                  <dt>{x.q}</dt>
                  <dd className={/URL|ID|variable/i.test(x.q) ? "mono" : undefined}>{x.a}</dd>
                </span>
              ))}
            </dl>
          </div>
        </>
      )}

      {reviewAgents.length > 0 && (
        <>
          <h2 className="section-title">Review notes</h2>
          <div className="card panel" style={{ marginBottom: 24 }}>
            {reviewAgents.map((a) => (
              <div key={a.agent} style={{ marginBottom: 12 }}>
                <div className="mono" style={{ fontSize: "var(--text-sm)", fontWeight: 500, marginBottom: 6 }}>{a.agent}</div>
                {a.notes.map((n, i) => (
                  <div key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "4px 0", fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>
                    <span style={{ color: "var(--warn)", flex: "none", marginTop: 2 }}><AlertTriangle size={13} /></span>
                    {n}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </>
      )}

      <div className="hstack">
        <Btn onClick={() => go(`#/w/${ws.id}`)}><ArrowLeft size={15} /> Back to workspace</Btn>
        {p.report && <Btn variant="primary" onClick={dlReport}><Download size={15} /> Download report</Btn>}
        <div className="spacer" />
        <ArmedDelete
          asIcon={false}
          label="Delete project"
          onConfirm={() => {
            actions.deleteProject(ws.id, p.id);
            toast("Project deleted", "trash");
            go(`#/w/${ws.id}`);
          }}
        />
      </div>
    </>
  );
}
