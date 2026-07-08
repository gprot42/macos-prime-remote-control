import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Window is created visible by default (with backgroundColor preventing white flash).
// No manual show() needed; this restores normal macOS focus/activation behavior on launch.
