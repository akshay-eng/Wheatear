import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Bot,
  Boxes,
  Check,
  CheckCircle2,
  ChevronDown,
  Circle,
  Copy,
  Download,
  ExternalLink,
  Eye,
  EyeOff,
  FileArchive,
  FileText,
  KeyRound,
  ListFilter,
  Loader2,
  LogIn,
  LogOut,
  LockKeyhole,
  Network,
  PackageOpen,
  Play,
  RefreshCw,
  Rocket,
  Route,
  ScanSearch,
  Search,
  Server,
  SortAsc,
  TerminalSquare,
  Upload,
  Workflow,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  configureConnection,
  connectJobEvents,
  deleteCopilotAuthSession,
  discoverSource,
  getCopilotAuthSession,
  scanCopilotSolutions,
  startMigration,
  startCopilotAuthSession,
  uploadSource,
  validateTarget,
} from "./api.js";
import { defaultSession, loadSession, saveSession } from "./session.js";

const STEPS = ["Source", "Target", "Connections", "Select", "Configure", "Execute"];
const IDLE_CHECK = { phase: "idle", message: "", error: "" };
const IDLE_AUTH = {
  phase: "idle",
  user_code: "",
  verification_uri: "",
  account_name: "",
  environments: [],
  error: "",
};

export default function App() {
  const [session, setSession] = useState(loadSession);
  const [files, setFiles] = useState([]);
  const [checks, setChecks] = useState({
    source: { ...IDLE_CHECK },
    target: { ...IDLE_CHECK },
  });
  const [scan, setScan] = useState({ ...IDLE_CHECK });
  const [copilotAuth, setCopilotAuth] = useState(() => ({
    ...IDLE_AUTH,
    phase: session.source.auth_session_id ? "checking" : "idle",
    account_name: session.source.account_name || "",
    environments: session.source.environments || [],
  }));
  const [run, setRun] = useState({
    phase: "idle",
    jobId: "",
    lines: [],
    summary: null,
  });
  const logRef = useRef(null);

  useEffect(() => saveSession(session), [session]);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [run.lines]);
  useEffect(() => {
    const sessionId = session.source.auth_session_id;
    const isCopilotLive = session.source.platform === "copilot-studio"
      && session.source.mode === "live";
    if (!sessionId || !isCopilotLive) return undefined;

    let stopped = false;
    let timer;
    const poll = async () => {
      try {
        const result = await getCopilotAuthSession(sessionId);
        if (stopped) return;
        setCopilotAuth({
          phase: result.status,
          user_code: result.user_code || "",
          verification_uri: result.verification_uri || "",
          account_name: result.account_name || "",
          environments: result.environments || [],
          error: result.error || "",
        });
        if (result.status === "authenticated") {
          setSession((current) => {
            if (current.source.auth_session_id !== sessionId) return current;
            const environments = result.environments || [];
            const selectedStillExists = environments.some(
              (item) => item.id === current.source.environment_id,
            );
            return {
              ...current,
              source: {
                ...current.source,
                account_name: result.account_name || "",
                environments,
                environment_id: selectedStillExists
                  ? current.source.environment_id
                  : "",
              },
            };
          });
          return;
        }
        if (result.status === "failed") return;
        timer = window.setTimeout(poll, 1500);
      } catch (error) {
        if (stopped) return;
        setCopilotAuth({ ...IDLE_AUTH, phase: "error", error: error.message });
        setSession((current) => {
          if (current.source.auth_session_id !== sessionId) return current;
          return {
            ...current,
            source: {
              ...current.source,
              auth_session_id: "",
              account_name: "",
              environment_id: "",
              environments: [],
            },
          };
        });
      }
    };
    poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [
    session.source.auth_session_id,
    session.source.mode,
    session.source.platform,
  ]);

  const patch = (key, values) => {
    setSession((current) => ({
      ...current,
      [key]: { ...current[key], ...values },
    }));
  };

  const setStep = (step) => setSession((current) => ({ ...current, step }));
  const setCheck = (kind, value) => {
    setChecks((current) => ({ ...current, [kind]: value }));
  };

  const invalidateSource = () => {
    setCheck("source", { ...IDLE_CHECK });
    setScan({ ...IDLE_CHECK });
    patch("source", {
      upload_id: "",
      scan_id: "",
      solutions: [],
      selected_solution_ids: [],
      items: [],
      selected_ids: [],
    });
  };

  const invalidateTarget = () => setCheck("target", { ...IDLE_CHECK });

  const startCopilotLogin = async () => {
    setCheck("source", { ...IDLE_CHECK });
    setCopilotAuth({ ...IDLE_AUTH, phase: "starting" });
    try {
      const result = await startCopilotAuthSession();
      setCopilotAuth({
        phase: result.status,
        user_code: result.user_code || "",
        verification_uri: result.verification_uri || "",
        account_name: result.account_name || "",
        environments: result.environments || [],
        error: result.error || "",
      });
      setSession((current) => ({
        ...current,
        source: {
          ...current.source,
          auth_session_id: result.id,
          account_name: "",
          environment_id: "",
          environments: [],
          scan_id: "",
          solutions: [],
          selected_solution_ids: [],
          items: [],
          selected_ids: [],
        },
      }));
    } catch (error) {
      setCopilotAuth({ ...IDLE_AUTH, phase: "error", error: error.message });
    }
  };

  const signOutCopilot = async () => {
    const sessionId = session.source.auth_session_id;
    if (sessionId) {
      try {
        await deleteCopilotAuthSession(sessionId);
      } catch {
        // The local session is cleared even when the server already expired it.
      }
    }
    setCheck("source", { ...IDLE_CHECK });
    setScan({ ...IDLE_CHECK });
    setCopilotAuth({ ...IDLE_AUTH });
    patch("source", {
      auth_session_id: "",
      account_name: "",
      environment_id: "",
      environments: [],
      scan_id: "",
      solutions: [],
      selected_solution_ids: [],
      items: [],
      selected_ids: [],
    });
  };

  const chooseSource = (platform) => {
    setFiles([]);
    setChecks({ source: { ...IDLE_CHECK }, target: { ...IDLE_CHECK } });
    setScan({ ...IDLE_CHECK });
    setSession((current) => ({
      ...current,
      source: {
        ...defaultSession().source,
        platform,
        base_url: current.source.base_url || "http://localhost:5678",
      },
    }));
  };

  const chooseSourceMode = (mode) => {
    setFiles([]);
    setCheck("source", { ...IDLE_CHECK });
    setScan({ ...IDLE_CHECK });
    patch("source", {
      mode,
      upload_id: "",
      scan_id: "",
      solutions: [],
      selected_solution_ids: [],
      items: [],
      selected_ids: [],
    });
  };

  const testSource = async () => {
    setCheck("source", { phase: "loading", message: "", error: "" });
    try {
      const source = { ...session.source };
      let discovered;
      if (source.mode === "upload") {
        if (!files.length) throw new Error("Choose the source export first.");
        discovered = await uploadSource(source.platform, files);
        source.upload_id = discovered.upload_id;
      } else {
        discovered = await discoverSource(source);
      }

      const isCopilotLive = source.platform === "copilot-studio" && source.mode === "live";
      setSession((current) => ({
        ...current,
        source: {
          ...source,
          scan_id: "",
          solutions: isCopilotLive ? discovered.items : [],
          selected_solution_ids: [],
          items: isCopilotLive ? [] : discovered.items,
          selected_ids: [],
        },
      }));
      setScan({ ...IDLE_CHECK });
      setCheck("source", {
        phase: "ready",
        message: discovered.message,
        error: "",
      });
    } catch (error) {
      setCheck("source", { phase: "error", message: "", error: error.message });
    }
  };

  const testTarget = async () => {
    setCheck("target", { phase: "loading", message: "", error: "" });
    try {
      const result = await validateTarget(session.target);
      setCheck("target", { phase: "ready", message: result.message, error: "" });
    } catch (error) {
      setCheck("target", { phase: "error", message: "", error: error.message });
    }
  };

  const scanSolutions = async () => {
    setScan({ phase: "loading", message: "", error: "" });
    try {
      const result = await scanCopilotSolutions(
        session.source,
        session.source.selected_solution_ids,
      );
      patch("source", {
        scan_id: result.scan_id,
        items: result.items,
        selected_ids: [],
      });
      setScan({
        phase: "ready",
        message: result.message,
        error: (result.issues || []).join(" "),
      });
    } catch (error) {
      setScan({ phase: "error", message: "", error: error.message });
    }
  };

  const toggleSourceId = (field, id) => {
    const picked = new Set(session.source[field]);
    if (picked.has(id)) picked.delete(id);
    else picked.add(id);
    patch("source", { [field]: [...picked] });
  };

  const execute = async () => {
    setRun({ phase: "starting", jobId: "", lines: [], summary: null });
    try {
      const job = await startMigration({
        source: stripUiSource(session.source),
        target: session.target,
        translation: session.translation,
      });
      setRun((current) => ({ ...current, phase: "running", jobId: job.id }));
      const stream = connectJobEvents(
        job.id,
        (line) => setRun((current) => ({
          ...current,
          lines: [...current.lines, line],
        })),
        (result) => {
          stream.close();
          setRun((current) => ({
            ...current,
            phase: result.status === "completed" ? "completed" : "failed",
            summary: result.summary,
          }));
        },
        () => {
          stream.close();
          setRun((current) => ({
            ...current,
            phase: "failed",
            summary: { message: "The live log connection closed unexpectedly." },
          }));
        },
      );
    } catch (error) {
      setRun({
        phase: "failed",
        jobId: "",
        lines: [],
        summary: { message: error.message },
      });
    }
  };

  const reset = () => {
    setRun({ phase: "idle", jobId: "", lines: [], summary: null });
    setChecks({ source: { ...IDLE_CHECK }, target: { ...IDLE_CHECK } });
    setScan({ ...IDLE_CHECK });
    setSession((current) => ({
      ...current,
      step: 0,
      source: {
        ...current.source,
        upload_id: "",
        scan_id: "",
        solutions: [],
        selected_solution_ids: [],
        items: [],
        selected_ids: [],
      },
    }));
    setFiles([]);
  };

  const canContinue = [
    Boolean(session.source.platform),
    true,
    checks.source.phase === "ready" && checks.target.phase === "ready",
    session.source.selected_ids.length > 0,
    session.translation.provider === "none" || Boolean(session.translation.api_key),
    false,
  ];

  return (
    <div className="app-shell">
      <Header />
      <main className="workspace">
        <StepRail current={session.step} />
        <section className="work-area" aria-live="polite">
          {session.step === 0 && (
            <SourceStep
              session={session}
              chooseSource={chooseSource}
              chooseSourceMode={chooseSourceMode}
            />
          )}
          {session.step === 1 && <TargetStep session={session} patch={patch} />}
          {session.step === 2 && (
            <ConnectionsStep
              session={session}
              patch={patch}
              files={files}
              setFiles={setFiles}
              checks={checks}
              testSource={testSource}
              testTarget={testTarget}
              invalidateSource={invalidateSource}
              invalidateTarget={invalidateTarget}
              copilotAuth={copilotAuth}
              startCopilotLogin={startCopilotLogin}
              signOutCopilot={signOutCopilot}
            />
          )}
          {session.step === 3 && (
            <SelectionStep
              session={session}
              patch={patch}
              scan={scan}
              scanSolutions={scanSolutions}
              toggleSourceId={toggleSourceId}
            />
          )}
          {session.step === 4 && (
            <ConfigureStep session={session} patch={patch} checks={checks} />
          )}
          {session.step === 5 && (
            <ExecuteStep
              session={session}
              run={run}
              execute={execute}
              reset={reset}
              logRef={logRef}
            />
          )}

          {session.step < 5 && (
            <footer className="wizard-actions">
              <button
                className="btn btn-quiet"
                disabled={session.step === 0}
                onClick={() => setStep(session.step - 1)}
              >
                <ArrowLeft size={16} /> Back
              </button>
              <span className="step-progress">{session.step + 1} / {STEPS.length}</span>
              <button
                className="btn btn-primary"
                disabled={!canContinue[session.step]}
                onClick={() => setStep(session.step + 1)}
              >
                Continue <ArrowRight size={16} />
              </button>
            </footer>
          )}
        </section>
      </main>
    </div>
  );
}

