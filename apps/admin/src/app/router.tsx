import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router";
import { useEffect, useRef } from "react";

import { useAdminAuth } from "@/app/use-admin-auth";
import { AuditPage } from "@/pages/audit";
import { DashboardPage } from "@/pages/dashboard";
import { LoginPage } from "@/pages/login";
import { MatchDetailPage } from "@/pages/match-detail";
import { MatchesPage } from "@/pages/matches";
import { ModerationPage } from "@/pages/moderation";
import { NotificationDetailPage } from "@/pages/notification-detail";
import { NotificationsPage } from "@/pages/notifications";
import { TournamentDetailPage } from "@/pages/tournament-detail";
import { TournamentsPage } from "@/pages/tournaments";
import { UserDetailPage } from "@/pages/user-detail";
import { UsersPage } from "@/pages/users";
import { signOut as revokeSession } from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";

/**
 * The admin console's routes — A64-024.2 §6, §7.
 *
 * `@tanstack/react-router`, the same router `apps/web` uses (ADR-002), so
 * there is one routing vocabulary in this repository rather than two. It
 * replaces A64-024.1's `useState` navigation, which could not survive a
 * refresh, had no URL to share and no back button.
 *
 * ## Authorization lives in one place
 *
 * `ProtectedLayout` is the parent route of every admin page, so a section
 * added later is protected by **being a child** rather than by its author
 * remembering a guard. §6 forbids duplicating the check per page and this
 * is the shape that makes duplication unnecessary.
 *
 * `/login` is the only public route, and it is deliberately a sibling
 * rather than a child.
 */

const SECTIONS = [
  { path: "/users", label: "nav.users" },
  { path: "/matches", label: "nav.matches" },
  { path: "/tournaments", label: "nav.tournaments" },
  { path: "/moderation", label: "nav.moderation" },
  { path: "/notifications", label: "nav.notifications" },
  { path: "/audit", label: "nav.audit" },
] as const;

const rootRoute = createRootRoute({
  component: () => <Outlet />,
  notFoundComponent: () => <NotFound />,
});

function NotFound() {
  const { t } = useTranslation();
  return (
    <main className="gate">
      <h1>{t("route.notFound")}</h1>
      <p className="muted">{t("route.notFoundHint")}</p>
      <Link className="action" to="/">
        {t("route.backToDashboard")}
      </Link>
    </main>
  );
}

/**
 * Everything privileged hangs off this — §7.
 *
 * Five states, and the shell is reached from exactly one. The others render
 * no navigation, no identity and no section content, which is what "must
 * never flash while authority is unresolved" means in practice: there is no
 * code path that paints the shell before `/admin/me` has answered.
 *
 * An unauthenticated visitor is sent to `/login` **carrying where they were
 * going**, so §8's intended-destination return works from any protected
 * route without each page arranging it.
 */
function ProtectedLayout() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const { auth, resolve, forget } = useAdminAuth(pathname);

  // Where they were going, captured on the **first** render of this layout.
  // Reading `pathname` at redirect time would capture `/login` once the
  // navigation has begun, and the console would return an administrator to
  // the form they just left.
  const intended = useRef(pathname);

  useEffect(() => {
    if (auth.state !== "unauthenticated") return;
    void navigate({
      to: "/login",
      search: { next: intended.current },
      replace: true,
    });
  }, [auth.state, navigate]);

  if (auth.state === "checking" || auth.state === "unauthenticated") {
    return (
      <main className="gate">
        <p role="status">{t("auth.checking")}</p>
      </main>
    );
  }

  if (auth.state === "forbidden") {
    return (
      <main className="gate">
        <h1>{t("app.title")}</h1>
        <p role="alert">{t("auth.denied")}</p>
        <p className="muted">{t("auth.deniedHint")}</p>
        <button
          type="button"
          className="action"
          onClick={() => {
            void revokeSession().then(forget);
          }}
        >
          {t("auth.signOut")}
        </button>
      </main>
    );
  }

  if (auth.state === "unavailable") {
    return (
      <main className="gate">
        <p role="alert">{t("auth.failed")}</p>
        <button type="button" className="action" onClick={resolve}>
          {t("route.backToDashboard")}
        </button>
      </main>
    );
  }

  const identity = auth.session.display_name ?? auth.session.username;

  return (
    <div className="shell">
      <header className="shell__header">
        <div>
          <h1>{t("app.title")}</h1>
          <p className="muted">{t("app.subtitle")}</p>
        </div>
        <div className="shell__identity">
          <span>{t("auth.signedInAs", { name: identity })}</span>
          <button
            type="button"
            className="action"
            onClick={() => {
              // Revoke server-side first, then forget locally. The order
              // matters: forgetting first would leave a live session on the
              // server that nothing here can reach to end.
              void revokeSession().then(() => {
                forget();
                void navigate({ to: "/login", replace: true });
              });
            }}
          >
            {t("auth.signOut")}
          </button>
        </div>
      </header>

      <div className="shell__body">
        <nav aria-label={t("nav.label")}>
          <ul>
            <li>
              <Link to="/" activeOptions={{ exact: true }}>
                {t("nav.dashboard")}
              </Link>
            </li>
            {SECTIONS.map((section) => (
              <li key={section.path}>
                <Link to={section.path}>{t(section.label as TranslationKey)}</Link>
              </li>
            ))}
          </ul>
        </nav>

        <main>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
  validateSearch: (search: Record<string, unknown>): { next?: string } =>
    typeof search.next === "string" ? { next: search.next } : {},
});

const protectedRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "protected",
  component: ProtectedLayout,
});

// A64-024.9. The home route stops being a placeholder: six facts, the
// attention list, and the ten most recent privileged actions — every one of
// them a link into the console that owns the work.
const dashboardRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/",
  component: DashboardPage,
});

// A64-024.3. `/users` was the first real section. Every section in
// `SECTIONS` is now built — A64-024.10 removed the placeholder route and
// its page, which had become unreachable.
const usersRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/users",
  component: UsersPage,
  validateSearch: (search: Record<string, unknown>) => ({
    ...(typeof search.q === "string" ? { q: search.q } : {}),
    ...(typeof search.active === "string" ? { active: search.active } : {}),
    ...(typeof search.verified === "string" ? { verified: search.verified } : {}),
  }),
});

const userDetailRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/users/$userId",
  component: UserDetailPage,
});

// A64-024.4. `/matches` is the second real section.
const matchesRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/matches",
  component: MatchesPage,
  validateSearch: (search: Record<string, unknown>) => ({
    ...(typeof search.status === "string" ? { status: search.status } : {}),
    ...(typeof search.rated === "string" ? { rated: search.rated } : {}),
    ...(typeof search.origin === "string" ? { origin: search.origin } : {}),
    ...(typeof search.participant === "string" ? { participant: search.participant } : {}),
  }),
});

const matchDetailRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/matches/$matchId",
  component: MatchDetailPage,
});

// A64-024.5. `/tournaments` is the third real section.
const tournamentsRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/tournaments",
  component: TournamentsPage,
  validateSearch: (search: Record<string, unknown>) => ({
    ...(typeof search.status === "string" ? { status: search.status } : {}),
    ...(typeof search.rated === "string" ? { rated: search.rated } : {}),
  }),
});

const tournamentDetailRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/tournaments/$tournamentId",
  component: TournamentDetailPage,
});

// A64-024.8. `/audit` is the fourth real section — and the one that
// unblocks moderation, because a mutation the console cannot record is a
// mutation the console must not offer.
const auditRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/audit",
  component: AuditPage,
  validateSearch: (search: Record<string, unknown>) => ({
    ...(typeof search.action === "string" ? { action: search.action } : {}),
    ...(typeof search.actor === "string" ? { actor: search.actor } : {}),
    ...(typeof search.subject === "string" ? { subject: search.subject } : {}),
  }),
});

// A64-024.6. `/moderation` becomes the fifth real section — and the first
// with a write behind it. The actions live on the account's own page; this
// route answers "who is restricted right now".
const moderationRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/moderation",
  component: ModerationPage,
});

// A64-024.7. `/notifications` becomes the sixth real section: reads, plus
// one mutation that re-arms a push a service never accepted. Nothing here
// sends anything new.
const notificationsRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/notifications",
  component: NotificationsPage,
  validateSearch: (search: Record<string, unknown>) => ({
    ...(typeof search.recipient === "string" ? { recipient: search.recipient } : {}),
    ...(typeof search.failed === "string" ? { failed: search.failed } : {}),
  }),
});

const notificationDetailRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/notifications/$notificationId",
  component: NotificationDetailPage,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  protectedRoute.addChildren([
    dashboardRoute,
    usersRoute,
    userDetailRoute,
    matchesRoute,
    matchDetailRoute,
    tournamentsRoute,
    tournamentDetailRoute,
    auditRoute,
    moderationRoute,
    notificationsRoute,
    notificationDetailRoute,
  ]),
]);

export function createAdminRouter(history?: Parameters<typeof createRouter>[0]["history"]) {
  return createRouter({ routeTree, ...(history ? { history } : {}) });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAdminRouter>;
  }
}
