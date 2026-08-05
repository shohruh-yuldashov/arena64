import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { mswServer } from "@/shared/test/msw/server";

/**
 * What every unit test gets, and why each piece is not optional.
 *
 * **MSW at the network boundary, not a mocked module.** Tests stub HTTP,
 * never `shared/api`. Mocking the module under the component would prove
 * the component calls a function; intercepting the request proves the
 * Axios instance, its interceptors, the envelope unwrap and the error
 * normalisation all work — the graph, not the seam (CLAUDE.md §6.8).
 *
 * `onUnhandledRequest: "error"` on purpose: a request no handler expected
 * is a test quietly reaching the real network, and the loudest possible
 * failure is the only one that stays fixed.
 *
 * **`matchMedia` does not exist in jsdom.** The theme reads it on every
 * render, so without this stub every test that mounts the app throws. It
 * defaults to light and is overridable per test.
 */
beforeAll(() => {
  mswServer.listen({ onUnhandledRequest: "error" });
});

afterEach(() => {
  cleanup();
  mswServer.resetHandlers();
});

afterAll(() => {
  mswServer.close();
});

vi.stubGlobal(
  "matchMedia",
  vi.fn((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
);