function Header() {
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark"><Rocket size={21} /></span>
        <span>
          <strong>Agent Liftoff</strong>
          <small>Migration Control</small>
        </span>
      </div>
      <div className="header-status">
        <span className="engine-status"><span /> Compiler online</span>
        <span className="session-note">
          <LockKeyhole size={15} />
          Secrets clear with this tab
        </span>
      </div>
    </header>
  );
}

function StepRail({ current }) {
  return (
    <nav className="step-rail" aria-label="Migration steps">
      {STEPS.map((label, index) => {
        const state = index < current ? "done" : index === current ? "current" : "todo";
        return (
          <div className="rail-step" data-state={state} key={label}>
            <span className="rail-marker">
              {state === "done" ? <Check size={14} /> : index + 1}
            </span>
            <span>
              <small>Step {index + 1}</small>
              <strong>{label}</strong>
            </span>
          </div>
        );
      })}
    </nav>
  );
}

function StepHeading({ eyebrow, title, children }) {
  return (
    <div className="step-heading">
      <span>{eyebrow}</span>
      <h1>{title}</h1>
      {children && <p>{children}</p>}
    </div>
  );
}

function SourceStep({ session, chooseSource, chooseSourceMode }) {
  const source = session.source;
  return (
    <>
      <StepHeading eyebrow="Source" title="Choose the migration corridor">
        Agent Liftoff preserves the source hierarchy before compiling anything.
      </StepHeading>
      <div className="platform-grid" role="radiogroup" aria-label="Source platform">
        <PlatformChoice
          selected={source.platform === "copilot-studio"}
          icon={Bot}
          name="Microsoft Copilot Studio"
          detail="Solutions, agents, tools, knowledge and delegation"
          onClick={() => chooseSource("copilot-studio")}
        />
        <PlatformChoice
          selected={source.platform === "n8n"}
          icon={Workflow}
          name="n8n"
          detail="Workflows, sub-workflows, tools and collaborators"
          onClick={() => chooseSource("n8n")}
        />
      </div>
      {source.platform && (
        <div className="mode-block">
          <label>Source access</label>
          <Segmented
            value={source.mode}
            onChange={chooseSourceMode}
            options={[
              { value: "live", label: "Live environment", icon: Network },
              { value: "upload", label: "Export files", icon: Upload },
            ]}
          />
          <p className="mode-note">
            {source.platform === "copilot-studio"
              ? source.mode === "live"
                ? "Browse unmanaged solutions, scan selected solutions, then choose their agents."
                : "Upload one PAC-unpacked solution ZIP and choose agents from it."
              : source.mode === "live"
                ? "Discover workflows from n8n and pull collaborator definitions automatically."
                : "Upload one or more workflow JSON exports."}
          </p>
        </div>
      )}
    </>
  );
}

