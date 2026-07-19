/* Migration report + wizard-answers file builders. Secrets never included. */

import { PLATFORMS, LLM_PROVIDERS } from "./catalog.js";

/* Every wizard question with the answer the user gave. */
export function collectAnswers(wiz, projectName) {
  const exportOnly = wiz.target === "export-only";
  const a = [];
  a.push({ q: "Where are your agents today?", a: PLATFORMS[wiz.source].label });
  if (wiz.source === "orchestrate") {
    a.push({ q: "Service instance URL", a: wiz.creds.instanceUrl });
    a.push({ q: "IBM Cloud API key", a: "(saved in OS keychain — not recorded)" });
    a.push({ q: "Workspace ID", a: wiz.creds.workspaceId });
  } else {
    a.push({ q: "Environment URL (Dataverse)", a: wiz.creds.envUrl });
    a.push({ q: "Tenant ID", a: wiz.creds.tenantId || "—" });
    a.push({ q: "Authentication", a: "Device-code sign-in (no stored secret)" });
  }
  a.push({ q: "Which agents should be migrated?", a: [...wiz.selected].join(", ") });
  a.push({ q: "Migrate to which platform?", a: PLATFORMS[wiz.target].label });
  if (exportOnly) {
    a.push({ q: "Which LLM translates instructions?", a: "Skipped — raw export uses no LLM" });
  } else {
    const p = LLM_PROVIDERS.find((x) => x.id === wiz.llm.provider);
    a.push({ q: "Which LLM translates instructions?", a: `${p.label} (${p.model})` });
    a.push({ q: "API key environment variable", a: wiz.llm.env });
  }
  a.push({ q: "Project name", a: projectName });
  return a;
}

export function buildAnswersYaml(p) {
  const q = (s) => `"${String(s ?? "").replace(/"/g, '\\"')}"`;
  return [
    "# Wheatear — wizard answers",
    `project: ${q(p.name)}`,
    `saved_at: ${q(new Date(p.createdAt).toISOString())}`,
    "answers:",
    ...(p.answers || []).flatMap((x) => [`  - question: ${q(x.q)}`, `    answer: ${q(x.a)}`]),
    "",
  ].join("\n");
}

export function buildReportMd(p, wsName) {
  const clean = p.agents.filter((a) => a.status === "done");
  const review = p.agents.filter((a) => a.status === "review");
  const dur = p.finishedAt && p.startedAt ? `${Math.round((p.finishedAt - p.startedAt) / 1000)}s` : "—";
  const provider = p.llm ? LLM_PROVIDERS.find((x) => x.id === p.llm.provider) : null;
  const L = [];
  L.push(`# Migration report — ${p.name}`);
  L.push("");
  L.push(`- **Workspace:** ${wsName}`);
  L.push(`- **Generated:** ${new Date(p.finishedAt || p.createdAt).toISOString()}`);
  L.push(`- **Corridor:** ${PLATFORMS[p.source]?.label || p.source} → ${PLATFORMS[p.target]?.label || p.target}`);
  L.push(`- **Source ${p.source === "orchestrate" ? "instance" : "environment"}:** \`${p.sourceRef || "—"}\``);
  if (p.workspaceId) L.push(`- **Source workspace:** \`${p.workspaceId}\``);
  L.push(`- **Translate LLM:** ${provider ? `${provider.label} (\`${provider.model}\`, key from \`${p.llm.env}\`)` : "none — raw export"}`);
  L.push(`- **Duration:** ${dur}`);
  L.push(`- **Result:** ${p.agents.length} agent(s) processed — ${clean.length} clean, ${review.length} need review`);
  L.push("");
  L.push("## Questions & answers");
  L.push("");
  L.push("| Question | Answer |");
  L.push("| --- | --- |");
  for (const x of p.answers || []) L.push(`| ${x.q} | ${x.a} |`);
  L.push("");
  L.push("## Agents");
  L.push("");
  for (const a of p.agents) {
    L.push(`### ${a.agent}`);
    L.push("");
    L.push(`- Status: ${a.status === "review" ? "needs review" : p.target === "export-only" ? "exported" : "migrated"}`);
    L.push(`- Artifact: \`${a.artifact}\``);
    if (a.stages?.length) L.push(`- Stages: ${a.stages.join(" → ")}`);
    for (const n of a.notes || []) L.push(`- ⚠ ${n}`);
    L.push("");
  }
  if (review.length) {
    L.push("## Follow-ups before import");
    L.push("");
    for (const a of review) for (const n of a.notes) L.push(`- [ ] **${a.agent}** — ${n}`);
    L.push("");
  }
  return L.join("\n");
}
