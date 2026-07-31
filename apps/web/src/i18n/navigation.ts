import { createNavigation } from "next-intl/navigation";

import { routing } from "@/i18n/routing";

/**
 * Locale-aware replacements for `next/link` and `next/navigation`. A
 * component that needs to link or navigate imports these, never the
 * framework originals — the framework versions don't know a locale prefix
 * exists and will silently produce unlocalized links.
 */
export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing);