function PlatformChoice({ selected, icon: Icon, name, detail, onClick }) {
  return (
    <button
      className="platform-choice"
      data-selected={selected}
      role="radio"
      aria-checked={selected}
      onClick={onClick}
    >
      <span className="platform-icon"><Icon size={23} /></span>
      <span className="platform-copy">
        <strong>{name}</strong>
        <small>{detail}</small>
      </span>
      <span className="radio-dot" />
    </button>
  );
}

function TargetStep({ session, patch }) {
  const target = session.target;
  return (
    <>
      <StepHeading eyebrow="Target" title="Set the delivery policy">
        Both corridors compile to native IBM watsonx Orchestrate assets.
      </StepHeading>
      <div className="target-platform">
        <span className="platform-icon target-icon"><Server size={24} /></span>
        <span>
          <strong>IBM watsonx Orchestrate</strong>
          <small>Agents, tools, knowledge bases, connections and collaborators</small>
        </span>
        <CheckCircle2 size={20} className="target-check" />
      </div>
      <div className="target-options">
        <div className="form-section">
          <div className="section-title">
            <div>
              <h2>Delivery</h2>
              <p>Deploy directly or compile a reviewable result bundle.</p>
            </div>
            <Toggle
              checked={target.deploy}
              onChange={(deploy) => patch("target", { deploy })}
              label={target.deploy ? "Deploy to target" : "Dry run only"}
            />
          </div>
        </div>
        <div className="form-section">
          <h2>Existing agent names</h2>
          <Segmented
            value={target.on_conflict}
            onChange={(on_conflict) => patch("target", { on_conflict })}
            options={[
              { value: "rename", label: "Rename new" },
              { value: "update", label: "Update existing" },
              { value: "skip", label: "Keep existing" },
            ]}
          />
          <p>
            {target.on_conflict === "rename" && "Create a numbered name when the target name is occupied."}
            {target.on_conflict === "update" && "Replace the existing target agent with the compiled version."}
            {target.on_conflict === "skip" && "Keep existing agents and reuse them for collaborator wiring."}
          </p>
          {target.on_conflict === "update" && (
            <InlineNotice tone="warning">
              Matching target agents will be replaced during deployment.
            </InlineNotice>
          )}
        </div>
      </div>
    </>
  );
}

function ConnectionsStep({
  session,
  patch,
  files,
  setFiles,
  checks,
  testSource,
  testTarget,
  invalidateSource,
  invalidateTarget,
  copilotAuth,
  startCopilotLogin,
  signOutCopilot,
}) {
  const { source, target } = session;
  const sourceTitle = source.platform === "n8n" ? "n8n source" : "Copilot Studio source";

  const patchSource = (values) => {
    invalidateSource();
    patch("source", values);
  };
  const patchTarget = (values) => {
    invalidateTarget();
    patch("target", values);
  };
  const copilotReady = copilotAuth.phase === "authenticated"
    && Boolean(source.environment_id);

  return (
    <>
      <StepHeading eyebrow="Connections" title="Connect both environments">
        Validate source and target independently before browsing migration candidates.
      </StepHeading>
      <div className="connection-grid">
        <ConnectionPanel
          icon={source.platform === "n8n" ? Route : Bot}
          title={sourceTitle}
          check={checks.source}
          action={testSource}
          actionLabel={
            source.mode === "upload"
              ? "Read export"
              : source.platform === "copilot-studio"
                ? "Discover solutions"
                : "Test and discover"
          }
          actionDisabled={
            source.mode === "live"
            && source.platform === "copilot-studio"
            && !copilotReady
          }
        >
          {source.mode === "upload" ? (
            <UploadField
              platform={source.platform}
              files={files}
              onChange={(nextFiles) => {
                invalidateSource();
                setFiles(nextFiles);
              }}
            />
          ) : source.platform === "n8n" ? (
            <>
              <TextField
                label="n8n base URL"
                value={source.base_url}
                placeholder="http://localhost:5678"
                onChange={(base_url) => patchSource({ base_url })}
              />
              <SecretField
                label="n8n API key"
                value={source.api_key}
                onChange={(api_key) => patchSource({ api_key })}
              />
            </>
          ) : (
            <CopilotSignIn
              auth={copilotAuth}
              source={source}
              start={startCopilotLogin}
              signOut={signOutCopilot}
              selectEnvironment={(environment_id) => patchSource({ environment_id })}
            />
          )}
        </ConnectionPanel>

        <ConnectionPanel
          icon={Server}
          title="watsonx Orchestrate target"
          check={checks.target}
          action={testTarget}
          actionLabel="Test target"
        >
          <TextField
            label="Service instance URL"
            value={target.instance_url}
            placeholder="https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/..."
            onChange={(instance_url) => patchTarget({ instance_url })}
          />
          <SecretField
            label="IBM Cloud API key"
            value={target.api_key}
            onChange={(api_key) => patchTarget({ api_key })}
          />
          <TextField
            label="Workspace ID"
            value={target.workspace_id}
            onChange={(workspace_id) => patchTarget({ workspace_id })}
            mono
          />
          <SecretField
            label="Console cookie"
            value={target.console_cookie}
            onChange={(console_cookie) => patchTarget({ console_cookie })}
            optional
          />
        </ConnectionPanel>
      </div>
      <div className="connection-gate">
        <StatusDot ready={checks.source.phase === "ready"} />
        Source
        <span />
        <StatusDot ready={checks.target.phase === "ready"} />
        Target
        <small>Both checks must pass to continue.</small>
      </div>
    </>
  );
}

function ConnectionPanel({
  icon: Icon,
  title,
  check,
  action,
  actionLabel,
  actionDisabled = false,
  children,
}) {
  return (
    <section className="connection-panel">
      <div className="connection-panel-head">
        <SectionLabel icon={Icon} title={title} />
        <ConnectionBadge phase={check.phase} />
      </div>
      <div className="connection-fields">{children}</div>
      <button
        className="btn btn-secondary connection-action"
        disabled={check.phase === "loading" || actionDisabled}
        onClick={action}
      >
        {check.phase === "loading"
          ? <Loader2 size={16} className="spin" />
          : <RefreshCw size={16} />}
        {check.phase === "loading" ? "Connecting..." : actionLabel}
      </button>
      {check.message && <InlineNotice tone="success">{check.message}</InlineNotice>}
      {check.error && <InlineNotice tone="error">{check.error}</InlineNotice>}
    </section>
  );
}

