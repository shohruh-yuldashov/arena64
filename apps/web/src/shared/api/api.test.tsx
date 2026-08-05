import { useQuery } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { api, createQueryClient, QUERY_GC_TIME_MS, QUERY_STALE_TIME_MS } from "@/shared/api";
import { ApiError } from "@/shared/api/errors";
import { env } from "@/shared/config/env";
import { failure, ok } from "@/shared/test/msw/handlers";
import { mswServer } from "@/shared/test/msw/server";
import { renderWithProviders } from "@/shared/test/render";

/**
 * The API layer, through MSW rather than a mocked module.
 *
 * Stubbing `shared/api` would prove a component calls a function.
 * Intercepting the *request* proves the whole path: the Axios instance,
 * its correlation-id interceptor, the `{data, meta}` unwrap, the error
 * normalisation, and the query client's policy — the graph, not the seam
 * (CLAUDE.md §6.8).
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;

describe("the api layer", () => {
  it("unwraps the envelope through a real query and applies the documented policy", async () => {
    mswServer.use(ok(url("/api/v1/probe"), { value: "unwrapped" }));

    function Probe() {
      const { data } = useQuery({
        queryKey: ["probe"],
        queryFn: () => api.get<{ value: string }>("/api/v1/probe"),
      });
      return <output>{data?.value ?? "…"}</output>;
    }

    renderWithProviders(<Probe />);

    // `data` is the payload, not `{data, meta}` — a component never sees
    // the envelope, which is the whole point of `request`.
    expect(await screen.findByText("unwrapped")).toBeVisible();

    // The policy is a decision with reasons written beside it
    // (`query-client.ts`), so it is asserted rather than left to drift back
    // to TanStack's defaults on the next refactor. `staleTime: 0` — the
    // default — would mean every mount refetches a ladder that cannot have
    // moved.
    const defaults = createQueryClient().getDefaultOptions().queries;
    expect(defaults?.staleTime).toBe(QUERY_STALE_TIME_MS);
    expect(defaults?.gcTime).toBe(QUERY_GC_TIME_MS);
    expect(defaults?.refetchOnWindowFocus).toBe(true);
    // A mutation is not known to be idempotent, so it is never retried —
    // that is how a player enters a tournament twice.
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });

  it("normalises every failure into one typed error, and marks what is worth retrying", async () => {
    // 1. The API refused it, with the platform's coded body.
    mswServer.use(failure(url("/api/v1/full"), 409, "tournament_full", "This is full."));
    const conflict = await api.get("/api/v1/full").catch((error: unknown) => error);

    expect(conflict).toBeInstanceOf(ApiError);
    expect(conflict).toMatchObject({
      kind: "http",
      status: 409,
      code: "tournament_full",
      message: "This is full.",
    });
    // A 409 will fail identically however many times it is sent. Retrying
    // it turns one user-visible failure into three (CLAUDE.md §9.10).
    expect((conflict as ApiError).isRetryable).toBe(false);

    // 2. The API answered, but with nothing this client can read — a proxy
    //    page, a crashed worker. The status still means something.
    mswServer.use(
      http.get(url("/api/v1/broken"), () => new HttpResponse("<html>", { status: 502 })),
    );
    const broken = await api.get("/api/v1/broken").catch((error: unknown) => error);

    expect(broken).toMatchObject({ kind: "http", status: 502, code: null });
    expect((broken as ApiError).isRetryable).toBe(true);

    // 3. Nothing answered at all. Distinct from every status, because the
    //    request may never have reached the server and the user's answer
    //    ("check your connection") is a different sentence.
    mswServer.use(http.get(url("/api/v1/offline"), () => HttpResponse.error()));
    const offline = await api.get("/api/v1/offline").catch((error: unknown) => error);

    expect(offline).toMatchObject({ kind: "network", status: null, code: null });
    expect((offline as ApiError).isRetryable).toBe(true);
    // The original is preserved on every path — CLAUDE.md §9.4. A tidier
    // message that discarded it would make the one error worth debugging
    // the one impossible to debug.
    expect((offline as ApiError).cause).toBeDefined();
  });
});
