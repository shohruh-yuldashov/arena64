/**
 * Types for `generate-seo.mjs` — A64-026.3 §42.
 *
 * The script is plain Node because it runs outside the bundle, after
 * `vite build`, over `dist/`. This declares the surface it exports so that
 * `src/app/seo.test.ts` is type-checked like everything else rather than
 * asserting against `any` — a test that cannot see a signature is a test
 * that keeps passing after the signature changes.
 */

/** Every path the sitemap lists. Must not intersect the robots policy. */
export declare const INDEXABLE: readonly string[];

/** The robots policy a build with no configured origin publishes instead. */
export declare const BLOCK_EVERYTHING: string;

/**
 * `VITE_PUBLIC_ORIGIN`, normalised, or `null` when nothing configured one.
 *
 * Throws for a value that is not a URL, and for `http` on anything but
 * localhost.
 */
export declare function readOrigin(): string | null;

/** `index.html` with the canonical, `og:url`, an absolute image and JSON-LD. */
export declare function injectHead(html: string, origin: string): string;

/** `sitemap.xml` over `INDEXABLE`, on `origin`. */
export declare function sitemap(origin: string): string;

/** The JSON-LD graph: one `WebSite` and one `WebApplication`, nothing else. */
export declare function structuredData(origin: string): {
  "@context": string;
  "@graph": {
    "@type": string;
    "@id": string;
    url: string;
    name: string;
    description: string;
    applicationCategory?: string;
    operatingSystem?: string;
    inLanguage?: readonly string[];
    isPartOf?: { "@id": string };
  }[];
};
