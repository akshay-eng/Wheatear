import { useEffect, useState } from "react";
import { Sidebar, Toasts, Topbar, ViewFrame } from "./components/Shell.jsx";
import Home from "./views/Home.jsx";
import Project from "./views/Project.jsx";
import Wizard from "./views/Wizard.jsx";
import Workspace from "./views/Workspace.jsx";
import { go, useHashRoute, useStore } from "./store.jsx";

export default function App() {
  const route = useHashRoute();
  const { state } = useStore();
  const [navOpen, setNavOpen] = useState(false);

  const ws = route.wsId ? state.workspaces.find((w) => w.id === route.wsId) : null;
  const project = ws && route.pId ? ws.projects.find((p) => p.id === route.pId) : null;

  // Guard bad deep links (deleted workspace/project) — bounce up a level.
  useEffect(() => {
    if (route.wsId && !ws) go("#/");
    else if (route.pId && !project) go(`#/w/${route.wsId}`);
  }, [route, ws, project]);

  const crumbs = [{ label: "Workspaces", href: "#/" }];
  if (ws) crumbs.push({ label: ws.name, href: `#/w/${ws.id}` });
  if (route.view === "wizard") crumbs.push({ label: "New migration" });
  if (project) crumbs.push({ label: project.name });

  let view = null;
  if (route.view === "home") view = <Home />;
  else if (route.view === "workspace" && ws) view = <Workspace ws={ws} />;
  else if (route.view === "wizard" && ws) view = <Wizard ws={ws} key={ws.id} />;
  else if (route.view === "project" && ws && project) view = <Project ws={ws} project={project} />;

  return (
    <div className="app">
      <Sidebar route={route} open={navOpen} onClose={() => setNavOpen(false)} />
      {navOpen && <div className="scrim show" onClick={() => setNavOpen(false)} />}
      <div className="main">
        <Topbar crumbs={crumbs} onMenu={() => setNavOpen(true)} />
        <ViewFrame routeKey={location.hash}>{view}</ViewFrame>
      </div>
      <Toasts />
    </div>
  );
}
