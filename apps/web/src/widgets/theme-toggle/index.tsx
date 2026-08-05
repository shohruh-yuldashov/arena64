import { LaptopIcon, MoonIcon, SunIcon } from "lucide-react";

import { type ThemeMode, THEMES, useTheme } from "@/shared/theme/theme-context";
import { Button } from "@/shared/ui";

const ICONS: Record<ThemeMode, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: LaptopIcon,
};

const LABELS: Record<ThemeMode, string> = {
  light: "Light theme",
  dark: "Dark theme",
  system: "Follow system theme",
};

/**
 * Three explicit choices rather than one cycling button.
 *
 * A single button that rotates light → dark → system cannot say what it
 * will do next, and `system` is invisible in it: a user who wants "follow
 * my OS" has to click until they land on it and then guess whether they
 * did. A radio group states all three and marks the current one.
 *
 * `aria-pressed` rather than a `role="radiogroup"`: these are toggle
 * buttons, and Radix is not needed for three buttons that are already
 * keyboard-reachable in DOM order.
 */
export function ThemeToggle() {
  const { mode, setMode } = useTheme();

  return (
    <div className="flex items-center gap-1" role="group" aria-label="Theme">
      {THEMES.map((candidate) => {
        const Icon = ICONS[candidate];
        return (
          <Button
            key={candidate}
            type="button"
            size="icon"
            variant={mode === candidate ? "secondary" : "ghost"}
            aria-pressed={mode === candidate}
            aria-label={LABELS[candidate]}
            onClick={() => setMode(candidate)}
          >
            <Icon aria-hidden="true" />
          </Button>
        );
      })}
    </div>
  );
}
