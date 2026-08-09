import { EyeIcon, EyeOffIcon } from "lucide-react";
import { type ComponentProps, useState } from "react";

import { FormField } from "@/features/auth/ui/form-field";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";

/**
 * A password field a person can check before submitting — A64-025.4 §8.
 *
 * Four of the five auth forms ask for a password and none of them let you
 * see what you typed. On a phone, with autocorrect off and a policy that
 * wants a symbol, that is the single commonest reason a sign-in fails twice
 * before it works.
 *
 * ## What the toggle must not do
 *
 * - **Not submit.** `type="button"`, because a bare `<button>` inside a
 *   `<form>` submits it, and a person revealing their password to check it
 *   would post the form instead.
 * - **Not lose the value.** Only the `type` attribute changes; the input is
 *   never unmounted, so React keeps it and the browser keeps the caret.
 * - **Not lose focus.** Nothing here moves it.
 * - **Not be a mystery.** The accessible name says what pressing it will do
 *   and `aria-pressed` says which state it is in, so the icon is not the
 *   only signal.
 *
 * The button sits inside the field rather than beside it, so the label, the
 * description and the error still belong to one control — `FormField` owns
 * that wiring and this passes through it rather than around it.
 *
 * `autoComplete` stays the caller's: `current-password` on sign-in and
 * `new-password` everywhere else is a distinction password managers act on,
 * and it is not this component's to guess.
 */
export function PasswordField(
  props: Omit<ComponentProps<typeof FormField>, "type" | "trailing">,
) {
  const { t } = useTranslation();
  const [revealed, setRevealed] = useState(false);
  const Icon = revealed ? EyeOffIcon : EyeIcon;

  return (
    <FormField
      {...props}
      type={revealed ? "text" : "password"}
      trailing={
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-pressed={revealed}
          aria-label={t(revealed ? "auth.common.hidePassword" : "auth.common.showPassword")}
          onClick={() => setRevealed((shown) => !shown)}
        >
          <Icon aria-hidden="true" className="size-4" />
        </Button>
      }
    />
  );
}
