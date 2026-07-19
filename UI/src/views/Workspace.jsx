/* Workspace detail: stats + project list. */

import { Plus, Route } from "lucide-react";
import { Btn, Corridor, Empty, RowChevron, StatusPill } from "../components/ui.jsx";
import { timeAgo } from "../lib/utils.js";
import { go } from "../store.jsx";

export default function Workspace({ ws }) {
  const agentCount = ws.projects.reduce((n, p) => n + p.agents.length, 0);
  const reviewCount = ws.projects.filter((p) => p.status === "attention").length;
  const start = () => go(`#/w/${ws.id}/new`);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{ws.name}</h1>
          <p className="sub">Created {timeAgo(ws.createdAt)}</p>
        </div>
        <div className="spacer" />
        <Btn variant="primary" onClick={start}><Plus size={15} /> New migration</Btn>
      </div>

      {ws.projects.length ? (
        <>
          <div className="stat-strip">
            <div className="stat"><div className="n">{ws.projects.length}</div><div className="l">Projects</div></div>
            <div className="stat"><div className="n">{agentCount}</div><div className="l">Agents processed</div></div>
            <div className="stat"><div className="n">{reviewCount}</div><div className="l">Needing review</div></div>
          </div>
          <div className="rows">
            {ws.projects.map((p) => (
              <button key={p.id} className="row clickable" onClick={() => go(`#/w/${ws.id}/p/${p.id}`)}>
                <span className="r-body">
                  <span className="r-title">{p.name}</span>
                  <span className="r-meta wrap">
                    <Corridor project={p} /> · {p.agents.length} agent{p.agents.length === 1 ? "" : "s"} · {timeAgo(p.createdAt)}
                  </span>
                </span>
                <span className="r-trail">
                  <StatusPill project={p} />
                  <RowChevron />
                </span>
              </button>
            ))}
          </div>
        </>
      ) : (
        <Empty
          glyph={Route}
          title="No migrations yet"
          action={<Btn variant="primary" onClick={start}><Plus size={15} /> Start a migration</Btn>}
        >
          A migration project connects to a source platform, discovers its agents, and either exports
          them raw or translates them for a new target. It asks the same questions as the CLI wizard.
        </Empty>
      )}
    </>
  );
}