function CopilotSignIn({ auth, source, start, signOut, selectEnvironment }) {
  const waiting = ["starting", "checking", "pending"].includes(auth.phase);
  const authenticated = auth.phase === "authenticated";

  if (authenticated) {
    return (
      <div className="microsoft-auth">
        <div className="signed-in-account">
          <span className="auth-state-icon"><CheckCircle2 size={18} /></span>
          <span>
            <small>Signed in with Microsoft</small>
            <strong>{auth.account_name || source.account_name || "Power Platform user"}</strong>
          </span>
          <button
            className="icon-button"
            title="Use another Microsoft account"
            aria-label="Use another Microsoft account"
            onClick={signOut}
          >
            <LogOut size={16} />
          </button>
        </div>
        <EnvironmentPicker
          environments={auth.environments}
          selectedId={source.environment_id}
          onSelect={selectEnvironment}
        />
      </div>
    );
  }

  return (
    <div className="microsoft-auth">
      {auth.phase === "pending" ? (
        <div className="device-login">
          <span className="auth-kicker">Microsoft sign-in ready</span>
          <strong className="device-code">{auth.user_code}</strong>
          <p>Open Microsoft sign-in and enter this one-time code.</p>
          <div className="device-actions">
            <button
              className="btn btn-secondary btn-small"
              onClick={() => window.open(
                auth.verification_uri,
                "_blank",
                "noopener,noreferrer",
              )}
            >
              <ExternalLink size={15} /> Open Microsoft
            </button>
            <button
              className="icon-button"
              title="Copy one-time code"
              aria-label="Copy one-time code"
              onClick={() => navigator.clipboard?.writeText(auth.user_code)}
            >
              <Copy size={15} />
            </button>
          </div>
          <span className="auth-waiting">
            <Loader2 size={14} className="spin" /> Waiting for sign-in
          </span>
        </div>
      ) : (
        <div className="sign-in-start">
          <span className="auth-state-icon"><LockKeyhole size={20} /></span>
          <div>
            <strong>Microsoft account</strong>
            <p>Sign in on Microsoft to use your password, MFA, or passwordless account.</p>
          </div>
          <button
            className="btn btn-secondary"
            disabled={waiting}
            onClick={start}
          >
            {waiting
              ? <Loader2 size={16} className="spin" />
              : <LogIn size={16} />}
            {waiting ? "Starting sign-in..." : "Sign in with Microsoft"}
          </button>
        </div>
      )}
      {auth.error && <InlineNotice tone="error">{auth.error}</InlineNotice>}
      {auth.phase === "failed" && (
        <button className="btn btn-quiet btn-small auth-retry" onClick={start}>
          <RefreshCw size={14} /> Try again
        </button>
      )}
    </div>
  );
}

function EnvironmentPicker({ environments, selectedId, onSelect }) {
  const [query, setQuery] = useState("");
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return environments.filter((item) => (
      !needle || `${item.name} ${item.id}`.toLowerCase().includes(needle)
    ));
  }, [environments, query]);

  return (
    <div className="environment-picker">
      <div className="environment-picker-head">
        <span>
          <strong>Power Platform environment</strong>
          <small>{environments.length} available</small>
        </span>
        <Search size={15} />
        <input
          aria-label="Search Power Platform environments"
          value={query}
          placeholder="Search environments"
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div
        className="environment-list"
        role="radiogroup"
        aria-label="Power Platform environment"
      >
        {visible.map((environment) => (
          <button
            className="environment-row"
            data-selected={selectedId === environment.id}
            role="radio"
            aria-checked={selectedId === environment.id}
            aria-label={`${environment.name} environment`}
            key={environment.id}
            onClick={() => onSelect(environment.id)}
          >
            <span className="radio-dot" />
            <span>
              <strong>{environment.name}</strong>
              <small>{environment.id}</small>
            </span>
          </button>
        ))}
        {!visible.length && (
          <div className="list-empty">No environments match this search.</div>
        )}
      </div>
    </div>
  );
}

function ConnectionBadge({ phase }) {
  const labels = {
    idle: "Not tested",
    loading: "Testing",
    ready: "Connected",
    error: "Needs attention",
  };
  return <span className="connection-badge" data-phase={phase}>{labels[phase]}</span>;
}

function SelectionStep({
  session,
  patch,
  scan,
  scanSolutions,
  toggleSourceId,
}) {
  const { source } = session;
  const copilotLive = source.platform === "copilot-studio" && source.mode === "live";
  return (
    <>
      <StepHeading eyebrow="Select" title={
        copilotLive ? "Choose solutions, then agents" : `Choose ${source.platform === "n8n" ? "workflows" : "agents"}`
      }>
        {copilotLive
          ? "This follows the source structure: scan only the solutions you choose, then select their agents."
          : source.platform === "n8n"
            ? "Selected supervisors automatically pull in the sub-workflows they delegate to."
            : "Choose the agents to compile from the uploaded solution."}
      </StepHeading>

      {copilotLive && (
        <SolutionSelector
          solutions={source.solutions}
          selectedIds={source.selected_solution_ids}
          setSelectedIds={(selected_solution_ids) => patch("source", {
            selected_solution_ids,
            items: [],
            selected_ids: [],
          })}
          scan={scan}
          scanSolutions={scanSolutions}
        />
      )}

      {scan.error && <InlineNotice tone="error">{scan.error}</InlineNotice>}
      {scan.message && <InlineNotice tone="success">{scan.message}</InlineNotice>}

      {source.items.length > 0 && (
        <ItemSelector
          platform={source.platform}
          items={source.items}
          selectedIds={source.selected_ids}
          setSelectedIds={(selected_ids) => patch("source", { selected_ids })}
          toggleItem={(id) => toggleSourceId("selected_ids", id)}
          grouped={source.platform === "copilot-studio"}
        />
      )}

      {copilotLive && scan.phase === "ready" && source.items.length === 0 && (
        <EmptyState
          icon={PackageOpen}
          title="No agents found in those solutions"
          detail="Change the solution selection and scan again. Only PAC-unpacked bot components are migration candidates."
        />
      )}
    </>
  );
}

function SolutionSelector({
  solutions,
  selectedIds,
  setSelectedIds,
  scan,
  scanSolutions,
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("name");
  const selected = new Set(selectedIds);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return [...solutions]
      .filter((item) => !needle || `${item.name} ${item.description} ${item.version}`
        .toLowerCase().includes(needle))
      .sort((a, b) => {
        if (sort === "version") return naturalCompare(b.version, a.version);
        if (sort === "unique") return naturalCompare(a.description, b.description);
        return naturalCompare(a.name, b.name);
      });
  }, [solutions, query, sort]);

  const selectVisible = () => {
    const next = new Set(selected);
    const allVisibleSelected = visible.every((item) => next.has(item.id));
    visible.forEach((item) => {
      if (allVisibleSelected) next.delete(item.id);
      else next.add(item.id);
    });
    setSelectedIds([...next]);
  };

  return (
    <section className="selection-surface solution-surface">
      <SelectionHeader
        icon={Boxes}
        title="Unmanaged solutions"
        count={`${selected.size} of ${solutions.length} selected`}
      />
      <SelectionToolbar
        query={query}
        setQuery={setQuery}
        placeholder="Search solution name or unique name"
        sort={sort}
        setSort={setSort}
        sortOptions={[
          ["name", "Friendly name"],
          ["unique", "Unique name"],
          ["version", "Version"],
        ]}
        actionLabel={visible.length && visible.every((item) => selected.has(item.id))
          ? "Clear visible"
          : "Select visible"}
        action={selectVisible}
      />
      <div className="solution-list">
        {visible.map((solution) => (
          <button
            className="solution-row"
            data-selected={selected.has(solution.id)}
            key={solution.id}
            role="checkbox"
            aria-checked={selected.has(solution.id)}
            onClick={() => {
              const next = new Set(selected);
              if (next.has(solution.id)) next.delete(solution.id);
              else next.add(solution.id);
              setSelectedIds([...next]);
            }}
          >
            <span className="checkbox">
              {selected.has(solution.id) && <Check size={13} />}
            </span>
            <span className="solution-main">
              <strong>{solution.name}</strong>
              <small>{solution.description}</small>
            </span>
            <span className="version-label">v{solution.version || "unknown"}</span>
          </button>
        ))}
        {!visible.length && (
          <div className="list-empty">No solutions match this search.</div>
        )}
      </div>
      <div className="scan-action">
        <div>
          <strong>{selected.size} solution{selected.size === 1 ? "" : "s"} queued for scan</strong>
          <small>Export and PAC unpack are cached for this browser session.</small>
        </div>
        <button
          className="btn btn-primary"
          disabled={!selected.size || scan.phase === "loading"}
          onClick={scanSolutions}
        >
          {scan.phase === "loading"
            ? <Loader2 size={16} className="spin" />
            : <ScanSearch size={16} />}
          {scan.phase === "loading" ? "Exporting and scanning..." : "Scan selected solutions"}
        </button>
      </div>
    </section>
  );
}

