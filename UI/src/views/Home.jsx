/* Workspaces overview + inline create. */

import { Layers, Plus } from "lucide-react";
import { useState } from "react";
import { ArmedDelete, Btn, Empty, RowChevron } from "../components/ui.jsx";
import { timeAgo } from "../lib/utils.js";
import { go, useStore } from "../store.jsx";

export default function Home() {
  const { state, actions, toast } = useStore();
  const [name, setName] = useState("");

  const create = (e) => {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    const w = actions.createWorkspace(n);
    toast(`Workspace “${n}” created`);
    setName("");
    go(`#/w/${w.id}`);
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Workspaces</h1>
          <p className="sub">A workspace groups related migration projects — one per team, environment, or platform exit.</p>
        </div>
      </div>

      <form className="inline-create" onSubmit={create}>
        <input
          className="input"
          id="new-ws-name"
          placeholder="Workspace name — e.g. Platform Exit Q3"
          maxLength={60}
          aria-label="New workspace name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Btn variant="primary" type="submit"><Plus size={15} /> Create workspace</Btn>
      </form>

      {state.workspaces.length ? (
        <div className="rows">
          {state.workspaces.map((w) => {
            const agentCount = w.projects.reduce((n, p) => n + p.agents.length, 0);
            return (
              <div
                key={w.id}
                className="row clickable"
                role="link"
                tabIndex={0}
                onClick={() => go(`#/w/${w.id}`)}
                onKeyDown={(e) => { if (e.key === "Enter") go(`#/w/${w.id}`); }}
              >
                <span className="r-glyph"><Layers size={16} /></span>
                <span className="r-body">
                  <span className="r-title">{w.name}</span>
                  <span className="r-meta">
                    {w.projects.length} project{w.projects.length === 1 ? "" : "s"} · {agentCount} agent{agentCount === 1 ? "" : "s"} · created {timeAgo(w.createdAt)}
                  </span>
                </span>
                <span className="r-trail">
                  <ArmedDelete
                    label={`Delete workspace ${w.name}`}
                    onConfirm={() => { actions.deleteWorkspace(w.id); toast("Workspace deleted", "trash"); }}
                  />
                  <RowChevron />
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <Empty glyph={Layers} title="No workspaces yet">
          Create a workspace above to start organizing migrations. Inside it you'll launch projects
          that pull agents out of watsonx Orchestrate or Copilot Studio.
        </Empty>
      )}
    </>
  );
}
