import { beforeEach, describe, expect, it } from "vitest";
import { defaultSession, loadSession, saveSession, SESSION_KEY } from "./session.js";

describe("session-scoped credential state", () => {
  beforeEach(() => sessionStorage.clear());

  it("stores secrets in sessionStorage and never localStorage", () => {
    const state = defaultSession();
    state.source.api_key = "n8n-secret";
    state.target.api_key = "ibm-secret";
    saveSession(state);

    expect(sessionStorage.getItem(SESSION_KEY)).toContain("ibm-secret");
    expect(window.localStorage?.getItem(SESSION_KEY)).toBeFalsy();
  });

  it("defaults name conflicts to the TUI update policy", () => {
    expect(defaultSession().target.on_conflict).toBe("update");
  });

  it("restores configuration but requires fresh discovery", () => {
    const state = defaultSession();
    state.step = 3;
    state.source.platform = "n8n";
    state.source.items = [{ id: "1", name: "Workflow" }];
    state.source.selected_ids = ["1"];
    saveSession(state);

    const restored = loadSession();

    expect(restored.step).toBe(2);
    expect(restored.source.platform).toBe("n8n");
    expect(restored.source.items).toEqual([]);
    expect(restored.source.selected_ids).toEqual([]);
  });

  it("falls back cleanly when session data is corrupt", () => {
    sessionStorage.setItem(SESSION_KEY, "{broken");
    expect(loadSession()).toEqual(defaultSession());
  });

  it("drops legacy Dataverse credentials while retaining the opaque auth session", () => {
    const state = defaultSession();
    state.source.auth_session_id = "opaque-session";
    state.source.environment_id = "environment-guid";
    state.source.environment_url = "https://secret.crm.dynamics.com";
    state.source.access_token = "legacy-bearer-token";
    saveSession(state);

    const restored = loadSession();

    expect(restored.source.auth_session_id).toBe("opaque-session");
    expect(restored.source.environment_id).toBe("environment-guid");
    expect(restored.source.environment_url).toBeUndefined();
    expect(restored.source.access_token).toBeUndefined();
  });
});
