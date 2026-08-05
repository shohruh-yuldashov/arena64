import "@/app/styles/globals.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";

/**
 * The browser entry point, and the only file that touches the DOM directly.
 *
 * `StrictMode` stays on in development: it double-invokes renders and
 * effects to surface the impure ones, which is exactly the class of bug
 * that otherwise appears months later as "it works except on the second
 * navigation". It is stripped from production builds by React itself.
 *
 * The `#root` lookup throws rather than falling back. A missing mount
 * point means `index.html` and this file have disagreed, and a silent
 * `return` would leave a blank page with an empty console.
 */
const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html is missing its #root element — nothing to mount into.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
