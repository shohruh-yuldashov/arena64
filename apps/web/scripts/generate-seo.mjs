#!/usr/bin/env node
/**
 * Finishes the built bundle's SEO — A64-026.3 §42.
 *
 * Runs after `vite build`, over `dist/`. Three things need a value this
 * repository cannot know until something deploys it — the origin the site
 * is served from — and this is where that value is applied:
 *
 *     <link rel="canonical">      which URL this content belongs to
 *     og:url                      the same, for a share preview
 *     og:image                    absolute, which several crawlers require
 *     sitemap.xml                 a list of absolute URLs, by definition
 *     JSON-LD                     `url` is most of what makes it useful
 *
 * ## Why a post-build script rather than a Vite plugin
 *
 * A plugin would live in `vite.config.ts`, and this workspace's config
 * carries per-developer entries that are not committed. A script the build
 * calls needs no edit there, is readable on its own, and can be run again
 * over an existing `dist/` while looking at the result.
 *
 * ## No origin means no production build
 *
 * If `VITE_PUBLIC_ORIGIN` is unset, none of the above is written **and the
 * robots policy is replaced with `Disallow: /`**. That is the safe reading:
 * a build that cannot say which URL its content belongs to is a preview, a
 * staging deploy or somebody's laptop, and none of those should be indexed.
 * A canonical guessed from `localhost` would be worse than absent — it
 * tells a crawler to index a page that is not there.
 *
 * Usage: `npm run build` (or `node scripts/generate-seo.mjs` over a dist).
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const DIST = join(ROOT, "dist");

/**
 * The one page meant to be found.
 *
 * Everything else on this origin is behind authentication or is a form,
 * and `public/robots.txt` says so in full. A sitemap listing a URL that
 * robots.txt disallows is a contradiction a crawler resolves by trusting
 * neither, so this list and that file have to agree — the test asserts it.
 */
const INDEXABLE = ["/"];

/**
 * The origin, if somebody configured one.
 *
 * `VITE_PUBLIC_ORIGIN` is the frontend's name for the value the backend
 * already calls `PUBLIC_APP_URL`. They must be the same string in a given
 * deployment: that one is what an emailed reset link points at and this one
 * is what a canonical claims, and a site whose mail and whose canonical
 * disagree about its own address is a site with two identities.
 */
function readOrigin() {
  const raw = process.env.VITE_PUBLIC_ORIGIN?.trim();
  if (!raw) return null;

  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`VITE_PUBLIC_ORIGIN is not a URL: ${raw}`);
  }
  if (url.protocol !== "https:" && url.hostname !== "localhost") {
    throw new Error(
      `VITE_PUBLIC_ORIGIN must be https for anything but localhost: ${raw}. ` +
        "A canonical on http tells a crawler the http page is the real one.",
    );
  }
  // No trailing slash, so every join below is `origin + path` and there is
  // one spelling of every URL this site publishes about itself.
  return url.origin;
}

/** `<meta property="og:image" content="/og-card.png">` and friends. */
function absolute(origin, path) {
  return `${origin}${path}`;
}

function injectHead(html, origin) {
  const tags = [
    `<link rel="canonical" href="${absolute(origin, "/")}" />`,
    `<meta property="og:url" content="${absolute(origin, "/")}" />`,
  ].join("\n    ");

  let out = html;

  // The relative `og:image` becomes absolute. Facebook and several others
  // do not resolve a relative one against the page, so the tag that was
  // shipped in A64-026.2 was correct for some clients and invisible to the
  // rest — §41.5 recorded exactly this as the thing an origin unblocks.
  out = out.replace(
    /(<meta property="og:image" content=")(\/[^"]+)(")/,
    (_, before, path, after) => `${before}${absolute(origin, path)}${after}`,
  );

  // `id` so a second run over the same `dist/` replaces rather than repeats.
  const jsonLd = `<script type="application/ld+json" id="arena64-jsonld">${JSON.stringify(
    structuredData(origin),
  )}</script>`;

  out = out.replace(/\n\s*<script type="application\/ld\+json"[^<]*<\/script>/, "");
  out = out.replace(/\n\s*<link rel="canonical"[^>]*>/, "");
  out = out.replace(/\n\s*<meta property="og:url"[^>]*>/, "");

  return out.replace("</head>", `  ${tags}\n    ${jsonLd}\n  </head>`);
}

