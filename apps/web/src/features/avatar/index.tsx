import { useRef, useState } from "react";

import type { MyProfile } from "@/entities/profile";
import { avatarSrc, initialsOf } from "@/entities/profile";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { useDeleteAvatar, useUploadAvatar } from "@/features/profile/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import {
  Avatar,
  AvatarFallback,
  AvatarImage,
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Spinner,
} from "@/shared/ui";

/**
 * Upload, replace and remove a profile photo.
 *
 * ## The client checks, and the server decides
 *
 * `MAX_BYTES` and `ACCEPTED_TYPES` mirror the API's own limits
 * (`avatars.domain.images`, and the endpoint's own description). They exist
 * to fail in a hundred milliseconds instead of after uploading five
 * megabytes over a phone connection — **not** as the guarantee. The server
 * re-validates every byte and its answer is the one that counts; a client
 * check that disagreed would only ever be wrong in the safe direction.
 *
 * `file.type` is the browser's guess from the extension and content, and it
 * is trivially forged. That is fine here precisely *because* it is not the
 * guarantee — the backend sniffs the actual bytes.
 *
 * ## No base64 anywhere
 *
 * The preview is an object URL, revoked when it is replaced or the upload
 * finishes. A data URL would put a multi-megabyte string into React state,
 * where it is copied on every render and kept alive by every closure that
 * captured it.
 *
 * ## Cancellation
 *
 * An `AbortController` per upload. A five-megabyte image on a slow
 * connection is long enough that leaving the page should stop it rather
 * than finish invisibly.
 */
const MAX_BYTES = 5 * 1024 * 1024;
const MAX_MB = MAX_BYTES / (1024 * 1024);
const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;

export function AvatarManager({ profile }: { profile: MyProfile }) {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [confirmingRemoval, setConfirmingRemoval] = useState(false);

  const upload = useUploadAvatar();
  const remove = useDeleteAvatar();

  const stored = avatarSrc(profile.avatar_url, null);
  const shown = preview ?? stored;

  function reject(key: TranslationKey): void {
    setFailure(key);
    // Cleared so choosing the same file again re-fires `change` — without
    // this, correcting the file and picking it again does nothing.
    if (input.current !== null) input.current.value = "";
  }

  async function onFileChosen(file: File): Promise<void> {
    setFailure(null);

    if (!ACCEPTED_TYPES.includes(file.type as (typeof ACCEPTED_TYPES)[number])) {
      reject("profile.avatar.wrongType");
      return;
    }
    if (file.size > MAX_BYTES) {
      reject("profile.avatar.tooLarge");
      return;
    }

    const objectUrl = URL.createObjectURL(file);
    setPreview((previous) => {
      if (previous !== null) URL.revokeObjectURL(previous);
      return objectUrl;
    });

    controller.current?.abort();
    controller.current = new AbortController();

    try {
      await upload.mutateAsync({ file, signal: controller.current.signal });
    } catch (error) {
      setFailure(profileErrorKey(error));
    } finally {
      // The stored URL is authoritative once the mutation settles, and it
      // is versioned, so the preview has done its job either way.
      URL.revokeObjectURL(objectUrl);
      setPreview(null);
      if (input.current !== null) input.current.value = "";
    }
  }

  async function onRemove(): Promise<void> {
    setFailure(null);
    try {
      await remove.mutateAsync();
      setConfirmingRemoval(false);
    } catch (error) {
      setFailure(profileErrorKey(error));
    }
  }

  const busy = upload.isPending || remove.isPending;

  return (
    <section aria-labelledby="avatar-heading" className="flex flex-col gap-3">
      <h2 id="avatar-heading" className="text-base font-semibold">
        {t("profile.avatar.title")}
      </h2>

      <div className="flex items-center gap-4">
        <Avatar className="size-20">
          {shown !== null && <AvatarImage src={shown} alt="" />}
          <AvatarFallback className="text-lg">{initialsOf(profile)}</AvatarFallback>
        </Avatar>

        <div className="flex flex-col gap-2">
          {/* A real `<input type="file">`, labelled — not a hidden input
              behind a `<div onClick>`. The native control is keyboard
              reachable, announces its accepted types, and opens the system
              picker on Enter; a div does none of that. */}
          <label htmlFor="avatar-file" className="text-sm font-medium">
            {profile.avatar_url === null
              ? t("profile.avatar.upload")
              : t("profile.avatar.replace")}
          </label>
          <input
            ref={input}
            id="avatar-file"
            type="file"
            accept={ACCEPTED_TYPES.join(",")}
            disabled={busy}
            aria-describedby="avatar-hint"
            className="text-sm file:mr-3 file:min-h-11 file:rounded-md file:border file:bg-transparent file:px-3 file:text-sm file:font-medium"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file !== undefined) void onFileChosen(file);
            }}
          />
          <p id="avatar-hint" className="text-muted-foreground text-xs">
            {t("profile.avatar.hint", { max: MAX_MB })}
          </p>
        </div>
      </div>

      {busy && (
        <p role="status" className="text-muted-foreground flex items-center gap-2 text-sm">
          <Spinner label={t("profile.avatar.uploading")} />
          {t("profile.avatar.uploading")}
        </p>
      )}

      {failure !== null && (
        <p role="alert" className="text-destructive text-sm font-medium">
          {t(failure, { max: MAX_MB })}
        </p>
      )}

      {profile.avatar_url !== null && (
        <Dialog open={confirmingRemoval} onOpenChange={setConfirmingRemoval}>
          <DialogTrigger asChild>
            <Button variant="outline" className="min-h-11 self-start" disabled={busy}>
              {t("profile.avatar.remove")}
            </Button>
          </DialogTrigger>
          {/* Radix's dialog: focus trap, focus return, Escape, aria-modal.
              A destructive action needs an explicit confirmation, and one
              that a keyboard user can escape from. */}
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("profile.avatar.removeTitle")}</DialogTitle>
              <DialogDescription>{t("profile.avatar.removeBody")}</DialogDescription>
            </DialogHeader>
            <div className="flex flex-wrap justify-end gap-2">
              <DialogClose asChild>
                <Button variant="ghost" className="min-h-11">
                  {t("profile.avatar.cancel")}
                </Button>
              </DialogClose>
              <Button
                variant="destructive"
                className="min-h-11"
                disabled={remove.isPending}
                onClick={() => void onRemove()}
              >
                {remove.isPending ? (
                  <Spinner label={t("profile.avatar.uploading")} />
                ) : (
                  t("profile.avatar.removeConfirm")
                )}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </section>
  );
}
