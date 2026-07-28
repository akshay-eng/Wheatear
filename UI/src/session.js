export const SESSION_KEY = "agent-liftoff.migration.session.v1";
const LEGACY_SESSION_KEY = "wheatear.migration.session.v1";

export function defaultSession() {
  return {
    step: 0,
    source: {
      platform: "",
      mode: "live",
      base_url: "http://localhost:5678",
      api_key: "",
      auth_session_id: "",
      account_name: "",
      environment_id: "",
      environments: [],
      upload_id: "",
      scan_id: "",
      solutions: [],
      selected_solution_ids: [],
      selected_ids: [],
      items: [],
    },
    target: {
      instance_url: "",
      api_key: "",
      workspace_id: "00000000-0000-0000-0000-000000000001",
      console_cookie: "",
      model: "groq/openai/gpt-oss-120b",
      deploy: true,
      on_conflict: "update",
    },
    translation: {
      provider: "none",
      api_key: "",
    },
  };
}

export function loadSession() {
  const fallback = defaultSession();
  try {
    const current = sessionStorage.getItem(SESSION_KEY);
    const legacy = sessionStorage.getItem(LEGACY_SESSION_KEY);
    const raw = current || legacy;
    const saved = JSON.parse(raw);
    if (!saved) return fallback;
    if (!current && legacy) sessionStorage.removeItem(LEGACY_SESSION_KEY);
    const {
      environment_url: _legacyEnvironmentUrl,
      access_token: _legacyAccessToken,
      ...savedSource
    } = saved.source || {};
    return {
      ...fallback,
      ...saved,
      source: {
        ...fallback.source,
        ...savedSource,
        items: [],
        solutions: [],
        selected_ids: [],
        selected_solution_ids: [],
        scan_id: "",
        upload_id: "",
      },
      target: { ...fallback.target, ...saved.target },
      translation: { ...fallback.translation, ...saved.translation },
      step: Math.min(Number(saved.step) || 0, 2),
    };
  } catch {
    return fallback;
  }
}

export function saveSession(session) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // Private browsing or a locked-down browser can disable storage.
  }
}
