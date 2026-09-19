"""Synthetic card layouts: real positive Canvas markup is not yet verified."""

from pathlib import Path
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from monitor import (EMPTY_MESSAGE, Listing, PageChangedError, clean_link, load_state,
                     parse_listings, process_snapshot, send_email, URL,
                     check_and_email_status, simulate_alert, create_tls_context,
                     is_verification_page, BrowserFetcher, VerificationRequiredError)


def page(content):
    return f"<html><head><title>Canvas Utrecht | RENTCafe</title></head><body><h1>Floor Plans</h1>{content}</body></html>"


def card(name="Classic Studio - High Rise", label="Availability", count="", href="availableunits.aspx?floorplanid=123", attributes=""):
    return f'<article {attributes}><h2>{name}</h2><p>{count}</p><a href="{href}">{label}</a></article>'


class ParsingTests(unittest.TestCase):
    def test_captured_canvas_hidden_empty_results(self):
        fixture = Path(__file__).parent / "fixtures" / "canvas_empty_results.html"
        self.assertEqual(parse_listings(fixture.read_text()), [])

    def test_empty_page_with_filter_names_and_explanatory_copy_is_empty(self):
        self.assertEqual(parse_listings(page(
            '<label>Classic Studio - High Rise</label>'
            '<p>Apartments with the “Availability” button are ready to be booked.</p>'
            + EMPTY_MESSAGE)), [])

    def test_bookable_studio_includes_count_and_link(self):
        items = parse_listings(page(card(count="2 units available")))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "Classic Studio - High Rise")
        self.assertEqual(items[0].count, 2)
        self.assertTrue(items[0].url.endswith("availableunits.aspx?floorplanid=123"))

    def test_contact_and_one_bedroom_do_not_trigger(self):
        self.assertEqual(parse_listings(page(card(label="Contact us") + card(
            name="One Bedroom Apartment - Low Rise"))), [])

    def test_contact_studio_does_not_inherit_nearby_apartment_availability(self):
        self.assertEqual(parse_listings(page(
            card(label="Contact us") + card(name="One Bedroom Apartment - Low Rise"))), [])

    def test_hidden_card_does_not_trigger(self):
        self.assertEqual(parse_listings(page(card(attributes='style="display: none"') + EMPTY_MESSAGE)), [])

    def test_disabled_booking_button_does_not_trigger(self):
        content = card().replace('<a href=', '<a aria-disabled="true" href=')
        self.assertEqual(parse_listings(page(content + EMPTY_MESSAGE)), [])

    def test_zero_available_does_not_trigger(self):
        self.assertEqual(parse_listings(page(card(count="0 available"))), [])

    def test_cloudflare_is_error_not_empty(self):
        with self.assertRaises(PageChangedError):
            parse_listings("<title>Just a moment...</title><p>Verify you are human</p>")

    def test_unknown_layout_is_error_not_empty(self):
        with self.assertRaises(PageChangedError):
            parse_listings(page('<div>Classic Studio</div><a>Availability</a>'))

    def test_missing_results_is_error_not_empty(self):
        with self.assertRaises(PageChangedError):
            parse_listings(page('<div>Loading...</div>'))

    def test_conflicting_empty_and_available_fails(self):
        with self.assertRaises(PageChangedError):
            parse_listings(page(card() + EMPTY_MESSAGE))

    def test_tracking_and_case_do_not_change_identity(self):
        first = parse_listings(page(card(href="availableunits.aspx?FloorPlanID=123&_gl=old")))
        second = parse_listings(page(card(href="availableunits.aspx?floorplanid=123&_gl=new")))
        self.assertEqual(first, second)

    def test_unsafe_links_fall_back_to_canonical_url(self):
        self.assertEqual(clean_link("javascript:doBooking()"), URL)
        self.assertEqual(clean_link("https://other.example/"), URL)

    def test_duplicate_mobile_and_desktop_cards_collapse(self):
        self.assertEqual(len(parse_listings(page(card() + card()))), 1)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.mail = Mock()
        self.item = Listing("studio-1", "Classic Studio", URL, 1)

    def process(self, items, state):
        return process_snapshot(items, state, self.path, self.mail)

    def test_first_detection_alerts_then_restart_does_not_repeat(self):
        self.process([self.item], load_state(self.path))
        self.process([self.item], load_state(self.path))
        self.mail.assert_called_once()
        self.assertEqual(self.mail.call_args.args[0], "there is a studio available")

    def test_disappearance_and_return_alert_again(self):
        state = self.process([self.item], load_state(self.path))
        state = self.process([], state)
        self.process([self.item], state)
        self.assertEqual(self.mail.call_count, 2)

    def test_new_studio_alerts_while_an_old_studio_remains(self):
        state = self.process([self.item], load_state(self.path))
        second = Listing("studio-2", "Premium Studio", URL)
        self.process([self.item, second], state)
        self.assertEqual(self.mail.call_count, 2)
        self.assertIn("Premium Studio", self.mail.call_args.args[1])
        self.assertNotIn("Classic Studio", self.mail.call_args.args[1])

    def test_count_increase_alerts_but_decrease_does_not(self):
        state = self.process([self.item], load_state(self.path))
        bigger = Listing(self.item.key, self.item.name, URL, 2)
        state = self.process([bigger], state)
        self.process([self.item], state)
        self.assertEqual(self.mail.call_count, 2)

    def test_mail_failure_does_not_acknowledge_listing(self):
        state = self.process([], load_state(self.path))
        before = self.path.read_text()
        self.mail.side_effect = smtplib.SMTPException("not delivered")
        with self.assertRaises(smtplib.SMTPException):
            self.process([self.item], state)
        self.assertEqual(self.path.read_text(), before)
        self.mail.side_effect = None
        self.process([self.item], load_state(self.path))
        self.assertEqual(len(load_state(self.path)["listings"]), 1)

    def test_broken_state_is_not_silently_reset(self):
        self.path.write_text('{"version": 1, "listings": {"id": {"count": "bad"}}}')
        with self.assertRaises(ValueError):
            load_state(self.path)

    def test_tls_is_enabled_before_gmail_login(self):
        env = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
               "SMTP_USER": "test@example.com", "SMTP_PASSWORD": "abcd efgh ijkl mnop",
               "EMAIL_TO": "recipient@example.com"}
        with patch.dict("os.environ", env), patch("monitor.smtplib.SMTP") as smtp:
            server = smtp.return_value
            server.send_message.return_value = {}
            send_email("Test", "Test message")
            self.assertEqual([call[0] for call in server.method_calls],
                             ["starttls", "login", "send_message"])
            server.login.assert_called_once_with("test@example.com", "abcdefghijklmnop")

    def test_tls_verifies_certificates_even_without_a_system_ca_bundle(self):
        empty_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty_context.get_ca_certs(), [])
        with patch("monitor.ssl.create_default_context", return_value=empty_context):
            context = create_tls_context()
        self.assertGreater(len(context.get_ca_certs()), 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class ManualTestingModesTests(unittest.TestCase):
    def test_successful_empty_check_still_sends_email(self):
        mail = Mock()
        self.assertEqual(check_and_email_status(lambda: [], mail), 0)
        mail.assert_called_once()
        self.assertEqual(mail.call_args.args[0], "Canvas check: no studios available")
        self.assertIn("read successfully", mail.call_args.args[1])

    def test_blocked_check_sends_unknown_not_empty(self):
        fetch = Mock(side_effect=PageChangedError("Verification required"))
        mail = Mock()
        with self.assertLogs("canvas", level="ERROR"):
            self.assertEqual(check_and_email_status(fetch, mail), 1)
        mail.assert_called_once()
        self.assertEqual(mail.call_args.args[0], "Canvas check: availability unknown")
        self.assertIn("does not mean", mail.call_args.args[1])

    def test_live_available_result_includes_studio_details(self):
        mail = Mock()
        item = Listing("id", "Classic Studio", URL)
        self.assertEqual(check_and_email_status(lambda: [item], mail), 0)
        self.assertIn("1 bookable studio", mail.call_args.args[0])
        self.assertIn(item.name, mail.call_args.args[1])

    def test_mail_failure_is_not_reported_as_website_failure(self):
        mail = Mock(side_effect=smtplib.SMTPException("Mail failed"))
        with self.assertRaises(smtplib.SMTPException):
            check_and_email_status(lambda: [], mail)
        mail.assert_called_once()

    def test_simulation_sends_labeled_alert_without_touching_real_state(self):
        mail = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            real_state = Path(temporary) / "real.json"
            real_state.write_text("existing state")
            with patch.dict("os.environ", {"STATE_FILE": str(real_state)}), \
                    patch("monitor.BrowserFetcher") as browser:
                simulate_alert(mail)
                browser.assert_not_called()
            self.assertEqual(real_state.read_text(), "existing state")
        mail.assert_called_once()
        self.assertEqual(mail.call_args.args[0], "TEST ONLY: there is a studio available")
        self.assertTrue(mail.call_args.args[1].startswith("SIMULATION:"))


class BrowserFailureTests(unittest.TestCase):
    def test_verification_detection_does_not_match_generic_cloudflare_footer(self):
        self.assertTrue(is_verification_page("Just a moment...", ""))
        self.assertTrue(is_verification_page("", "Performing security verification"))
        self.assertFalse(is_verification_page("Canvas Utrecht", "Powered by Cloudflare"))
        self.assertFalse(is_verification_page("Canvas Utrecht", "Loading floor plans..."))

    def test_timeout_on_challenge_reports_verification_and_closes_browser(self):
        fetcher = BrowserFetcher(Mock())
        browser = fetcher.browser = Mock()
        fetcher.page = Mock()
        fetcher.page.wait_for_function.side_effect = TimeoutError("Page timed out")
        with patch.object(fetcher, "record_failure", return_value=True):
            with self.assertRaises(VerificationRequiredError):
                fetcher.fetch()
        browser.close.assert_called_once()
        self.assertIsNone(fetcher.page)

    def test_generic_timeout_is_not_mislabeled_verification(self):
        fetcher = BrowserFetcher(Mock())
        fetcher.browser = Mock()
        fetcher.page = Mock()
        error = TimeoutError("Page timed out")
        fetcher.page.wait_for_function.side_effect = error
        with patch.object(fetcher, "record_failure", return_value=False):
            with self.assertRaises(TimeoutError) as raised:
                fetcher.fetch()
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
