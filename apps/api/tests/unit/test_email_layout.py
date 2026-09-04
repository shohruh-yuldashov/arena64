"""What the email shell guarantees — A64-025.10F §31.

Not a snapshot of the markup. A test that pins the exact bytes of a design
fails on every visual change and asserts nothing about whether the design
works, which is the opposite of what a test is for. These pin the four
properties that are *contracts* — the ones that would be silently broken by
somebody tidying the shell, and that no reviewer would catch by eye.

The rendered messages themselves are reviewed by looking at them, in every
language, in both parts. That is not something a test can do.
"""

from app.core.enums import Locale
from app.modules.auth.application.verification_email import build_verification_code_email
from app.platform.email.layout import EmailAction, render_email_body


class TestThePreviewLine:
    """The line a mail client shows beside the subject, before anything is opened."""

    def test_the_verification_code_never_reaches_the_preview(self) -> None:
        """The reason the code is kept out of the subject, applied to the other door.

        A preview line is displayed by every notification surface a phone
        has — a lock screen, a watch, a preview pane — so a code in one is a
        code a shoulder-surfer reads without unlocking anything. The subject
        has been protected since A64-021.5H; a preheader is the second way
        the same value could get there.
        """
        message = build_verification_code_email(
            code="482913", recipient_name="Shohruh", locale=Locale.UZ
        )

        head, _, _ = message["html_body"].partition("</div>")
        assert "482913" not in head
        assert "482913" in message["html_body"]

    def test_the_preview_is_not_repeated_in_the_text_part(self) -> None:
        """In the text part the preheader *is* the first line, so it is not added.

        A plain-text client has no message list to preview into. Emitting it
        there would open every message with its second sentence stated twice.
        """
        text, _ = render_email_body(
            paragraphs=["Hello Shohruh,"], preheader="Somebody asked to reset your password."
        )

        assert text.count("Somebody asked to reset your password.") == 0
        assert text.startswith("Hello Shohruh,")


class TestTheTwoParts:
    def test_the_text_part_is_never_escaped(self) -> None:
        """`Bob & Sons` reaches a plain-text client as `Bob & Sons`.

        Escaping a string that is never parsed as markup is the defect this
        rule exists for, and it is invisible in review because it lives in
        the half of the message nobody opens.
        """
        text, _ = render_email_body(paragraphs=["You are registered for Bob & Sons."])

        assert "Bob & Sons" in text
        assert "&amp;" not in text

    def test_the_html_part_escapes_at_the_interpolation_site(self) -> None:
        _, html = render_email_body(paragraphs=["<script>alert(1)</script>"])

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_plain_text_reader_can_reach_the_action(self) -> None:
        """The URL bare, on its own, because there is nothing to click.

        A text part that says "choose a new password" and hides the link in
        an anchor it cannot render is a message that cannot be acted on.
        """
        text, _ = render_email_body(
            paragraphs=["Choose a new one:"],
            action=EmailAction(label="Choose a new password", url="https://arena64.gg/reset?t=abc"),
        )

        assert "https://arena64.gg/reset?t=abc" in text

    def test_the_heading_is_in_both_parts(self) -> None:
        text, html = render_email_body(paragraphs=["Hello."], heading="Reset your password")

        assert text.startswith("Reset your password")
        assert "Reset your password" in html


class TestTheMasthead:
    def test_the_gradient_always_has_a_solid_colour_beneath_it(self) -> None:
        """§31.2 — the condition on which the gradient was allowed back.

        A64-025.10E banned gradients from email because a client that drops
        one paints white on white. They are permitted again *only* because
        `bgcolor` and `background-color` are declared first, so the failure
        mode is solid indigo rather than nothing. Remove either and the ban
        was the right call again — which is what this asserts.
        """
        _, html = render_email_body(paragraphs=["Hello."])

        masthead, _, _ = html.partition("Arena64</span>")
        assert 'bgcolor="#494fcc"' in masthead
        assert "background-color:#494fcc" in masthead
        assert masthead.index("background-color:#494fcc") < masthead.index("background-image:")

    def test_no_image_is_ever_requested(self) -> None:
        """A blocked logo is a broken icon; a loaded one is a tracking pixel."""
        _, html = render_email_body(
            paragraphs=["Hello."], action=EmailAction(label="Open", url="https://arena64.gg/x")
        )

        assert "<img" not in html
        assert "<script" not in html
        assert "<link" not in html


class TestTheDocument:
    def test_the_language_reaches_the_document(self) -> None:
        """So a screen reader pronounces a Russian message in Russian."""
        _, html = render_email_body(paragraphs=["Здравствуйте."], language=Locale.RU.value)

        assert '<html lang="ru">' in html

    def test_layout_tables_are_hidden_from_assistive_technology(self) -> None:
        """Otherwise every message opens with "table, three rows"."""
        _, html = render_email_body(paragraphs=["Hello."])

        assert html.count("<table") == html.count('role="presentation"')
