/* Platform catalog + demo discovery data — mirrors the CLI wizard. */

import {
  Server, Bot, Download, Sparkles, Package, Route,
} from "lucide-react";

export const PLATFORMS = {
  orchestrate: { label: "IBM watsonx Orchestrate", short: "watsonx Orchestrate", Glyph: Server },
  "copilot-studio": { label: "Microsoft Copilot Studio", short: "Copilot Studio", Glyph: Bot },
  "export-only": { label: "Export raw YAML to folder", short: "Raw export", Glyph: Download },
  "vertex-ai": { label: "Google Vertex AI Agent Builder", short: "Vertex AI", Glyph: Sparkles },
  bedrock: { label: "AWS Bedrock AgentCore", short: "Bedrock", Glyph: Package },
  "openai-assistants": { label: "OpenAI Assistants", short: "OpenAI", Glyph: Sparkles },
  n8n: { label: "n8n", short: "n8n", Glyph: Route },
};

export const LLM_PROVIDERS = [
  { id: "anthropic", label: "Anthropic Claude", model: "claude-sonnet-5", env: "ANTHROPIC_API_KEY", recommended: true },
  { id: "openai", label: "OpenAI", model: "gpt-4o", env: "OPENAI_API_KEY" },
  { id: "watsonx", label: "IBM watsonx.ai", model: "granite-3-2-8b-instruct", env: "WATSONX_API_KEY" },
  { id: "google", label: "Google Gemini", model: "gemini-2.5-pro", env: "GEMINI_API_KEY" },
];

/* Demo discovery data — stands in for live REST discovery. */
export const DEMO_AGENTS = {
  orchestrate: [
    { name: "Incident_Managementagent_0044s7", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Creates and triages ServiceNow incidents via SNOWMCP tools." },
    { name: "Turbonomic_Alert_Handler_4400Gc", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Handles Turbonomic performance alerts and remediation runbooks." },
    { name: "HR_Onboarding_Assistant", kind: "native", llm: "llama-3-2-90b", desc: "Guides new hires through onboarding tasks and paperwork." },
    { name: "Expense_Audit_Agent", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Reviews expense reports against travel policy." },
    { name: "ServiceNow_Change_Approver", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Summarizes change requests and routes approvals." },
    { name: "Supply_Chain_Monitor", kind: "external", llm: "—", desc: "Watches supplier feeds and flags delivery risk." },
    { name: "Customer_Email_Summarizer", kind: "native", llm: "llama-3-2-90b", desc: "Digests inbound support email into case notes." },
    { name: "Network_Diagnostics_Agent", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Runs AWX network diagnostics playbooks on request." },
    { name: "Payroll_FAQ_Agent", kind: "native", llm: "granite-3-2-8b-instruct", desc: "Answers payroll and benefits questions from the KB." },
    { name: "Contract_Review_Copilot", kind: "external", llm: "—", desc: "First-pass review of contract clauses against playbook." },
  ],
  "copilot-studio": [
    { name: "Benefits Buddy", kind: "copilot", llm: "gpt-4o", desc: "Answers employee benefits questions, 14 topics." },
    { name: "IT Helpdesk Copilot", kind: "copilot", llm: "gpt-4o", desc: "Password resets, device requests, ticket status. 22 topics." },
    { name: "Sales Quote Assistant", kind: "copilot", llm: "gpt-4o", desc: "Builds quotes from price book via Power Automate flows." },
    { name: "Field Service Scheduler", kind: "copilot", llm: "gpt-4o", desc: "Books and reschedules field technician visits." },
    { name: "Legal Intake Bot", kind: "copilot", llm: "gpt-4o", desc: "Routes legal requests to the right practice group." },
    { name: "Store Ops Assistant", kind: "copilot", llm: "gpt-4o", desc: "Store associates ask about planograms and promos." },
  ],
};

export const CONNECT_SCRIPT = {
  orchestrate: [
    { msg: "Authenticating with IBM IAM…", done: "IAM token issued (expires in 3600s)" },
    { msg: "GET /v1/orchestrate/agents", done: "10 agents found" },
    { msg: "GET /v1/orchestrate/toolkits", done: "4 toolkits found (SNOWMCP, itsmtoolsandawx, +2)" },
  ],
  "copilot-studio": [
    { msg: "Device-code sign-in with Microsoft Entra ID…", done: "Access token acquired" },
    { msg: "GET /api/data/v9.2/bots", done: "6 agents found" },
    { msg: "GET /api/data/v9.2/workflows?$filter=category eq 5", done: "12 cloud flows found" },
  ],
};

export const PIPELINE_STAGES = {
  export: [
    { msg: "GET /v1/orchestrate/agents/{id}", done: "export assembled" },
    { msg: "writing agent.yaml", done: "saved" },
  ],
  full: [
    { msg: "extract from source", done: "IR captured" },
    { msg: "map tools & connections", done: "refs resolved" },
    { msg: "translate instructions (LLM)", done: "instructions synthesized" },
    { msg: "validate against target schema", done: "schema OK" },
    { msg: "write target package", done: "package written" },
  ],
};
