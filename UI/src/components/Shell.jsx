/* App frame: sidebar (brand + workspaces), topbar (crumbs + theme), toasts. */

import { Check, ChevronRight, Globe, Layers, Menu, Moon, Plus, Sun, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { go, useStore, useTheme } from "../store.jsx";

export function Sidebar({ route, open, onClose }) {
  const { state } = useStore();
  return (
    <aside className={`sidebar${open ? " open" : ""}`} id="sidebar">
      <div className="sidebar-brand">
        <img src="./wheatear-mark-64.png" alt="" width={28} height={28} />
        <div className="wordmark">Wheatear<small>CONSOLE</small></div>
      </div>
      <div className="sidebar-section">
        <span>Workspaces</span>
        <button
          className="icon-btn"
          aria-label="New workspace"
          title="New workspace"
          onClick={() => {
            go("#/");
            onClose();
            setTimeout(() => document.getElementById("new-ws-name")?.focus(), 80);
          }}
        >
          <Plus size={15} />
        </button>
      </div>
      <nav aria-label="Workspaces">
        {state.workspaces.map((w) => (
          <a
            key={w.id}
            className="nav-item"
            href={`#/w/${w.id}`}
            aria-current={route.wsId === w.id}
            onClick={onClose}
          >
            <Layers size={15} />
            <span className="label">{w.name}</span>
            <span className="count">{w.projects.length}</span>
          </a>
        ))}
        {state.workspaces.length === 0 && <span className="nav-empty">None yet</span>}
      </nav>
      <div className="sidebar-foot">
        <span className="ver">wheatear v0.1.0</span>
        <a href="../index.html" className="icon-btn" aria-label="Project site" title="Project site">
          <Globe size={15} />
        </a>
      </div>
    </aside>
  );
}

export function Topbar({ crumbs, onMenu }) {
  const [theme, toggleTheme] = useTheme();
  return (
    <header className="topbar">
      <button className="icon-btn menu-btn" aria-label="Open navigation" onClick={onMenu}>
        <Menu size={17} />
      </button>
      <nav className="crumbs" aria-label="Breadcrumb">
        {crumbs.map((c, i) => {
          const last = i === crumbs.length - 1;
          return (
            <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
              {i > 0 && <span className="sep"><ChevronRight size={13} /></span>}
              {last ? <span className="here">{c.label}</span> : <a href={c.href}>{c.label}</a>}
            </span>
          );
        })}
      </nav>
      <div className="topbar-actions">
        <button className="icon-btn theme-toggle" aria-label="Toggle color theme" onClick={toggleTheme}>
          {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
        </button>
      </div>
    </header>
  );
}

export function Toasts() {
  const { toasts } = useStore();
  return (
    <div className="toast-zone" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast${t.leaving ? " leaving" : ""}`}>
          {t.kind === "trash" ? <Trash2 size={16} /> : <Check size={16} />}
          <span>{t.msg}</span>
        </div>
      ))}
    </div>
  );
}

/* Content wrapper that replays the enter animation on route change. */
export function ViewFrame({ routeKey, children }) {
  const [key, setKey] = useState(routeKey);
  useEffect(() => setKey(routeKey), [routeKey]);
  return (
    <div className="content">
      <div className="content-inner view-enter" key={key}>
        {children}
      </div>
    </div>
  );
}
