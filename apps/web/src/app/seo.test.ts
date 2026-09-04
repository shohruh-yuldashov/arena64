import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it } from "vitest";

import {
  BLOCK_EVERYTHING,
  INDEXABLE,
  injectHead,
  readOrigin,
  sitemap,
  structuredData,
} from "../../scripts/generate-seo.mjs";

/**
 * What Arena64 publishes about itself — A64-026.3 §42.
 *
 * ## Why the policy is tested and the copy is not
 *
 * A title and a description are read by a person and reviewed by reading
 * them. What a test can hold is the part that is *silently* wrong: a
 * private route leaking into a sitemap, a canonical built from `localhost`,
 * a robots policy that stops disallowing something, or structured data that
 * grows a field nobody can substantiate.
 *
 * Every one of those is invisible in a browser and visible only to a
 * crawler, which is to say visible only once it is too late.
 */

const ORIGIN = "https://arena64.gg";
// Relative to the workspace root, which is where vitest runs — the same
// way `design-system.test.tsx` reads `globals.css`. `import.meta.url` is an
// http URL under Vite, so `fileURLToPath` cannot be used here.
const ROBOTS = readFileSync("public/robots.txt", "utf8");

/**
 * The route prefixes `public/robots.txt` must keep out.
 *
 * Three different reasons, and they are not interchangeable: forms nobody
 * should land on from a search result, routes behind authentication, and —
 * since A64-026.4 — routes that are **public and shareable and still not
 * indexed**. The two tests below pin the second and third by name, because
 * a list alone cannot say which is which.
 */
const DISALLOWED = [
  "/login",
  "/register",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
  "/profile",
  "/players/",
  "/settings/",
  "/friends",
  "/challenges",
  "/search",
  "/play",
  "/games/",
  "/notifications",
  "/tournaments",
  "/api/",
];

function disallowedPaths(robots: string): string[] {
  return robots
    .split("\n")
    .filter((line) => line.startsWith("Disallow:"))
    .map((line) => line.slice("Disallow:".length).trim());
}

afterEach(() => {
  delete process.env.VITE_PUBLIC_ORIGIN;
});

describe("the robots policy", () => {
  it("keeps every authenticated and account route out", () => {
    // Not "some of them". A route added to the router and forgotten here is
    // a route a crawler walks into, and the only thing it can index is the
    // login form it gets redirected to.
    const disallowed = disallowedPaths(ROBOTS);
    for (const path of DISALLOWED) expect(disallowed).toContain(path);
  });

  it("does not disallow the landing page", () => {
    // The one page meant to be found. `Disallow: /` here would be the whole
    // task undone in one line, and it is one line away — the no-origin
    // policy is exactly that string.
    expect(disallowedPaths(ROBOTS)).not.toContain("/");
  });

  it("keeps player profiles out, which is a decision rather than an omission", () => {
    // Public to view, not for indexing. The privacy settings a player has
    // control who sees what; none of them says "and list me in a search
    // engine". Removal from an index takes months, so the default that can
    // be reversed is the one that stays.
    expect(disallowedPaths(ROBOTS)).toContain("/players/");
  });

  it("keeps tournaments out even though they are public — §43.7", () => {
    // The row most likely to be "fixed" by somebody who reads the router,
    // sees an open route, and takes the entry for a leftover. It is not.
    //
    // Public, shareable and not indexed are three different things. Every
    // route here serves one `index.html` with one title and one
    // description, so an indexed tournament would enter the index as a
    // copy of the landing page — thousands of duplicates diluting the one
    // page meant to be found. The blocker is the missing per-route
    // metadata layer, not the visibility.
    expect(disallowedPaths(ROBOTS)).toContain("/tournaments");
  });
});

describe("the sitemap", () => {
  it("lists the landing page and nothing else", () => {
    expect(INDEXABLE).toEqual(["/"]);
  });

  it("never lists a path the robots policy disallows", () => {
    // A sitemap and a robots policy that disagree is a contradiction a
    // crawler resolves by trusting neither. This is the assertion that
    // keeps the two files one decision.
    const disallowed = disallowedPaths(ROBOTS);
    for (const path of INDEXABLE) {
      expect(disallowed.some((rule) => rule !== "" && path.startsWith(rule))).toBe(false);
    }
  });

  it("emits absolute URLs on the configured origin", () => {
    const xml = sitemap(ORIGIN);
    expect(xml).toContain("<loc>https://arena64.gg/</loc>");
    expect(xml).toContain('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"');
    expect(xml).not.toContain("localhost");
  });
});