/**
 * The smallest structured data that is entirely true.
 *
 * `WebSite` and `WebApplication`, and nothing else. There is no
 * `aggregateRating` because nobody has rated this, no `offers` because
 * nothing is sold, no `author` because a platform is not authored, and no
 * `SearchAction` because this site has no public search endpoint a crawler
 * could use — `/search` is behind authentication.
 *
 * `applicationCategory` is `GameApplication`, which is what schema.org
 * means by it. `operatingSystem: "Web"` is the honest answer for something
 * with no native build; A64-027.3 owns the install experience, and if a
 * store listing ever exists this is where it would be said.
 */
function structuredData(origin) {
  const description =
    "Rus shashkasi onlayn. Bir necha soniyada raqib toping, reytingli yoki " +
    "oddiy o'ynang, turnirlarda qatnashing.";

  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "WebSite",
        "@id": `${origin}/#website`,
        url: `${origin}/`,
        name: "Arena64",
        description,
        inLanguage: ["uz", "ru", "en"],
      },
      {
        "@type": "WebApplication",
        "@id": `${origin}/#app`,
        url: `${origin}/`,
        name: "Arena64",
        description,
        applicationCategory: "GameApplication",
        operatingSystem: "Web",
        isPartOf: { "@id": `${origin}/#website` },
      },
    ],
  };
}

function sitemap(origin) {
  const urls = INDEXABLE.map(
    (path) => `  <url>\n    <loc>${absolute(origin, path)}</loc>\n  </url>`,
  ).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`;
}

/**
 * What a build with no origin publishes instead of a policy.
 *
 * Every agent, everything. A preview deploy that is crawled is a duplicate
 * of production with a different address, which is the one thing a canonical
 * exists to prevent — and this build has no canonical to prevent it with.
 */
const BLOCK_EVERYTHING = `# Arena64 — built with no VITE_PUBLIC_ORIGIN.
#
# A build that cannot say which URL its content belongs to is a preview, a
# staging deploy or a developer's machine. None of those should be indexed,
# and this file is replaced rather than shipped as the production policy so
# that forgetting the variable fails closed. See A64-026.3 §42.4.

User-agent: *
Disallow: /
`;

/**
 * Exported so `seo.test.ts` can assert the policy without a `dist/`.
 *
 * The alternative was a test that runs a build, which would be slow, would
 * fail for reasons unrelated to SEO, and would still only check the same
 * three strings this exports.
 */
export { INDEXABLE, BLOCK_EVERYTHING, readOrigin, injectHead, sitemap, structuredData };

function main() {
  if (!existsSync(DIST)) {
    throw new Error(`No build at ${DIST}. Run \`vite build\` first.`);
  }

  const robotsPath = join(DIST, "robots.txt");
  const indexPath = join(DIST, "index.html");
  const origin = readOrigin();

  if (origin === null) {
    writeFileSync(robotsPath, BLOCK_EVERYTHING);
    console.log("seo: no VITE_PUBLIC_ORIGIN — robots.txt blocks everything");
    console.log("seo: no canonical, no og:url, no sitemap, no structured data");
    return;
  }

  writeFileSync(indexPath, injectHead(readFileSync(indexPath, "utf8"), origin));
  writeFileSync(join(DIST, "sitemap.xml"), sitemap(origin));

  const robots = readFileSync(robotsPath, "utf8").replace(/\n*# Sitemap[\s\S]*$/, "\n");
  writeFileSync(
    robotsPath,
    `${robots}\n# Written by scripts/generate-seo.mjs, which is the only thing that\n# knows this deployment's origin.\nSitemap: ${absolute(origin, "/sitemap.xml")}\n`,
  );

  console.log(`seo: origin ${origin}`);
  console.log(`seo: canonical, og:url, absolute og:image and JSON-LD in index.html`);
  console.log(`seo: sitemap.xml with ${INDEXABLE.length} url(s), Sitemap line in robots.txt`);
}

// Only when run, not when imported by a test.
if (process.argv[1] === fileURLToPath(import.meta.url)) main();
