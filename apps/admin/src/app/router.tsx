import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { type Theme, useTheme } from "@/app/theme";
import { useAdminAuth } from "@/app/use-admin-auth";
import { AuditPage } from "@/pages/audit";
import { AnalyticsPage } from "@/pages/analytics";
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
import { BrandMark } from "@/shared/ui/brand-mark";
import { Icon, type IconName } from "@/shared/ui/icon";
import { ToastProvider } from "@/shared/ui/toast";

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

/**
 * The console's map — A64-027A §3.
 *
 * Nine destinations in six named groups, and the grouping is the point. A
 * flat list of nine asks the reader to hold the whole console in their head
 * to find anything; "Safety" containing moderation and the audit log tells
 * an administrator who has never seen Arena64's source where to look when
 * something is wrong.
 *
 * The group names describe **what an operator is trying to do**, not which
 * module owns the route. `audit` and `moderation` live in different backend
 * modules and belong in the same group here, because the person reaching
 * for one is often about to reach for the other.
 *
 * Every entry is a real route backed by a real capability. §3 forbids the
 * menu item that exists because the sidebar looked unbalanced without it.
 */
interface NavItem {
  path: string;
  label: TranslationKey;
  icon: IconName;
}

interface NavGroup {
  label: TranslationKey;
  items: NavItem[];
}

const NAVIGATION: NavGroup[] = [
  {
    label: "nav.group.overview",
    items: [{ path: "/", label: "nav.dashboard", icon: "dashboard" }],
  },
  {
    label: "nav.group.management",
    items: [
      { path: "/users", label: "nav.users", icon: "users" },
      { path: "/matches", label: "nav.matches", icon: "matches" },
      { path: "/tournaments", label: "nav.tournaments", icon: "tournaments" },
    ],
  },
  {
    label: "nav.group.communication",
    items: [{ path: "/notifications", label: "nav.notifications", icon: "notifications" }],
  },
  {
    label: "nav.group.insights",
    items: [{ path: "/analytics", label: "nav.analytics", icon: "analytics" }],
  },
  {
    label: "nav.group.safety",
    items: [
      { path: "/moderation", label: "nav.moderation", icon: "moderation" },
      { path: "/audit", label: "nav.audit", icon: "audit" },
    ],
  },
];

/**
 * Which navigation entry a path belongs to, for the toolbar's context line.
 *
 * Longest match wins, so `/users/{id}` resolves to Users rather than to the
 * dashboard's `/`. Without the length ordering every detail page would
 * claim to be the dashboard, since every path starts with `/`.
 */
function sectionFor(pathname: string): NavItem | null {
  const all = NAVIGATION.flatMap((group) => group.items);
  const matches = all
    .filter((item) => (item.path === "/" ? pathname === "/" : pathname.startsWith(item.path)))
    .sort((left, right) => right.path.length - left.path.length);
  return matches[0] ?? null;
}

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

  return (
    <ToastProvider>
      <ConsoleShell
        identity={auth.session.display_name ?? auth.session.username}
        username={auth.session.username}
        roles={auth.session.roles}
        pathname={pathname}
        onSignOut={() => {
          // Revoke server-side first, then forget locally. The order
          // matters: forgetting first would leave a live session on the
          // server that nothing here can reach to end.
          void revokeSession().then(() => {
            forget();
            void navigate({ to: "/login", replace: true });
          });
        }}
      />
    </ToastProvider>
  );
}

/**
 * The console frame — A64-027A §4.
 *
 * Three regions, and each answers one of the two questions §4 says an
 * administrator must never have to ask:
 *
 *     sidebar     "where am I, and where else could I be"
 *     toolbar     "what is this screen, and what is true of my session"
 *     workspace   "what can I do here"
 *
 * ## The sidebar is one element in two shapes
 *
 * Below 64rem it is a sheet over the page; above, a column beside it. Both
 * shapes are the *same markup* — the difference is transform and position —
 * so the reading order a screen reader gets never depends on the viewport,
 * and a keyboard operator tabs through navigation before content on every
 * device.
 *
 * ## The rail
 *
 * Collapsing to icons is a real preference on a 13-inch laptop, and a real
 * accessibility hazard if the labels simply vanish: `title` plus the
 * button's own accessible name keep every destination announceable. The
 * state is deliberately not persisted — it is a momentary "give me more
 * width for this table", not a setting.
 */
