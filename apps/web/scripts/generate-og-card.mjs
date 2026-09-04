#!/usr/bin/env node
/**
 * Renders the Arena64 social preview card into `public/og-card.png`.
 *
 * ## Why this one uses a browser and `generate-icons.mjs` does not
 *
 * That script draws two rectangles and a circle, so it writes PNG bytes
 * directly and needs no library. This card has **type** on it, and drawing
 * type without a rasteriser means shipping glyph outlines — a font file, a
 * parser, and a second description of the wordmark that would drift from
 * the one the application renders.
 *
 * Playwright is already a dev dependency of this workspace, and it renders
 * the card from the **real stylesheet**: `globals.css` is imported, so the
 * tokens, the brand gradient and the type are the ones the product uses. A
 * card built any other way is a second design system with one consumer.
 *
 * That is also why the output is committed rather than built: a share
 * preview is fetched by a crawler that never runs `npm run build`, and a
 * `public/` asset is the only kind it can read.
 *
 * ## 1200×630
 *
 * The size every platform crops from. Nothing important sits within 60px of
 * an edge, because the crop varies and the parts that get cut are the ones
 * nobody chose.
 *
 * Usage: `npm run assets:og`
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "public", "og-card.png");

/**
 * The card, as a document.
 *
 * `globals.css` is linked rather than copied, which is the whole point: the
 * gradient, the radius and the type all come from the same file the running
 * application reads, so a change to the brand reaches this card by running
 * the script rather than by somebody remembering it exists.
 *
 * The board is the same 2×2 mark the icons carry, drawn in the same two
 * tones — one identity across a favicon, an installed tile and a share
 * preview.
 */
const HTML = `<!doctype html>
<html class="dark">
  <head>
    <meta charset="utf-8" />
    <link rel="stylesheet" href="/src/app/styles/globals.css" />
    <style>
      html, body { margin: 0; padding: 0; }
      body {
        width: 1200px;
        height: 630px;
        background: var(--background);
        color: var(--foreground);
        font-family: var(--font-sans, system-ui, sans-serif);
        display: grid;
        grid-template-columns: 1fr auto;
        align-items: center;
        gap: 72px;
        padding: 88px;
        box-sizing: border-box;
        overflow: hidden;
      }
      .grid {
        position: absolute; inset: 0;
        background-image:
          linear-gradient(var(--primary) 1px, transparent 1px),
          linear-gradient(90deg, var(--primary) 1px, transparent 1px);
        background-size: 64px 64px;
        opacity: 0.06;
        mask-image: radial-gradient(120% 80% at 72% 0%, black, transparent 70%);
      }
      .wordmark {
        font-size: 34px; font-weight: 600; letter-spacing: -0.01em;
        background-image: linear-gradient(115deg, var(--brand-from), var(--brand-to));
        background-clip: text; -webkit-background-clip: text; color: transparent;
      }
      h1 {
        margin: 22px 0 0; font-size: 62px; line-height: 1.06;
        font-weight: 600; letter-spacing: -0.025em; text-wrap: balance;
      }
      p { margin: 26px 0 0; font-size: 27px; line-height: 1.45; color: var(--muted-foreground); max-width: 22ch; }
      .mark {
        width: 300px; height: 300px; border-radius: 42px; overflow: hidden;
        display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr;
        background: #202268; position: relative;
        box-shadow: 0 24px 70px rgb(0 0 0 / 0.45);
      }
      .brand-sq { background: #494fcc; }
      .piece {
        position: absolute; left: 12%; top: 62%; width: 26%; aspect-ratio: 1;
        border-radius: 50%; background: #fafafa;
      }
      .content { position: relative; }
    </style>
  </head>
  <body>
    <div class="grid"></div>
    <div class="content">
      <div class="wordmark">Arena64</div>
      <h1>__TITLE__</h1>
      <p>__BODY__</p>
    </div>
    <div class="mark">
      <div class="brand-sq"></div><div></div>
      <div></div><div class="brand-sq"></div>
      <div class="piece"></div>
    </div>
  </body>
</html>`;

/**
 * The card's words, in Uzbek.
 *
 * The same language `index.html` declares and the landing page's metadata
 * uses, and for the same reason: a crawler and a share preview read one
 * value each, before any script has chosen a locale. A card per language is
 * A64-026.3's problem, once there is a way to serve one.
 */
const TITLE = "Onlayn shashka, tirik raqiblar bilan";
const BODY = "Reytingli o'yinlar, turnirlar va do'stlar bilan chorlovlar.";

const server = process.env.ARENA64_DEV_ORIGIN ?? "http://localhost:5173";

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1200, height: 630 },
    deviceScaleFactor: 1,
  });

  // Navigated to the dev server first so the stylesheet's absolute path
  // resolves. The script therefore needs `npm run dev` running, which is
  // stated in the failure below rather than left as a blank page.
  const response = await page.goto(server).catch(() => null);
  if (!response?.ok()) {
    throw new Error(
      `The dev server is not answering at ${server}. Start it with \`npm run dev\`, ` +
        "or set ARENA64_DEV_ORIGIN. This script renders the card from the real " +
        "stylesheet, which has to be served to be read.",
    );
  }

  await page.setContent(HTML.replace("__TITLE__", TITLE).replace("__BODY__", BODY), {
    waitUntil: "networkidle",
  });
  const png = await page.screenshot({ type: "png" });

  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, png);
  console.log(`og-card.png             1200×630  ${png.length} bytes`);
} finally {
  await browser.close();
}
