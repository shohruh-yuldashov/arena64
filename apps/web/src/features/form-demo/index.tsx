import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button, Input } from "@/shared/ui";

/**
 * The React Hook Form + Zod wiring, demonstrated once.
 *
 * **Not a login form and not a business form.** It exists so the pattern
 * every later phase copies is in the repository, compiled, and covered —
 * rather than described in a document and reinvented five times.
 *
 * Three things it pins down:
 *
 *   1. **One schema is both the validation and the type.** `FormValues` is
 *      inferred from `schema`, so a field cannot be validated and typed
 *      differently — the mismatch that produces "it says it's valid but
 *      the request 422s".
 *   2. **Errors are wired to the input, not just printed.** `aria-invalid`
 *      and `aria-describedby` are what make a rejection perceivable to a
 *      screen reader; a red border alone is a message only sighted users
 *      receive.
 *   3. **Submission is async and the button reflects it.** `isSubmitting`
 *      disables the control, so a slow network cannot produce two writes
 *      from one intent.
 */
const schema = z.object({
  handle: z
    .string()
    .trim()
    .min(3, "At least 3 characters.")
    .max(24, "At most 24 characters.")
    .regex(/^[a-z0-9_]+$/i, "Letters, digits and underscores only."),
});

type FormValues = z.infer<typeof schema>;

export function FormDemo() {
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting, isSubmitSuccessful },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    // Validate on blur, then live once a field has been rejected: telling
    // somebody their handle is too short while they are typing the third
    // character is noise, and staying silent after they fixed it is worse.
    mode: "onTouched",
    defaultValues: { handle: "" },
  });

  const onSubmit = handleSubmit(async () => {
    // Nothing is sent anywhere. The submit handler is the seam a real
    // feature replaces with a mutation; this phase has no endpoint to call
    // and inventing one would be the business logic the task excludes.
    await Promise.resolve();
  });

  const error = errors.handle;

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="flex flex-col gap-2" noValidate>
      <label htmlFor="handle" className="text-sm font-medium">
        Handle
      </label>
      <Input
        id="handle"
        autoComplete="off"
        aria-invalid={error !== undefined}
        aria-describedby={error !== undefined ? "handle-error" : undefined}
        {...register("handle")}
      />
      {error !== undefined && (
        <p id="handle-error" role="alert" className="text-destructive text-sm">
          {error.message}
        </p>
      )}
      <Button type="submit" disabled={isSubmitting} className="self-start">
        {isSubmitting ? "Checking…" : "Validate"}
      </Button>
      {isSubmitSuccessful && (
        <p role="status" className="text-muted-foreground text-sm">
          That handle is well formed.
        </p>
      )}
    </form>
  );
}