function ConsoleShell({
  identity,
  username,
  roles,
  pathname,
  onSignOut,
}: {
  identity: string;
  username: string;
  roles: string[];
  pathname: string;
  onSignOut: () => void;
}) {
  const { t } = useTranslation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [rail, setRail] = useState(false);
  const current = sectionFor(pathname);

  // A navigation closes the sheet. Without this, following a link on a
  // phone leaves the destination hidden behind the menu that opened it.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [drawerOpen]);

  return (
    <div className="shell" data-rail={rail ? "true" : undefined}>
      <aside className="sidebar" data-open={drawerOpen ? "true" : undefined}>
        <Link className="sidebar__brand" to="/">
          {/* The platform's own mark. A64-027A.1 drew an "A64" monogram
              here — a third brand treatment beside the wordmark and the
              favicon, and the only one this repository invented. */}
          <BrandMark className="sidebar__mark" size={34} />
          <span className="sidebar__name">
            <strong>{t("brand.name")}</strong>
            <span>{t("brand.console")}</span>
          </span>
        </Link>

        <nav aria-label={t("nav.label")}>
          {NAVIGATION.map((group) => (
            <div key={group.label}>
              <p className="sidebar__group">{t(group.label)}</p>
              <ul>
                {group.items.map((item) => (
                  <li key={item.path}>
                    <Link
                      to={item.path}
                      title={t(item.label)}
                      activeOptions={item.path === "/" ? { exact: true } : undefined}
                    >
                      <Icon name={item.icon} size={17} />
                      <span>{t(item.label)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__account">
            <span className="sidebar__avatar" aria-hidden="true">
              {identity.slice(0, 2).toUpperCase()}
            </span>
            <span className="sidebar__who">
              <strong>{identity}</strong>
              {/* The role is shown because it is the answer to "may I do
                  this", and an operator who cannot see their own authority
                  discovers its limits by being refused. */}
              <span>{roles.length > 0 ? roles.join(" · ") : `@${username}`}</span>
            </span>
          </div>
          <button type="button" className="action subtle" onClick={onSignOut}>
            <Icon name="signOut" size={16} />
            <span>{t("auth.signOut")}</span>
          </button>
        </div>
      </aside>

      {drawerOpen && (
        <button
          type="button"
          className="scrim"
          aria-label={t("shell.closeMenu")}
          onClick={() => {
            setDrawerOpen(false);
          }}
        />
      )}

      <div className="workspace">
        <header className="topbar">
          <button
            type="button"
            className="action subtle icon-only menu-toggle"
            aria-label={t("shell.openMenu")}
            aria-expanded={drawerOpen}
            onClick={() => {
              setDrawerOpen(true);
            }}
          >
            <Icon name="menu" size={18} />
          </button>

          <nav className="topbar__crumbs" aria-label={t("shell.breadcrumb")}>
            <Link to="/">{t("app.title")}</Link>
            <Icon name="chevronRight" size={14} aria-hidden="true" />
            <strong>{current === null ? t("nav.dashboard") : t(current.label)}</strong>
          </nav>

          <div className="topbar__spacer" />

          <EnvironmentBadge />

          <button
            type="button"
            className="action subtle icon-only rail-toggle"
            aria-label={t("shell.collapseNav")}
            aria-pressed={rail}
            onClick={() => {
              setRail((value) => !value);
            }}
          >
            <Icon name="panel" size={18} />
          </button>

          <ThemeSwitch />
        </header>

        <main>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/**
 * Which deployment this console is pointed at — A64-027A.2 §9.
 *
 * Real, and read from the browser rather than invented: an administrator
 * with a staging console and a production one open in two tabs has no other
 * way to tell them apart, and the consequences of confusing them are the
 * reason this task refuses a decorative toolbar.
 *
 * `localhost` reads as "local"; anything else is named by its host, which
 * is what an operator actually recognises. Nothing here calls the API — the
 * origin the console was served from *is* the answer.
 */
function EnvironmentBadge() {
  const { t } = useTranslation();
  const host = typeof window === "undefined" ? "" : window.location.hostname;
  if (host === "") return null;

  const local = host === "localhost" || host === "127.0.0.1" || host.endsWith(".localhost");
  return (
    <span className="topbar__env" title={host}>
      {local ? t("shell.envLocal") : host}
    </span>
  );
}

/**
 * The theme control — three states, one button.
 *
 * A two-state toggle cannot express "follow my machine", which is the state
 * most operators are already in and the one a console has no business
 * overriding. Cycling through three is a smaller control than a menu and
 * the current state is always announced, so nobody has to guess which of
 * the three they are in.
 */
function ThemeSwitch() {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();
  const order: Theme[] = ["system", "light", "dark"];
  const next = order[(order.indexOf(theme) + 1) % order.length] ?? "system";

  return (
    <button
      type="button"
      className="action subtle icon-only"
      onClick={() => {
        setTheme(next);
      }}
      aria-label={t("theme.switch", { current: t(`theme.${theme}` as TranslationKey) })}
      title={t(`theme.${theme}` as TranslationKey)}
    >
      <Icon
        name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "settings"}
        size={18}
      />
    </button>
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
const analyticsRoute = createRoute({
  getParentRoute: () => protectedRoute,
  path: "/analytics",
  component: AnalyticsPage,
});

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
    // A64-027A §13. The workspace's tab lives in the URL so a link to the
    // history is a link to the history, and Back moves between them.
    ...(typeof search.tab === "string" ? { tab: search.tab } : {}),
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
    analyticsRoute,
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
