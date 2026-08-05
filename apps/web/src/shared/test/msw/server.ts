import { setupServer } from "msw/node";

import { handlers } from "@/shared/test/msw/handlers";

/** One interception server for the whole suite; `setup.ts` runs its lifecycle. */
export const mswServer = setupServer(...handlers);