function ItemSelector({
  platform,
  items,
  selectedIds,
  setSelectedIds,
  toggleItem,
  grouped,
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("name");
  const [stateFilter, setStateFilter] = useState("all");
  const selected = new Set(selectedIds);
  const isN8n = platform === "n8n";
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return [...items]
      .filter((item) => !needle || `${item.name} ${item.description} ${item.group_name}`
        .toLowerCase().includes(needle))
      .filter((item) => {
        if (!isN8n || stateFilter === "all") return true;
        return stateFilter === "active" ? item.active : !item.active;
      })
      .sort((a, b) => {
        if (sort === "schema") return naturalCompare(a.source_id, b.source_id);
        if (sort === "solution") {
          return naturalCompare(a.group_name, b.group_name)
            || naturalCompare(a.name, b.name);
        }
        if (sort === "status") return Number(b.active) - Number(a.active)
          || naturalCompare(a.name, b.name);
        return naturalCompare(a.name, b.name);
      });
  }, [items, query, sort, stateFilter, isN8n]);

  const groups = useMemo(() => {
    if (!grouped) return [["", visible]];
    const mapped = new Map();
    visible.forEach((item) => {
      const key = item.group_id || "ungrouped";
      if (!mapped.has(key)) mapped.set(key, []);
      mapped.get(key).push(item);
    });
    return [...mapped.entries()];
  }, [visible, grouped]);

  const toggleVisible = () => {
    const next = new Set(selected);
    const allVisibleSelected = visible.every((item) => next.has(item.id));
    visible.forEach((item) => {
      if (allVisibleSelected) next.delete(item.id);
      else next.add(item.id);
    });
    setSelectedIds([...next]);
  };

  const toggleGroup = (groupItems) => {
    const next = new Set(selected);
    const allSelected = groupItems.every((item) => next.has(item.id));
    groupItems.forEach((item) => {
      if (allSelected) next.delete(item.id);
      else next.add(item.id);
    });
    setSelectedIds([...next]);
  };

  return (
    <section className="selection-surface item-surface">
      <SelectionHeader
        icon={isN8n ? Workflow : Bot}
        title={`Select ${isN8n ? "workflows" : "agents"}`}
        count={`${selected.size} of ${items.length} selected`}
      />
      <SelectionToolbar
        query={query}
        setQuery={setQuery}
        placeholder={`Search ${isN8n ? "workflow" : "agent"} name${grouped ? ", schema or solution" : ""}`}
        sort={sort}
        setSort={setSort}
        sortOptions={isN8n
          ? [["name", "Name"], ["status", "Status"]]
          : [["name", "Name"], ["schema", "Schema"], ["solution", "Solution"]]}
        actionLabel={visible.length && visible.every((item) => selected.has(item.id))
          ? "Clear visible"
          : "Select visible"}
        action={toggleVisible}
      >
        {isN8n && (
          <select
            className="compact-select"
            aria-label="Workflow status filter"
            value={stateFilter}
            onChange={(event) => setStateFilter(event.target.value)}
          >
            <option value="all">All states</option>
            <option value="active">Active only</option>
            <option value="inactive">Inactive only</option>
          </select>
        )}
      </SelectionToolbar>
      <div className="grouped-list">
        {groups.map(([groupId, groupItems]) => (
          <div className="item-group" key={groupId || "all"}>
            {grouped && (
              <div className="item-group-head">
                <span>
                  <strong>{groupItems[0]?.group_name || groupId}</strong>
                  <small>{groupItems[0]?.group_id} {groupItems[0]?.version && `v${groupItems[0].version}`}</small>
                </span>
                <button
                  className="text-action"
                  onClick={() => toggleGroup(groupItems)}
                >
                  {groupItems.every((item) => selected.has(item.id)) ? "Clear group" : "Select group"}
                </button>
              </div>
            )}
            {groupItems.map((item) => (
              <button
                className="select-item"
                key={item.id}
                role="checkbox"
                aria-checked={selected.has(item.id)}
                onClick={() => toggleItem(item.id)}
              >
                <span className="checkbox">
                  {selected.has(item.id) && <Check size={13} />}
                </span>
                <span className="item-copy">
                  <strong>{item.name}</strong>
                  <small>{item.description || item.kind}</small>
                </span>
                {item.active != null && (
                  <span className={`status-label ${item.active ? "active" : ""}`}>
                    {item.active ? "Active" : "Inactive"}
                  </span>
                )}
              </button>
            ))}
          </div>
        ))}
        {!visible.length && (
          <div className="list-empty">No items match the current search and filters.</div>
        )}
      </div>
      <div className="selection-footer">
        <span>{visible.length} visible</span>
        <span>{selected.size} selected</span>
        {selected.size > 0 && (
          <button className="text-action" onClick={() => setSelectedIds([])}>
            Clear selection
          </button>
        )}
      </div>
    </section>
  );
}

function SelectionHeader({ icon: Icon, title, count }) {
  return (
    <div className="selection-title">
      <span className="selection-icon"><Icon size={17} /></span>
      <div><h2>{title}</h2><p>{count}</p></div>
    </div>
  );
}

function SelectionToolbar({
  query,
  setQuery,
  placeholder,
  sort,
  setSort,
  sortOptions,
  actionLabel,
  action,
  children,
}) {
  return (
    <div className="selection-toolbar">
      <label className="search-field">
        <Search size={15} />
        <input
          aria-label={placeholder}
          value={query}
          placeholder={placeholder}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      {children}
      <label className="sort-field">
        <SortAsc size={15} />
        <select
          aria-label="Sort selection"
          value={sort}
          onChange={(event) => setSort(event.target.value)}
        >
          {sortOptions.map(([value, label]) => (
            <option value={value} key={value}>{label}</option>
          ))}
        </select>
        <ChevronDown size={14} />
      </label>
      <button className="btn btn-quiet btn-small" disabled={!action} onClick={action}>
        {actionLabel}
      </button>
    </div>
  );
}

