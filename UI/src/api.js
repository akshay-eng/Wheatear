export async function uploadSource(platform, files) {
  const body = new FormData();
  body.append("platform", platform);
  files.forEach((file) => body.append("files", file));
  return request("/api/uploads", { method: "POST", body });
}

export function discoverSource(source) {
  return request("/api/discover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: stripItems(source) }),
  });
}

export function scanCopilotSolutions(source, solutionIds) {
  return request("/api/copilot/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source: stripItems(source),
      solution_ids: solutionIds,
    }),
  });
}

export function startCopilotAuthSession() {
  return request("/api/copilot/auth/sessions", { method: "POST" });
}

export function getCopilotAuthSession(sessionId) {
  return request(`/api/copilot/auth/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
  });
}

export function deleteCopilotAuthSession(sessionId) {
  return request(`/api/copilot/auth/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export function validateTarget(target) {
  return request("/api/target/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target }),
  });
}

export function configureConnection(payload) {
  return request("/api/connections/configure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function startMigration(payload) {
  return request("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function connectJobEvents(jobId, onLog, onDone, onError) {
  const events = new EventSource(`/api/jobs/${jobId}/events`);
  events.addEventListener("log", (event) => onLog(JSON.parse(event.data)));
  events.addEventListener("done", (event) => onDone(JSON.parse(event.data)));
  events.onerror = onError;
  return events;
}

export async function request(path, options) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { detail: await response.text() };
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join(" ")
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status}).`);
  }
  return payload;
}

function stripItems(source) {
  const {
    account_name,
    environments,
    items,
    solutions,
    selected_solution_ids,
    ...rest
  } = source;
  return rest;
}
