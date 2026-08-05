import type { components } from "@/shared/api/generated/schema";

/**
 * The signed-in account, as the API describes it.
 *
 * An alias over the **generated** schema, not a re-declaration. A
 * hand-written `interface User` here would be a second definition of a
 * shape this app does not own, and the two would agree until the day the
 * backend added a field.
 */
export type User = components["schemas"]["UserRead"];

/** What to show when a user has no display name — never an empty string. */
export function displayNameOf(user: User): string {
  return user.display_name ?? user.username;
}
