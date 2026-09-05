import { useCallback, useEffect, useId, useRef, useState } from "react";

import {
  type BroadcastAudience,
  type BroadcastView,
  fetchAudienceSize,
  sendBroadcast,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { Icon } from "@/shared/ui/icon";
import { Section } from "@/shared/ui/section";
import { InfoBanner } from "@/shared/ui/states";
import { useToast } from "@/shared/ui/toast";

/**
 * The broadcast composer — A64-027A §14–§18.
 *
 * Not a title, a textarea and a Send button. §12 rules that out explicitly,
 * and the reason is that this is the highest-reach control in the product:
 * one submission writes a row into every eligible inbox on the platform,
 * and there is no unsend.
 *
 * So it is a guided workflow with the audience decided first, the content
 * second, and a confirmation that restates both before anything happens.
 *
 * ## The recipient count is the server's
 *
 * §14. It is the number an administrator reads immediately before deciding
 * to address everybody, which makes a frontend estimate the most trusted
 * wrong number in the console. It is fetched, it is labelled as being "right
 * now", and when it cannot be fetched the composer says so rather than
 * showing a plausible figure.
 *
 * ## One send per composition
 *
 * The idempotency key is minted when the form is first shown and again after
 * a successful send — never per submit. A double click, a slow network and
 * an impatient retry therefore all carry the same key, and the server
 * returns the broadcast it already made. The button is also disabled while
 * a send is in flight, which handles the common case without relying on the
 * server for it.
 *
 * ## What is deliberately absent
 *
 * No rich text, no image, no link field. The notification domain stores
 * plain text and a destination from a closed set; a link field here would
 * be an open redirect written into every inbox, and a rich-text editor
 * would be markup the client either escapes or executes.
 */

const MAX_TITLE = 120;
const MAX_BODY = 600;

/** A short, unguessable key. Not a UUID import for one call site. */
function mintKey(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function BroadcastComposer({ onSent }: { onSent: (broadcast: BroadcastView) => void }) {
  const { t, locale } = useTranslation();
  const { notify } = useToast();
  const titleId = useId();
  const bodyId = useId();

  const [audience, setAudience] = useState<BroadcastAudience>("all_players");
  const [recipientsText, setRecipientsText] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [key, setKey] = useState(mintKey);

  const [size, setSize] = useState<number | null>(null);
  const [sizeFailed, setSizeFailed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    if (audience !== "all_players") {
      setSize(null);
      setSizeFailed(false);
      return;
    }
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;

    void fetchAudienceSize("all_players", next.signal).then((outcome) => {
      if (next.signal.aborted) return;
      if (outcome.status === "ok") {
        setSize(outcome.value.size);
        setSizeFailed(false);
        return;
      }
      // A count that could not be fetched is stated as unknown. Falling
      // back to a stale or invented number is how somebody sends to a
      // platform they think is a tenth of its size.
      setSize(null);
      setSizeFailed(true);
    });

    return () => {
      next.abort();
    };
  }, [audience]);

  const recipients = parseRecipients(recipientsText);
  const recipientProblem =
    audience === "specific_players" && recipientsText.trim() !== "" && recipients === null;

  const ready =
    title.trim().length > 0 &&
    title.length <= MAX_TITLE &&
    body.trim().length > 0 &&
    body.length <= MAX_BODY &&
    (audience === "all_players" || (recipients !== null && recipients.length > 0));

  const send = useCallback(async () => {
    setSending(true);
    setError(null);
    const outcome = await sendBroadcast({
      title: title.trim(),
      body: body.trim(),
      locale,
      audience,
      recipients: audience === "specific_players" ? (recipients ?? []) : [],
      idempotency_key: key,
    });
    setSending(false);

    if (outcome.status === "ok") {
      setConfirming(false);
      notify(t("broadcast.sent"), "success");
      onSent(outcome.value);
      setTitle("");
      setBody("");
      setRecipientsText("");
      // A fresh key: the next composition is a different broadcast, and
      // reusing this one would have the server return the last send.
      setKey(mintKey());
      return;
    }
    // The dialog stays open. The operator reads the failure beside what
    // they were confirming, and a retry carries the same idempotency key —
    // so a send that actually succeeded before the response was lost
    // returns the existing broadcast rather than making a second one.
    setError(
      t(outcome.status === "refused" ? "broadcast.errorRefused" : "broadcast.errorUnavailable"),
    );
    notify(t("broadcast.errorToast"), "danger");
  }, [audience, body, key, locale, notify, onSent, recipients, t, title]);

  const audienceLabel =
    audience === "all_players"
      ? size === null
        ? t("broadcast.audienceAllUnknown")
        : t("broadcast.audienceAllCount", { count: formatCount(size, locale) })
      : t("broadcast.audienceNamedCount", {
          count: String(recipients === null ? 0 : recipients.length),
        });

  return (
    <>
      <div className="composer">
        <div className="composer__steps">
          <Section title={t("broadcast.step1")} description={t("broadcast.step1Hint")}>
            {/* Cards rather than a segmented control: the two audiences
                differ by three orders of magnitude, and a choice that
                consequential should not look like a view toggle. */}
            <fieldset className="choices">
              <legend className="field__label">{t("broadcast.audience")}</legend>
              <button
                type="button"
                className="choice"
                aria-pressed={audience === "all_players"}
                onClick={() => {
                  setAudience("all_players");
                }}
              >
                <span className="choice__glyph">
                  <Icon name="users" size={17} />
                </span>
                <span className="choice__text">
                  <strong>{t("broadcast.audienceAll")}</strong>
                  <span>{t("broadcast.audienceAllHint")}</span>
                </span>
              </button>
              <button
                type="button"
                className="choice"
                aria-pressed={audience === "specific_players"}
                onClick={() => {
                  setAudience("specific_players");
                }}
              >
                <span className="choice__glyph">
                  <Icon name="search" size={17} />
                </span>
                <span className="choice__text">
                  <strong>{t("broadcast.audienceNamed")}</strong>
                  <span>{t("broadcast.audienceNamedHint")}</span>
                </span>
              </button>
            </fieldset>

            {audience === "all_players" ? (
              sizeFailed ? (
                <InfoBanner tone="warning">{t("broadcast.audienceUnavailable")}</InfoBanner>
              ) : (
                /* The scope of a platform-wide send, given the weight it
                   has. Attention, not alarm: this is a consequential
                   action, not an error, and a red panel here would make the
                   console cry wolf on a routine announcement (§23). */
                <p className="reach">
                  <span className="reach__glyph">
                    <Icon name="warning" size={18} />
                  </span>
                  <span className="reach__body">
                    <span className="reach__count">
                      {size === null
                        ? t("broadcast.audienceAllUnknown")
                        : formatCount(size, locale)}
                    </span>
                    <span className="reach__label">{t("broadcast.reachLabel")}</span>
                  </span>
                </p>
              )
            ) : (
              <div className="field">
                <label htmlFor="recipients">{t("broadcast.recipients")}</label>
                <span className="field__hint">{t("broadcast.recipientsHint")}</span>
                <textarea
                  id="recipients"
                  value={recipientsText}
                  aria-invalid={recipientProblem}
                  onChange={(event) => {
                    setRecipientsText(event.target.value);
                  }}
                />
                {recipientProblem ? (
                  <span className="field__error">
                    <Icon name="warning" size={15} />
                    {t("broadcast.recipientsInvalid")}
                  </span>
                ) : (
                  <span className="field__hint">{audienceLabel}</span>
                )}
              </div>
            )}
          </Section>

          <Section title={t("broadcast.step2")} description={t("broadcast.step2Hint")}>
            <div>
              <div className="field">
                <span className="field__labelrow">
                  <label htmlFor={titleId} className="field__label">
                    {t("broadcast.title")}
                  </label>
                  <span className="field__counter" data-over={title.length > MAX_TITLE}>
                    {title.length}/{MAX_TITLE}
                  </span>
                </span>
                <input
                  id={titleId}
                  value={title}
                  maxLength={MAX_TITLE + 20}
                  aria-invalid={title.length > MAX_TITLE}
                  onChange={(event) => {
                    setTitle(event.target.value);
                  }}
                />
              </div>

              <div className="field">
                <span className="field__labelrow">
                  <label htmlFor={bodyId} className="field__label">
                    {t("broadcast.body")}
                  </label>
                  <span className="field__counter" data-over={body.length > MAX_BODY}>
                    {body.length}/{MAX_BODY}
                  </span>
                </span>
                <span className="field__hint">{t("broadcast.bodyHint")}</span>
                <textarea
                  id={bodyId}
                  value={body}
                  maxLength={MAX_BODY + 50}
                  aria-invalid={body.length > MAX_BODY}
                  onChange={(event) => {
                    setBody(event.target.value);
                  }}
                />
              </div>
            </div>

            <div className="dialog-actions">
              <button
                type="button"
                className="action primary"
                disabled={!ready || sending}
                onClick={() => {
                  setConfirming(true);
                }}
              >
                <Icon name="send" size={16} />
                {t("broadcast.review")}
              </button>
            </div>
          </Section>
        </div>

        {/* The right column is what the send *will do*: how it reads, who
            it reaches, and on which channel. On a phone it follows the
            steps rather than disappearing — an operator about to address
            the platform should see the message before the button. */}
        <aside className="composer__aside">
          <Preview title={title} body={body} />
          <DeliverySummary audienceLabel={audienceLabel} />
        </aside>
      </div>

      <ConfirmDialog
        open={confirming}
        title={t("broadcast.confirmTitle")}
        description={t("broadcast.confirmHint")}
        confirmLabel={t("broadcast.send")}
        busy={sending}
        error={error}
        onCancel={() => {
          setConfirming(false);
          setError(null);
        }}
        onConfirm={() => {
          void send();
        }}
      >
        {/* Everything the send will do, restated. §18: an administrator
            confirms what they are about to do, not that they clicked. */}
        <dl className="facts">
          <dt>{t("broadcast.audience")}</dt>
          <dd>{audienceLabel}</dd>
          <dt>{t("broadcast.channel")}</dt>
          <dd>{t("broadcast.channelInApp")}</dd>
          <dt>{t("broadcast.title")}</dt>
          <dd>{title}</dd>
          <dt>{t("broadcast.body")}</dt>
          <dd>{body}</dd>
        </dl>
        {audience === "all_players" && (
          <InfoBanner tone="warning">{t("broadcast.confirmAll")}</InfoBanner>
        )}
      </ConfirmDialog>
    </>
  );
}

/**
 * What the send will do — A64-027A.4 §21, §26.
 *
 * Beside the preview rather than only inside the confirmation dialog: the
 * audience and the channel are the two facts that decide whether this is a
 * routine notice or a message to the whole platform, and an operator should
 * see them while they are writing rather than after they have finished.
 */
function DeliverySummary({ audienceLabel }: { audienceLabel: string }) {
  const { t } = useTranslation();
  return (
    <div className="summary-card">
      <p className="summary-card__title">{t("broadcast.summaryTitle")}</p>
      <dl className="facts">
        <dt>{t("broadcast.audience")}</dt>
        <dd>{audienceLabel}</dd>
        <dt>{t("broadcast.channel")}</dt>
        <dd>{t("broadcast.channelInApp")}</dd>
      </dl>
      {/* The preference rule, stated where the decision is made. §15: an
          administrator does not get a way around a player's own choice, and
          the gap between "eligible" and "delivered" is that choice. */}
      <p className="summary-card__note">{t("broadcast.preferenceNote")}</p>
    </div>
  );
}

/**
 * How the notification will read in a player's inbox.
 *
 * An approximation of one surface, not three: in-app is the only channel a
 * broadcast uses, so an "email preview" and a "push preview" beside it would
 * be previews of things this build does not send — §17 permits a preview
 * per **real** channel and no others.
 *
 * It renders text, and only text. Nothing here interprets markup, which is
 * the same guarantee the player client gives and the reason the preview is
 * an honest one.
 */
function Preview({ title, body }: { title: string; body: string }) {
  const { t } = useTranslation();
  return (
    <aside className="preview" aria-label={t("broadcast.preview")}>
      <p className="preview__caption">{t("broadcast.preview")}</p>
      <div className="preview__frame">
        <div className="preview__row">
          <span className="preview__glyph" aria-hidden="true">
            <Icon name="notifications" size={16} />
          </span>
          <div className="preview__text">
            <strong>{title.trim() === "" ? t("broadcast.previewTitle") : title}</strong>
            <span>{body.trim() === "" ? t("broadcast.previewBody") : body}</span>
          </div>
        </div>
      </div>
      <p className="muted preview__note">{t("broadcast.previewNote")}</p>
    </aside>
  );
}

/**
 * The pasted recipient list, as ids.
 *
 * `null` means "this is not a list of ids" — a distinct answer from an
 * empty list, so the field can say *why* it is refusing rather than
 * silently treating a typo as nobody.
 */
function parseRecipients(text: string): string[] | null {
  const trimmed = text.trim();
  if (trimmed === "") return [];

  const parts = trimmed
    .split(/[\s,;]+/)
    .map((part) => part.trim())
    .filter((part) => part !== "");

  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!parts.every((part) => uuid.test(part))) return null;

  // De-duplicated: §14 forbids the same person appearing twice, and a
  // paste from a spreadsheet is exactly where that happens.
  return [...new Set(parts.map((part) => part.toLowerCase()))];
}

function formatCount(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(value);
}
