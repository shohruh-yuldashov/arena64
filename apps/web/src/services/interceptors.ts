import { CORRELATION_ID_HEADER } from "@/lib/constants";

export type RequestInterceptor = (init: RequestInit) => RequestInit | Promise<RequestInit>;
export type ResponseInterceptor = (response: Response) => Response | Promise<Response>;

/**
 * The request interceptor pipeline — `services/api-client.ts` runs every
 * outgoing request through these, in order, before calling `fetch`.
 * Reusable and framework-friendly on purpose: adding a future auth token
 * (once `auth` exists) is `requestInterceptors.push(authTokenInterceptor)`
 * beside whichever module owns that concern, not an edit to
 * `api-client.ts` itself.
 */
export const requestInterceptors: RequestInterceptor[] = [
  correlationIdInterceptor,
  jsonContentTypeInterceptor,
];

/**
 * The response interceptor pipeline. Empty for now — there is no
 * cross-cutting response transformation platform infrastructure needs yet
 * (error parsing and envelope unwrapping are deliberately their own
 * modules, not interceptors, because both can *throw*, which an
 * interceptor — expected to hand back a `Response` — should not).
 */
export const responseInterceptors: ResponseInterceptor[] = [];

/**
 * Every request carries its own correlation id unless one is already set
 * — echoed by `apps/api/app/common/middleware.py`'s
 * `CorrelationIdMiddleware`, so a request that fails can be traced through
 * backend logs from the moment this client sent it, before any session or
 * auth concept exists to identify who sent it.
 */
function correlationIdInterceptor(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  if (!headers.has(CORRELATION_ID_HEADER)) {
    headers.set(CORRELATION_ID_HEADER, crypto.randomUUID());
  }
  return { ...init, headers };
}

function jsonContentTypeInterceptor(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return { ...init, headers };
}

export async function runRequestInterceptors(init: RequestInit): Promise<RequestInit> {
  let current = init;
  for (const interceptor of requestInterceptors) {
    current = await interceptor(current);
  }
  return current;
}

export async function runResponseInterceptors(response: Response): Promise<Response> {
  let current = response;
  for (const interceptor of responseInterceptors) {
    current = await interceptor(current);
  }
  return current;
}
