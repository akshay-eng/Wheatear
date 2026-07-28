import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "@fontsource-variable/archivo";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./styles.css";

createRoot(document.getElementById("root")).render(<App />);
