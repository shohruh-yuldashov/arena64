import "@/app/styles/globals.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";
import { registerServiceWorker, watchInstallability } from "@/shared/pwa";

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

/**
 * A64-020.9 §16. **Before** React mounts, not from an effect.
 *
 * `beforeinstallprompt` fires when the browser decides the site is
 * installable, which can be during the first paint — a listener attached
 * from a component effect misses it, and the event does not fire again.
 * This is the reason the install state lives in a module rather than in a
 * provider.
 */
watchInstallability();

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

/**
 * A64-020.9 §8, §32. The service worker, registered from the real
 * application bootstrap and nowhere else.
 *
 * **After** `render`, deliberately: registration competes for the network
 * with the chunks the first screen needs, and the shell has nothing to
 * gain from a worker that arrives a few hundred milliseconds sooner.
 *
 * `registerServiceWorker` is a no-op in development (`import.meta.env.PROD`
 * is false) and never throws — a browser that refuses it must still get a
 * working web application (§26).
 */
void registerServiceWorker();
