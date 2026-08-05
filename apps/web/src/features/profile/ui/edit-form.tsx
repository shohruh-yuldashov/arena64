import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import type { MyProfile } from "@/entities/profile";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { useUpdateProfile } from "@/features/profile/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";

/**
 * The three fields `PATCH /profile` accepts, and not one more.
 *
 * `ProfileUpdateRequest` is `{display_name?, bio?, country?}`. Username is
 * **not** editable through this endpoint and no input offers it; email is
 * not here either. Sending anything else would be rejected, and offering a
 * control for it would promise something the API does not do.
 *
 * ## Dirty tracking, and why the submit is disabled without it
 *
 * A `PATCH` that resends unchanged values is a write, an audit row and a
 * cache invalidation for nothing. `isDirty` comes from React Hook Form
 * comparing against `defaultValues`, which are the loaded profile — so
 * "changed" means changed from what the server has, not from empty.
 *
 * ## Nulls out, nulls in
 *
 * The API stores `null` for "not set" and this form shows an empty input
 * for it. On submit, an emptied field is sent as `null` rather than `""` —
 * they are different values to the server, and `""` would store a bio that
 * is present and blank.
 */
const DISPLAY_NAME_MAX = 50;
const BIO_MAX = 500;

const schema = z.object({
  display_name: z.string().trim().max(DISPLAY_NAME_MAX, "profile.edit.displayNameTooLong"),
  bio: z.string().trim().max(BIO_MAX, "profile.edit.bioTooLong"),
  country: z
    .string()
    .trim()
    .toUpperCase()
    .refine((value) => value === "" || /^[A-Z]{2}$/.test(value), "profile.edit.countryInvalid"),
});

type FormValues = z.infer<typeof schema>;

/** `""` from an emptied input means "unset", which the API spells `null`. */
const orNull = (value: string): string | null => (value === "" ? null : value);

export function ProfileEditForm({ profile }: { profile: MyProfile }) {
  const { t } = useTranslation();
  const update = useUpdateProfile();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [saved, setSaved] = useState(false);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    mode: "onTouched",
    defaultValues: {
      display_name: profile.display_name ?? "",
      bio: profile.bio ?? "",
      country: profile.country ?? "",
    },
  });

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    setSaved(false);
    try {
      const updated = await update.mutateAsync({
        display_name: orNull(values.display_name),
        bio: orNull(values.bio),
        country: orNull(values.country),
      });
      // Re-baselined against what the **server** stored, not what was
      // typed: it trims and normalises, so resetting to the local values
      // would leave the form dirty against its own successful save.
      reset({
        display_name: updated.display_name ?? "",
        bio: updated.bio ?? "",
        country: updated.country ?? "",
      });
      setSaved(true);
    } catch (error) {
      setFailure(profileErrorKey(error));
    }
  });

  const message = (key: string | undefined, values?: Record<string, string | number>) =>
    key === undefined ? undefined : t(key as TranslationKey, values);

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="flex flex-col gap-4" noValidate>
      <FormError messageKey={failure} />
      {saved && <FormStatus>{t("profile.edit.saved")}</FormStatus>}

      <FormField
        label={t("profile.edit.displayName")}
        autoComplete="nickname"
        maxLength={DISPLAY_NAME_MAX}
        error={message(errors.display_name?.message, { max: DISPLAY_NAME_MAX })}
        {...register("display_name")}
      />

      <div className="flex flex-col gap-1.5">
        <label htmlFor="bio" className="text-sm font-medium">
          {t("profile.edit.bio")}
        </label>
        <textarea
          id="bio"
          rows={4}
          maxLength={BIO_MAX}
          aria-invalid={errors.bio !== undefined}
          aria-describedby={errors.bio !== undefined ? "bio-error" : "bio-hint"}
          className="border-input focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:border-destructive w-full rounded-md border bg-transparent px-3 py-2 text-sm shadow-xs outline-none focus-visible:ring-[3px]"
          {...register("bio")}
        />
        {errors.bio === undefined ? (
          <p id="bio-hint" className="text-muted-foreground text-xs">
            {t("profile.edit.bioHint", { max: BIO_MAX })}
          </p>
        ) : (
          <p id="bio-error" role="alert" className="text-destructive text-sm font-medium">
            {message(errors.bio.message, { max: BIO_MAX })}
          </p>
        )}
      </div>

      <FormField
        label={t("profile.edit.country")}
        autoComplete="country"
        maxLength={2}
        description={t("profile.edit.countryHint")}
        error={message(errors.country?.message)}
        {...register("country")}
      />

      <Button type="submit" className="min-h-11 self-start" disabled={!isDirty || isSubmitting}>
        {isSubmitting ? <Spinner label={t("profile.edit.save")} /> : t("profile.edit.save")}
      </Button>
      {!isDirty && !saved && (
        <p className="text-muted-foreground text-xs">{t("profile.edit.noChanges")}</p>
      )}
    </form>
  );
}
