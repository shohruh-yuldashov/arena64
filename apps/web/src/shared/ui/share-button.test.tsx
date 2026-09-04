import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/shared/i18n";
import { ShareButton } from "@/shared/ui/share-button";

/**
 * `ShareButton` — A64-026.4 §43.8.
 *
 * Three behaviours, and each is a way the control could be wrong: it does
 * nothing at all where neither API exists, it prefers the system sheet, and
 * it treats a dismissed sheet as a decision rather than a failure.
 */

function renderShare() {
  return render(
    <I18nProvider>
      <ShareButton title="Sunday Open" />
    </I18nProvider>,
  );
}

function stub(share: unknown, clipboard: unknown) {
  Object.defineProperty(navigator, "share", { value: share, configurable: true });
  Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
}

afterEach(() => {
  stub(undefined, undefined);
  vi.restoreAllMocks();
});

describe("ShareButton", () => {
  it("renders nothing where neither API exists", () => {
    // A button that cannot do its one job looks broken, and the URL is in
    // the address bar of every browser old enough to lack both.
    stub(undefined, undefined);

    renderShare();

    expect(screen.queryByRole("button")).toBeNull();
  });

  it("opens the system sheet when the platform has one", async () => {
    const share = vi.fn().mockResolvedValue(undefined);
    const writeText = vi.fn().mockResolvedValue(undefined);
    stub(share, { writeText });

    renderShare();
    await userEvent.click(await screen.findByRole("button"));

    expect(share).toHaveBeenCalledWith({ title: "Sunday Open", url: window.location.href });
    // Not both. Sharing and then copying would leave a "Link copied"
    // confirmation for something the sheet already handled.
    expect(writeText).not.toHaveBeenCalled();
  });

  it("copies, and says so, where there is no sheet", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stub(undefined, { writeText });

    renderShare();
    await userEvent.click(await screen.findByRole("button"));

    expect(writeText).toHaveBeenCalledWith(window.location.href);
    expect(await screen.findByRole("status")).toHaveTextContent(
      /copied|nusxalandi|скопирована/i,
    );
  });

  it("stays quiet when the sheet is dismissed", async () => {
    // `AbortError` is somebody changing their mind. Falling through to the
    // clipboard would copy a link they just decided not to send, and
    // reporting it would fill a log with cancellations.
    const share = vi.fn().mockRejectedValue(new DOMException("cancelled", "AbortError"));
    const writeText = vi.fn().mockResolvedValue(undefined);
    const reported = vi.spyOn(console, "error").mockImplementation(() => undefined);
    stub(share, { writeText });

    renderShare();
    await userEvent.click(await screen.findByRole("button"));

    expect(writeText).not.toHaveBeenCalled();
    expect(reported).not.toHaveBeenCalled();
  });

  it("falls back to the clipboard when the sheet genuinely fails", async () => {
    const share = vi.fn().mockRejectedValue(new Error("no permission"));
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    stub(share, { writeText });

    renderShare();
    await userEvent.click(await screen.findByRole("button"));

    expect(writeText).toHaveBeenCalledWith(window.location.href);
  });
});
