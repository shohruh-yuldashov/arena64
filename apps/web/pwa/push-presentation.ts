/**
 * What a push notification says, and where it takes you — A64-021.6 §12, §13.
 *
 * ## Why this is a module and not a `switch` inside the worker
 *
 * The same reason `cache-policy.ts` is: these are the two decisions in the
 * push path that can be *wrong* in a way that matters, and both have to be
 * directly testable without a `ServiceWorkerGlobalScope` to fake.
 *
 *   the **text** is what appears on a lock screen. Getting it from the
 *     server would mean rendering server-chosen prose in a context the
 *     person did not open;
 *   the **route** is a navigation the browser performs on a tap. Getting it
 *     from the payload would hand a navigation primitive to whatever wrote
 *     the payload.
 *
 * So both are closed tables compiled into the worker, keyed on a value from
 * an enum the backend owns. A type this build has never heard of renders a
 * generic notification and navigates to the list — never nothing, and never
 * somewhere it was told to go.
 *
 * ## §12's two options, and which this is
 *
 * Approach **B**: the payload carries a type and an id, and this file maps
 * the type to bounded local text. Approach A — the server composing
 * localised strings per push — was rejected for two reasons.
 *
 * The first is privacy, and it is the stronger one: server-composed text is
 * *specific* text ("Round 3 is live in the Tashkent Open"), which names a
 * tournament somebody is in, on a lock screen, in public. The generic
 * sentence below discloses that this person uses Arena64 and nothing more.
 *
 * The second is that it would put four locales' worth of notification copy
 * into every encrypted payload's 4 KB budget, to say something this file can
 * say for free.
 *
 * ## One function, not two
 *
 * `presentationFor` returns the destination alongside the text, rather than
 * a separate `targetPathFor`. Two functions reading the same table is two
 * places for a type to be present in one and missing from the other — and
 * the failure mode of that is a notification whose text says "tournament"
 * and whose tap opens the notification list.
 *
 * ## Why the text is not translated
 *
 * A service worker has no React, no i18n runtime, and no access to the
 * language a person chose — that lives in a store the worker cannot read,
 * and fetching it would need a session the worker may not have.
 *
 * Reading the *browser's* language is possible and is deliberately not
 * done: `navigator.language` is the operating system's preference, which is
 * frequently not the language somebody chose in this app, and a
 * notification in the wrong language is worse than one in a consistent one.
 *
 * English is the honest choice for a one-line interruption whose entire
 * content is "something happened in your tournament". The full notification
 * — in the person's own language — is one tap away, which is the tap this
 * notification exists to prompt.
 */

/** A push payload, as `domain.push.PushPayload.as_dict` writes it. */
export interface PushPayload {
  /** The notification's id. Short key: the encrypted envelope has a fixed
   *  86-byte overhead and every push service caps the result. */
  readonly n: string;
  /** `domain.record.NotificationType`. */
  readonly t: string;
}

export interface PushPresentation {
  readonly title: string;
  readonly body: string;
  /**
   * Where a tap goes — §13.
   *
   * A **path**, never a URL, and the worker resolves it against its own
   * origin. That is the same-origin guarantee: nothing in the table below
   * can produce a scheme, so there is no value this module can return that
   * navigates off this platform.
   *
   * Deliberately not derived from the notification's id. Every route here
   * is a list rather than an item, because the payload's id names a
   * *notification* and not a tournament — turning one into
   * `/tournaments/{id}` would open a tournament that does not exist.
   * Resolving the real target needs the notification, which needs a
   * session the worker may not have.
   */
  readonly path: string;
  /**
   * Collapses repeats on the same subject — the Notification API's `tag`.
   *
   * The **type**, not the id: three rounds published in one tournament
   * while a phone was asleep should be one notification saying the latest
   * thing, not three stacked. The id would make every push distinct, which
   * is the behaviour that makes people turn a channel off.
   */
  readonly tag: string;
}

/** What every notification falls back to. Never nothing. */
const GENERIC: Omit<PushPresentation, "tag"> = {
  title: "Arena64",
  body: "You have a new notification.",
  path: "/notifications",
};

/**
 * The closed table — §12, §13.
 *
 * Three entries, matching `domain.push.PUSH_CAPABLE_TYPES` exactly. A type
 * the backend adds without an entry here is not broken: it renders the
 * generic notification and opens the list, which is `PRESENTATIONS`'s whole
 * degradation story.
 *
 * Every `path` is a literal. There is no branch below that concatenates a
 * payload value into one, which is what makes "no arbitrary URL" a property
 * of the code rather than a validation somebody has to remember.
 */
const PRESENTATIONS: Readonly<Record<string, Omit<PushPresentation, "tag">>> = {
  tournament_round_published: {
    title: "A new round is live",
    body: "Pairings are out for your tournament.",
    path: "/tournaments",
  },
  tournament_registration_confirmed: {
    title: "You are entered",
    body: "Your tournament registration is confirmed.",
    path: "/tournaments",
  },
  tournament_completed: {
    title: "Your tournament has finished",
    body: "Final standings are available.",
    path: "/tournaments",
  },
};

/**
 * Whether this is something this worker can act on.
 *
 * Narrow rather than trusting: the payload arrives decrypted by the browser
 * from bytes this platform encrypted, so it *should* be well formed — and
 * "should" is not a reason to index into an object with it.
 */
export function isPushPayload(value: unknown): value is PushPayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.n === "string" && typeof candidate.t === "string";
}

/**
 * What to show for one payload — §12.
 *
 * Total: every input produces a notification. An unknown type is the
 * generic one rather than a silent return, because a push that displays
 * nothing is indistinguishable from a push that never arrived, and that is
 * the failure nobody can report.
 */
export function presentationFor(payload: PushPayload): PushPresentation {
  const known = PRESENTATIONS[payload.t];
  return { ...(known ?? GENERIC), tag: payload.t || "arena64" };
}
