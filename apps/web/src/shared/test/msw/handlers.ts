import { http, HttpResponse } from "msw";

/**
 * The default handler set — deliberately **empty**.
 *
 * A shared list of "usual" responses is how a suite ends up asserting
 * against fixtures nobody chose: a test passes because some other test's
 * handler happened to answer. Each test declares the responses it depends
 * on with `mswServer.use(...)`, and anything it did not declare is an
 * error (see `setup.ts`).
 *
 * `ok` and `failure` below are the two envelope shapes the backend
 * actually produces, so a test states its intent rather than re-typing
 * `{data, meta}` and hoping it matches `app/core/responses.py`.
 */
export const handlers = [];

/** The success envelope — `app.core.responses.ApiResponse[T]`. */
export function ok<T>(url: string, data: T, status = 200) {
  return http.get(url, () =>
    HttpResponse.json({ data, meta: { request_id: null, correlation_id: null } }, { status }),
  );
}

/** The error body — `app.api.exception_handlers.ErrorResponse`. */
export function failure(url: string, status: number, code: string, message = "Nope.") {
  return http.get(url, () =>
    HttpResponse.json({ code, message, request_id: null, correlation_id: null }, { status }),
  );
}