function ConfigureStep({ session, patch, checks }) {
  const { source, target, translation } = session;
  const selectedItems = source.items.filter((item) => source.selected_ids.includes(item.id));
  const solutionCount = new Set(selectedItems.map((item) => item.group_id).filter(Boolean)).size;
  return (
    <>
      <StepHeading eyebrow="Configure" title="Translation and preflight">
        Confirm the compiler inputs before starting a billable or target-changing operation.
      </StepHeading>
      <div className="configure-grid">
        <section className="configure-panel">
          <SectionLabel icon={KeyRound} title="Translation model" />
          <p className="panel-copy">
            Optional for instruction synthesis, descriptions and ambiguous tool matching.
          </p>
          <label className="field">
            <span>Provider</span>
            <select
              className="select"
              aria-label="Translation model provider"
              value={translation.provider}
              onChange={(event) => patch("translation", { provider: event.target.value })}
            >
              <option value="none">Deterministic, no LLM</option>
              <option value="anthropic">Anthropic</option>
              <option value="google">Google Gemini</option>
              <option value="watsonx">IBM watsonx Orchestrate</option>
            </select>
          </label>
          {translation.provider !== "none" && (
            <SecretField
              label={`${translationProviderLabel(translation.provider)} API key`}
              value={translation.api_key}
              onChange={(api_key) => patch("translation", { api_key })}
            />
          )}
          {source.platform === "n8n" && (
            <TextField
              label="Orchestrate target model"
              value={target.model}
              onChange={(model) => patch("target", { model })}
              mono
            />
          )}
          {source.platform === "copilot-studio" && (
            <InlineNotice tone="success">
              The Foundry model matrix will choose the nearest tenant-supported model per agent.
            </InlineNotice>
          )}
        </section>

        <section className="configure-panel preflight-panel">
          <SectionLabel icon={ListFilter} title="Migration preflight" />
          <dl className="preflight-list">
            <PreflightRow label="Corridor" value={`${source.platform === "n8n" ? "n8n" : "Copilot Studio"} to watsonx Orchestrate`} />
            {source.platform === "copilot-studio" && (
              <PreflightRow label="Solutions" value={`${solutionCount || 1} source solution${solutionCount === 1 ? "" : "s"}`} />
            )}
            <PreflightRow label="Selection" value={`${selectedItems.length} ${source.platform === "n8n" ? "workflow" : "agent"}${selectedItems.length === 1 ? "" : "s"}`} />
            <PreflightRow label="Delivery" value={target.deploy ? "Deploy to target" : "Dry run and bundle"} />
            <PreflightRow label="Conflicts" value={target.on_conflict} />
            <PreflightRow label="Translation" value={translationProviderLabel(translation.provider)} />
          </dl>
          <div className="preflight-checks">
            <span><CheckCircle2 size={15} /> Source connection verified</span>
            <span><CheckCircle2 size={15} /> Target connection verified</span>
            <span><CheckCircle2 size={15} /> {selectedItems.length} item selection locked</span>
          </div>
        </section>
      </div>
      <div className="selected-manifest">
        <div>
          <strong>Launch manifest</strong>
          <small>{selectedItems.length} selected item{selectedItems.length === 1 ? "" : "s"}</small>
        </div>
        <div className="selected-preview">
          {selectedItems.map((item) => (
            <span key={item.id}>{item.name}</span>
          ))}
        </div>
      </div>
    </>
  );
}

