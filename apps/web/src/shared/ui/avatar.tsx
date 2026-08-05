import * as AvatarPrimitive from "@radix-ui/react-avatar";
import type { ComponentProps } from "react";

import { cn } from "@/shared/lib/cn";

/**
 * A player's picture, with the placeholder that is shown while it loads
 * and when it never does.
 *
 * Radix handles the part that is easy to get wrong: the fallback appears
 * only after the image has actually failed or has not arrived within
 * `delayMs`, so a fast connection never flashes initials before the photo.
 *
 * No player, handle or avatar URL is known to this component. Wiring it to
 * a profile belongs to the phase that has profiles — this is the
 * primitive, not the widget.
 */
export function Avatar({ className, ...props }: ComponentProps<typeof AvatarPrimitive.Root>) {
  return (
    <AvatarPrimitive.Root
      data-slot="avatar"
      className={cn("relative flex size-8 shrink-0 overflow-hidden rounded-full", className)}
      {...props}
    />
  );
}

export function AvatarImage({
  className,
  ...props
}: ComponentProps<typeof AvatarPrimitive.Image>) {
  return (
    <AvatarPrimitive.Image
      data-slot="avatar-image"
      className={cn("aspect-square size-full", className)}
      {...props}
    />
  );
}

export function AvatarFallback({
  className,
  ...props
}: ComponentProps<typeof AvatarPrimitive.Fallback>) {
  return (
    <AvatarPrimitive.Fallback
      data-slot="avatar-fallback"
      className={cn(
        "bg-muted text-muted-foreground flex size-full items-center justify-center rounded-full text-xs font-medium",
        className,
      )}
      {...props}
    />
  );
}
