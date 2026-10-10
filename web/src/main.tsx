import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";
import "./app-shell.css";
import "./components.css";
import "./chat.css";
import "./agents.css";
import "./goals.css";
import "./collaboration.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