function PreflightRow({ label, value }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function ExecuteStep({ session, run, execute, reset, logRef }) {
  const [reviewedConnectionCount, setReviewedConnectionCount] = useState(0);
  const selectedItems = session.source.items.filter((item) =>
    session.source.selected_ids.includes(item.id));
  const sourceName = session.source.platform === "n8n" ? "n8n" : "Copilot Studio";
  const active = ["starting", "running"].includes(run.phase);
  const finished = ["completed", "failed"].includes(run.phase);
  const connectionReviewTotal = run.summary?.connection_reviews?.length || 0;
  const needsConnectionReview = run.phase === "completed"
    && reviewedConnectionCount < connectionReviewTotal;

  useEffect(() => setReviewedConnectionCount(0), [run.jobId]);

  const copyLogs = async () => {
    const text = run.lines
      .map((line) => `${line.timestamp} [${line.stage}] ${line.message}`)
      .join("\n");
    await navigator.clipboard?.writeText(text);
  };

  return (
    <>
      <StepHeading eyebrow="Execute" title="Launch the migration">
        Follow every compiler, mapping, validation and deployment stage in real time.
      </StepHeading>
      {run.phase === "idle" ? (
        <div className="launch-ready">
          <div className="launch-hero">
            <span className="launch-icon"><Rocket size={28} /></span>
            <div>
              <strong>Ready for liftoff</strong>
              <p>{selectedItems.length} item{selectedItems.length === 1 ? "" : "s"} from {sourceName} will {session.target.deploy ? "compile and deploy" : "compile into a review bundle"}.</p>
            </div>
          </div>
          <dl className="run-summary">
            <PreflightRow label="Corridor" value={`${sourceName} to watsonx Orchestrate`} />
            <PreflightRow label="Selection" value={`${selectedItems.length} item${selectedItems.length === 1 ? "" : "s"}`} />
            <PreflightRow label="Delivery" value={session.target.deploy ? "Deploy" : "Dry run"} />
            <PreflightRow label="Name conflict" value={session.target.on_conflict} />
            <PreflightRow label="Translation" value={translationProviderLabel(session.translation.provider)} />
            <PreflightRow label="Target" value={session.target.instance_url} />
          </dl>
          <div className="execute-actions">
            <button className="btn btn-primary btn-run" onClick={execute}>
              <Play size={17} /> Start migration
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="run-status">
            <span className={`run-icon ${needsConnectionReview ? "review" : run.phase}`}>
              {active && <Loader2 size={20} className="spin" />}
              {run.phase === "completed" && (
                needsConnectionReview
                  ? <KeyRound size={20} />
                  : <CheckCircle2 size={20} />
              )}
              {run.phase === "failed" && <XCircle size={20} />}
            </span>
            <div>
              <strong>
                {active
                  ? "Migration in progress"
                  : needsConnectionReview
                    ? "Review connection credentials"
                    : run.phase === "completed"
                      ? "Migration complete"
                      : "Migration failed"}
              </strong>
              <small>{run.jobId ? `Run ${run.jobId.slice(0, 8)}` : "Starting service job"}</small>
            </div>
            <div className="run-stage-count">
              <strong>{run.lines.length}</strong>
              <small>events</small>
            </div>
          </div>
          <div className="terminal" ref={logRef}>
            <div className="terminal-bar">
              <TerminalSquare size={15} />
              <span>Live compiler output</span>
              {active && <span className="live-dot">live</span>}
              {run.lines.length > 0 && (
                <button title="Copy logs" aria-label="Copy logs" onClick={copyLogs}>
                  <Copy size={14} />
                </button>
              )}
            </div>
            <div className="terminal-body">
              {run.lines.length === 0 && (
                <div className="terminal-empty">Waiting for the first compiler event...</div>
              )}
              {run.lines.map((line) => (
                <div className="terminal-line" data-level={line.level} key={line.id}>
                  <span className="terminal-time">{line.timestamp}</span>
                  <span className="terminal-stage">{line.stage}</span>
                  <span className="terminal-mark">
                    {line.level === "error" ? <XCircle size={13} /> :
                      line.level === "warn" ? <AlertCircle size={13} /> :
                        line.level === "ok" ? <CheckCircle2 size={13} /> : <Circle size={8} />}
                  </span>
                  <span>{line.message}</span>
                </div>
              ))}
            </div>
          </div>
          {finished && (
            <ResultSummary
              run={run}
              reset={reset}
              session={session}
              onConnectionReviewProgress={setReviewedConnectionCount}
            />
          )}
        </>
      )}
    </>
  );
}

function ResultSummary({
  run,
  reset,
  session,
  onConnectionReviewProgress,
}) {
  const summary = run.summary || {};
  const agents = summary.agents || [];
  const followUp = summary.follow_up || [];
  const pendingTools = summary.pending_tools || [];
  const connectionReviews = summary.connection_reviews || [];
  const documents = summary.documents || [];
  return (
    <div className="result-stack">
      <div className={`result-summary ${run.phase}`}>
        <div>
          <strong>{summary.message || (
            summary.dry_run
              ? `${summary.processed || 0} item(s) compiled`
              : `${summary.deployed || 0} of ${summary.processed || 0} item(s) deployed`
          )}</strong>
          {summary.failed > 0 && <small>{summary.failed} failed. Review the output above.</small>}
          {followUp.length > 0 && (
            <small>{followUp.length} explicit follow-up step{followUp.length === 1 ? "" : "s"} below.</small>
          )}
        </div>
        <div className="result-actions">
          {run.phase === "completed" && run.jobId && (
            <a className="btn btn-primary" href={`/api/jobs/${run.jobId}/artifact`}>
              <Download size={16} /> Download results
            </a>
          )}
          <button className="btn btn-quiet" onClick={reset}>
            <RefreshCw size={16} /> New migration
          </button>
        </div>
      </div>

      {agents.length > 0 && (
        <section className="outcome-panel">
          <div className="outcome-heading">
            <SectionLabel icon={Bot} title="Migration results" />
            <span>{agents.length} agent{agents.length === 1 ? "" : "s"}</span>
          </div>
          <div className="outcome-list">
            {agents.map((agent) => (
              <div className="outcome-row" key={`${agent.id || ""}-${agent.name}`}>
                <span className="outcome-status" data-state={
                  agent.deployed === false ? "failed" : agent.deployed === true ? "deployed" : "compiled"
                }>
                  {agent.deployed === false
                    ? <XCircle size={16} />
                    : <CheckCircle2 size={16} />}
                </span>
                <span className="outcome-copy">
                  <strong>{agent.name}</strong>
                  <small>{agent.detail || (agent.deployed == null ? "Artifacts written" : "Imported")}</small>
                </span>
                <span className="outcome-metrics">
                  <small>{agent.tools?.length || 0} tool{agent.tools?.length === 1 ? "" : "s"} carried</small>
                  {agent.dropped?.length > 0 && (
                    <small className="metric-warning">{agent.dropped.length} omitted</small>
                  )}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {connectionReviews.length > 0 && (
        <ConnectionReviewPanel
          connections={connectionReviews}
          target={session.target}
          onProgress={onConnectionReviewProgress}
        />
      )}
      {summary.connection_review_error && (
        <InlineNotice tone="warning">{summary.connection_review_error}</InlineNotice>
      )}

      {(followUp.length > 0 || pendingTools.length > 0) && (
        <section className="outcome-panel follow-up-panel">
          <div className="outcome-heading">
            <SectionLabel icon={ListFilter} title="Post-migration checklist" />
            <span>{followUp.filter((step) => step.blocking).length} blocking</span>
          </div>
          <div className="follow-up-list">
            {followUp.map((step, index) => (
              <article className="follow-up-row" data-blocking={step.blocking} key={`${step.kind}-${index}`}>
                <span className="follow-up-number">{index + 1}</span>
                <div>
                  <strong>{step.title}</strong>
                  <p>{step.detail}</p>
                  <div className="follow-up-meta">
                    <span>{step.blocking ? "Required" : "Recommended"}</span>
                    {step.where && <span>{step.where}</span>}
                    {step.agents?.length > 0 && <span>{step.agents.join(", ")}</span>}
                  </div>
                  {step.command && <code>{step.command}</code>}
                </div>
              </article>
            ))}
            {followUp.length === 0 && pendingTools.map((tool, index) => (
              <article className="follow-up-row" data-blocking="true" key={tool.install_ref}>
                <span className="follow-up-number">{index + 1}</span>
                <div>
                  <strong>Install {tool.title}</strong>
                  <p>
                    Search for <code>{tool.install_ref}</code> in the Orchestrate tool catalog,
                    then configure {tool.connections?.join(", ") || "its required connection"}.
                  </p>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}
      {documents.length > 0 && run.jobId && (
        <section className="outcome-panel document-panel">
          <div className="outcome-heading">
            <SectionLabel icon={FileText} title="Evaluation pack" />
            <span>{documents.length} Markdown files</span>
          </div>
          <div className="document-list">
            {documents.map((document) => (
              <a
                className="document-row"
                href={`/api/jobs/${run.jobId}/documents/${encodeURIComponent(document.name)}`}
                key={document.name}
              >
                <FileText size={17} />
                <span>
                  <strong>{document.label}</strong>
                  <small>{document.name}</small>
                </span>
                <Download size={16} />
              </a>
            ))}
          </div>
        </section>
      )}
      {summary.notes?.length > 0 && (
        <section className="result-notes">
          {summary.notes.map((note) => <p key={note}>{note}</p>)}
        </section>
      )}
    </div>
  );
}

function ConnectionReviewPanel({ connections, target, onProgress }) {
  const [reviewed, setReviewed] = useState({});
  const reviewCount = Object.keys(reviewed).length;
  useEffect(() => onProgress(reviewCount), [onProgress, reviewCount]);

  return (
    <section className="outcome-panel credential-review-panel">
      <div className="outcome-heading">
        <SectionLabel icon={KeyRound} title="Connection credentials" />
        <span>{reviewCount} of {connections.length} reviewed</span>
      </div>
      <div className="credential-review-intro">
        Confirm every connection, including ones that are already ready. Existing secrets stay
        on the target and are never returned to Agent Liftoff.
      </div>
      <div className="credential-review-list">
        {connections.map((connection) => (
          <ConnectionReviewItem
            connection={connection}
            target={target}
            reviewed={reviewed[connection.app_id]}
            onReviewed={(decision) => setReviewed((current) => {
              const next = { ...current };
              if (decision) next[connection.app_id] = decision;
              else delete next[connection.app_id];
              return next;
            })}
            key={connection.app_id}
          />
        ))}
      </div>
    </section>
  );
}

function ConnectionReviewItem({ connection, target, reviewed, onReviewed }) {
  const [mode, setMode] = useState("choose");
  const [kind, setKind] = useState(connection.default_kind || "basic_auth");
  const [preference, setPreference] = useState(
    connection.preference === "member" ? "member" : "team",
  );
  const [serverUrl, setServerUrl] = useState(connection.server_url || "");
  const [credentials, setCredentials] = useState({});
  const [submission, setSubmission] = useState({ phase: "idle", message: "" });
  const option = connection.auth_options.find((item) => item.value === kind)
    || connection.auth_options[0];

  const chooseExisting = () => {
    setMode("complete");
    onReviewed(connection.ready ? "existing" : "later");
  };

  const submit = async () => {
    setSubmission({ phase: "loading", message: "" });
    try {
      const result = await configureConnection({
        target,
        app_id: connection.app_id,
        environment: connection.environment || "draft",
        kind,
        preference,
        server_url: serverUrl,
        credentials: preference === "team" ? credentials : {},
      });
      setCredentials({});
      setSubmission({ phase: "ready", message: result.message });
      setMode("complete");
      onReviewed("configured");
    } catch (error) {
      setSubmission({ phase: "error", message: error.message });
    }
  };

  return (
    <article className="credential-review-item" data-ready={connection.ready}>
      <div className="credential-review-head">
        <span>
          <strong>{connection.app_id}</strong>
          <small>Used by {connection.tools.join(", ")}</small>
        </span>
        <span className="credential-state">
          {connection.ready ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
          {connection.ready ? "Ready on target" : "Needs attention"}
        </span>
      </div>
      <p className="credential-summary">{connection.summary}</p>

      {mode === "choose" && (
        <div className="credential-choice">
          <button className="btn btn-secondary" onClick={chooseExisting}>
            <Check size={16} />
            {connection.ready ? "Use existing credential" : "Leave for later"}
          </button>
          <button className="btn btn-primary" onClick={() => setMode("configure")}>
            <KeyRound size={16} />
            {connection.ready ? "Use different credentials" : "Configure now"}
          </button>
        </div>
      )}

      {mode === "configure" && (
        <div className="credential-form">
          <div className="credential-form-grid">
            <label className="field">
              <span>Authentication</span>
              <select
                className="select"
                aria-label={`Authentication for ${connection.app_id}`}
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value);
                  setCredentials({});
                }}
              >
                {connection.auth_options.map((item) => (
                  <option value={item.value} key={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
            <TextField
              label={`Server URL for ${connection.app_id}`}
              value={serverUrl}
              onChange={setServerUrl}
              placeholder="https://your-system.example.com"
              mono
            />
          </div>
          <Segmented
            value={preference}
            onChange={setPreference}
            options={[
              { value: "team", label: "One shared credential" },
              { value: "member", label: "Each user signs in" },
            ]}
          />
          {preference === "team" && (
            <div className="credential-field-grid">
              {option.fields.map((field) => (
                field.secret ? (
                  <SecretField
                    label={`${field.label} for ${connection.app_id}`}
                    value={credentials[field.name] || ""}
                    onChange={(value) => setCredentials((current) => ({
                      ...current,
                      [field.name]: value,
                    }))}
                    key={field.name}
                  />
                ) : (
                  <TextField
                    label={`${field.label} for ${connection.app_id}`}
                    value={credentials[field.name] || ""}
                    onChange={(value) => setCredentials((current) => ({
                      ...current,
                      [field.name]: value,
                    }))}
                    key={field.name}
                  />
                )
              ))}
            </div>
          )}
          {preference === "member" && (
            <InlineNotice tone="success">
              Each user will be asked to sign in when the agent first uses this connection.
            </InlineNotice>
          )}
          {submission.phase === "error" && (
            <InlineNotice tone="error">{submission.message}</InlineNotice>
          )}
          <div className="credential-choice">
            <button
              className="btn btn-quiet"
              disabled={submission.phase === "loading"}
              onClick={() => setMode("choose")}
            >
              <ArrowLeft size={16} /> Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={submission.phase === "loading"}
              onClick={submit}
            >
              {submission.phase === "loading"
                ? <Loader2 size={16} className="spin" />
                : <KeyRound size={16} />}
              Apply once
            </button>
          </div>
        </div>
      )}

      {mode === "complete" && (
        <div className="credential-complete">
          <CheckCircle2 size={16} />
          <span>
            {reviewed === "configured"
              ? submission.message || "New credential submitted to the target."
              : reviewed === "existing"
              ? "Existing target credential confirmed."
              : "Left for manual configuration."}
          </span>
          <button
            className="btn btn-quiet btn-small"
            onClick={() => {
              setMode("choose");
              setSubmission({ phase: "idle", message: "" });
              onReviewed(null);
            }}
          >
            Change
          </button>
        </div>
      )}
    </article>
  );
}

function SectionLabel({ icon: Icon, title }) {
  return (
    <div className="section-label">
      <Icon size={17} />
      <h2>{title}</h2>
    </div>
  );
}

function TextField({ label, value, onChange, placeholder = "", mono = false }) {
  const id = useMemo(() => `field-${label.toLowerCase().replace(/\W+/g, "-")}`, [label]);
  return (
    <label className="field" htmlFor={id}>
      <span>{label}</span>
      <input
        id={id}
        className={mono ? "mono" : ""}
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function SecretField({ label, value, onChange, optional = false }) {
  const [visible, setVisible] = useState(false);
  const id = useMemo(() => `secret-${label.toLowerCase().replace(/\W+/g, "-")}`, [label]);
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label} {optional && <small>optional</small>}
      </label>
      <span className="secret-input">
        <input
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          aria-label={visible ? `Hide ${label}` : `Show ${label}`}
          title={visible ? `Hide ${label}` : `Show ${label}`}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
      </span>
    </div>
  );
}

function UploadField({ platform, files, onChange }) {
  const accept = platform === "n8n" ? ".json,application/json" : ".zip,application/zip";
  const multiple = platform === "n8n";
  return (
    <label className="upload-field">
      <input
        type="file"
        accept={accept}
        multiple={multiple}
        onChange={(event) => onChange([...event.target.files])}
      />
      <span className="upload-icon"><FileArchive size={23} /></span>
      <span>
        <strong>
          {files.length
            ? `${files.length} file${files.length === 1 ? "" : "s"} selected`
            : "Choose export files"}
        </strong>
        <small>
          {platform === "n8n"
            ? "One or more workflow JSON files"
            : "One PAC-unpacked solution ZIP"}
        </small>
      </span>
      <span className="btn btn-secondary">Browse</span>
    </label>
  );
}

function Segmented({ value, onChange, options }) {
  return (
    <div className="segmented">
      {options.map((option) => {
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            data-selected={value === option.value}
            onClick={() => onChange(option.value)}
          >
            {Icon && <Icon size={15} />}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <button
      className="toggle-wrap"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
    >
      <span className="toggle"><span /></span>
      <strong>{label}</strong>
    </button>
  );
}

function InlineNotice({ tone, children }) {
  const Icon = tone === "error" ? XCircle : tone === "warning" ? AlertCircle : CheckCircle2;
  return (
    <div className="inline-notice" data-tone={tone}>
      <Icon size={15} />
      <span>{children}</span>
    </div>
  );
}

function StatusDot({ ready }) {
  return <span className="status-dot" data-ready={ready} />;
}

function EmptyState({ icon: Icon, title, detail }) {
  return (
    <div className="empty-state">
      <Icon size={24} />
      <div><strong>{title}</strong><p>{detail}</p></div>
    </div>
  );
}

function naturalCompare(a = "", b = "") {
  return String(a).localeCompare(String(b), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

function translationProviderLabel(provider) {
  return {
    none: "Deterministic",
    anthropic: "Anthropic",
    google: "Google Gemini",
    watsonx: "IBM watsonx Orchestrate",
  }[provider] || provider;
}

function stripUiSource(source) {
  const {
    account_name,
    environments,
    items,
    solutions,
    selected_solution_ids,
    ...apiSource
  } = source;
  return apiSource;
}
