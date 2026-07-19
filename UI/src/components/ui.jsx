/* Shared primitives — one component vocabulary across every screen. */

import { AlertTriangle, Check, ChevronRight, ArrowRight, Trash2 } from "lucide-react";
import { useState } from "react";
import { PLATFORMS } from "../lib/catalog.js";

export function Btn({ variant = "secondary", sm, children, ...rest }) {
  return (
    <button className={`btn btn-${variant}${sm ? " btn-sm" : ""}`} {...rest}>
      {children}
    </button>
  );
}

export function IconBtn({ label, children, ...rest }) {
  return (
    <button className="icon-btn" aria-label={label} title={label} {...rest}>
      {children}
    </button>
  );
}

export function Pill({ tone = "muted", children }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function StatusPill({ project }) {
  if (project.status === "running") return <Pill tone="accent"><span className="dot" />Running</Pill>;
  if (project.status === "attention") return <Pill tone="warn"><AlertTriangle size={12} /> Needs review</Pill>;
  if (project.status === "complete") return <Pill tone="ok"><Check size={12} /> Complete</Pill>;
  return <Pill>Draft</Pill>;
}

export function Corridor({ project }) {
  const src = PLATFORMS[project.source]?.short || project.source;
  const tgt = PLATFORMS[project.target]?.short || project.target;
  return (
    <span className="corridor">
      {src} <ArrowRight size={13} /> {tgt}
    </span>
  );
}

export function Field({ label, htmlFor, hint, hintIcon: HintIcon, error, children }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {error && <p className="error-msg">{error}</p>}
      {hint && (
        <p className="hint">
          {HintIcon && <HintIcon size={13} />}
          <span>{hint}</span>
        </p>
      )}
    </div>
  );
}

export function Choice({ checked, onPick, glyph: Glyph, title, desc, descMono, disabled, badge }) {
  return (
    <button
      className="choice"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onPick}
    >
      <span className="glyph">{Glyph && <Glyph size={18} />}</span>
      <span className="c-body">
        <span className="c-title">{title}{badge}</span>
        <span className={`c-desc${descMono ? " mono" : ""}`} style={descMono ? { fontSize: "var(--text-xs)" } : undefined}>{desc}</span>
      </span>
      <span className="radio" />
    </button>
  );
}

export function Empty({ glyph: Glyph, title, children, action }) {
  return (
    <div className="empty">
      <div className="e-glyph">{Glyph && <Glyph size={22} />}</div>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}

export function RowChevron() {
  return <ChevronRight size={16} aria-hidden />;
}

/* Delete button that arms on first click — no modal needed. */
export function ArmedDelete({ label, onConfirm, asIcon = true }) {
  const [armed, setArmed] = useState(false);
  const click = (e) => {
    e.stopPropagation();
    if (armed) { onConfirm(); return; }
    setArmed(true);
    setTimeout(() => setArmed(false), 2500);
  };
  if (asIcon) {
    return (
      <button
        className="icon-btn"
        style={armed ? { color: "var(--danger)" } : undefined}
        aria-label={armed ? "Click again to confirm delete" : label}
        title={armed ? "Click again to confirm" : label}
        onClick={click}
      >
        <Trash2 size={15} />
      </button>
    );
  }
  return (
    <button className="btn btn-danger" onClick={click}>
      <Trash2 size={15} /> {armed ? "Click again to confirm" : label}
    </button>
  );
}
