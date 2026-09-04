import { expect, test } from "@playwright/test";

/**
 * The motion system, measured in a real browser — A64-025.12 §34.
 *
 * This suite exists here rather than in jsdom for one reason: **jsdom does
 * not resolve a stylesheet.** A unit test can assert that
 * `[data-motion="instant"] { --motion-scale: 0 }` is written in
 * `globals.css`; only a browser can say what a `transition-colors` element
 * then computes to. P3-5 is a defect about what the browser does, so the
 * test that closes it has to be one.
 *
 * Every number below was read off the running page before it was written
 * here, not predicted from the source.
 */

/** What a probe element with the given classes computes to, live. */
async function computed(
  page: import("@playwright/test").Page,
  motion: string | null,
): Promise<{
  scale: string;
  transition: string;
  pulse: string;
  spinner: string;
}> {
  return page.evaluate((mode) => {
    if (mode !== null) document.documentElement.dataset.motion = mode;
    else delete document.documentElement.dataset.motion;

    const probe = document.createElement("div");
    probe.className = "transition-colors";
    const pulse = document.createElement("div");
    pulse.className = "animate-pulse";
    const spinner = document.createElement("div");
    spinner.setAttribute("data-slot", "spinner");
    spinner.innerHTML = '<svg class="animate-spin"></svg>';
    document.body.append(probe, pulse, spinner);

    const animation = (el: Element) => {
      const s = getComputedStyle(el);
      return `${s.animationDuration} x${s.animationIterationCount}`;
    };
    const result = {
      scale: getComputedStyle(document.documentElement)
        .getPropertyValue("--motion-scale")
        .trim(),
      transition: getComputedStyle(probe).transitionDuration,
      pulse: animation(pulse),
      spinner: animation(spinner.querySelector("svg") as Element),
    };
    for (const el of [probe, pulse, spinner]) el.remove();
    return result;
  }, motion);
}

test.describe("the motion scale", () => {
  test("every speed the API offers reaches the stylesheet", async ({ page }) => {
    await page.goto("/");

    // `normal` is the absence of an override, which is why there is no
    // `[data-motion="normal"]` block: it would be a duplicate of `:root`
    // and the first pair to drift.
    expect(await computed(page, null)).toMatchObject({
      scale: "1",
      transition: "0.2s",
    });
    expect(await computed(page, "fast")).toMatchObject({ transition: "0.12s" });
    expect(await computed(page, "slow")).toMatchObject({ transition: "0.32s" });

    // `instant` disables motion rather than being a fourth speed — the
    // API's own words, and an accessibility setting rather than a taste.
    // It has to stop the keyframe animations too, which do not read a
    // token: a pulsing skeleton under "instant" was the gap this closes.
    const instant = await computed(page, "instant");
    expect(instant.scale).toBe("0");
    expect(instant.transition).toBe("0.001s");
    expect(instant.pulse).toBe("0.001s x1");
  });
});

test.describe("prefers-reduced-motion", () => {
  test("stops the interface, whatever the player's speed says", async ({ page }) => {
    // `emulateMedia` rather than the project-level option, so the setting
    // is visible in the test that depends on it rather than in a config
    // file three directories away.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");

    // P3-5. Before this, the operating system's setting reached nothing:
    // one `motion-reduce:animate-none` on the lobby's waiting card, and
    // every skeleton, dialog and hover transition in the product ignored
    // it.
    const os = await computed(page, null);
    expect(os.transition).toBe("0.001s");
    expect(os.pulse).toBe("0.001s x1");

    // The rule when both sources speak: **whichever asks for less motion
    // wins.** A player who chose `slow` on a machine set to reduce motion
    // gets none — the reduced-motion block is last on purpose, and moving
    // it above the attribute blocks would silently reverse this.
    expect(await computed(page, "slow")).toMatchObject({
      scale: "0",
      transition: "0.001s",
    });
  });

  test("leaves the spinner turning, because its motion is the message", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");

    // WCAG 2.3.3 exempts motion essential to what a control communicates,
    // and a spinner's rotation is the only thing telling a sighted reader
    // that work is still in flight — `Spinner`'s label is for screen
    // readers. Freezing it would trade one accessibility win for another.
    //
    // A 16px rotation is also far below the area that triggers vestibular
    // symptoms, which is what the media query is for.
    for (const mode of [null, "instant"]) {
      expect((await computed(page, mode)).spinner).toBe("1.2s xinfinite");
    }
  });
});