describe("the origin", () => {
  it("is absent until something configures it", () => {
    // And absence is the whole safety mechanism: no origin means no
    // canonical, no sitemap, and a robots policy that blocks everything.
    expect(readOrigin()).toBeNull();
    expect(BLOCK_EVERYTHING).toContain("Disallow: /");
  });

  it("refuses http for anything but localhost", () => {
    // A canonical on `http` tells a crawler the insecure page is the real
    // one, which is a redirect loop away from being an outage.
    process.env.VITE_PUBLIC_ORIGIN = "http://arena64.gg";
    expect(() => readOrigin()).toThrow(/https/);

    process.env.VITE_PUBLIC_ORIGIN = "http://localhost:4173";
    expect(readOrigin()).toBe("http://localhost:4173");
  });

  it("drops a trailing slash so there is one spelling of every URL", () => {
    process.env.VITE_PUBLIC_ORIGIN = "https://arena64.gg/";
    expect(readOrigin()).toBe("https://arena64.gg");
  });

  it("refuses a value that is not a URL rather than building a broken one", () => {
    process.env.VITE_PUBLIC_ORIGIN = "arena64.gg";
    expect(() => readOrigin()).toThrow(/not a URL/);
  });
});

describe("the injected head", () => {
  const HTML = `<html><head>
    <title>Arena64</title>
    <meta property="og:image" content="/og-card.png" />
  </head><body></body></html>`;

  it("makes the social image absolute", () => {
    // A64-026.2 §41.5 shipped it relative and recorded that several
    // crawlers will not resolve one. This is the thing an origin unblocks.
    expect(injectHead(HTML, ORIGIN)).toContain(
      '<meta property="og:image" content="https://arena64.gg/og-card.png" />',
    );
  });

  it("states the canonical and the share URL", () => {
    const out = injectHead(HTML, ORIGIN);
    expect(out).toContain('<link rel="canonical" href="https://arena64.gg/" />');
    expect(out).toContain('<meta property="og:url" content="https://arena64.gg/" />');
  });

  it("replaces rather than repeats when run twice over one build", () => {
    // The script is runnable against an existing `dist/`, which is how it
    // is inspected. Doing that twice must not leave two canonicals, which
    // a crawler treats as none.
    const twice = injectHead(injectHead(HTML, ORIGIN), ORIGIN);
    expect(twice.match(/rel="canonical"/g)).toHaveLength(1);
    expect(twice.match(/application\/ld\+json/g)).toHaveLength(1);
  });
});

describe("the structured data", () => {
  it("claims only what can be substantiated", () => {
    // The failure mode of JSON-LD is a field that is easy to write and
    // impossible to defend. None of these has a source, so none is here —
    // and a rich result built on an invented rating is a manual action
    // against the domain, not a nicer search listing.
    const serialised = JSON.stringify(structuredData(ORIGIN));
    for (const forbidden of [
      "aggregateRating",
      "ratingValue",
      "review",
      "offers",
      "price",
      "award",
      "author",
      "interactionCount",
    ]) {
      expect(serialised).not.toContain(forbidden);
    }
  });

  it("describes a website and a web application, and says so correctly", () => {
    const data = structuredData(ORIGIN);
    expect(data["@context"]).toBe("https://schema.org");
    expect(data["@graph"].map((node) => node["@type"])).toEqual(["WebSite", "WebApplication"]);

    const app = data["@graph"].find((node) => node["@type"] === "WebApplication");
    expect(app).toBeDefined();
    expect(app?.applicationCategory).toBe("GameApplication");
    expect(app?.operatingSystem).toBe("Web");
    expect(app?.url).toBe("https://arena64.gg/");
  });

  it("carries no localhost and no private path", () => {
    const serialised = JSON.stringify(structuredData(ORIGIN));
    expect(serialised).not.toContain("localhost");
    for (const path of DISALLOWED) {
      expect(serialised).not.toContain(`${ORIGIN}${path}`);
    }
  });
});
