import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import { applyStoredTheme } from "./theme";

// Before first paint, so the initial render is already in the right theme.
applyStoredTheme();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
