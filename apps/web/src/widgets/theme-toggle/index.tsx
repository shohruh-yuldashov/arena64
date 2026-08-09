import { LaptopIcon, MoonIcon, SunIcon } from "lucide-react";

import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { type ThemeMode, THEMES, useTheme } from "@/shared/theme/theme-context";
import { Button } from "@/shared/ui";

const ICONS: Record<ThemeMode, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: LaptopIcon,
};

// The keys existed from A64-020.1 and nothing used them: the control was
// the last hardcoded English in the shell (A64-025.3 §13).
const LABELS: Record<ThemeMode, TranslationKey> = {
  light: "theme.light",
  dark: "theme.dark",
  system: "theme.system",
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
  const { t } = useTranslation();
  const { mode, setMode } = useTheme();

  return (
    <div className="flex items-center gap-1" role="group" aria-label={t("theme.toggle")}>
      {THEMES.map((candidate) => {
        const Icon = ICONS[candidate];
        return (
          <Button
            key={candidate}
            type="button"
            size="icon"
            variant={mode === candidate ? "secondary" : "ghost"}
            aria-pressed={mode === candidate}
            aria-label={t(LABELS[candidate])}
            onClick={() => setMode(candidate)}
          >
            <Icon aria-hidden="true" />
          </Button>
        );
      })}
    </div>
  );
}
