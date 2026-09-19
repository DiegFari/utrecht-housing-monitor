"""Watch Canvas Utrecht's public studio availability. Python 3.10+, macOS/Linux."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import smtplib
import ssl
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import certifi
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

URL = "https://canvas-student.securerc.co.uk/onlineleasing/canvas-utrecht/floorplans.aspx"
EMPTY_MESSAGE = "floor plan details not available for this property"
AVAILABLE = {"availability", "check availability", "view availability"}
CONTACT = {"contact us", "contact"}
NAME_SELECTOR = "h2, h3, h4, .fp-name, .floorplan-name, .floorplan-title"
LOG = logging.getLogger("canvas")
RESULTS_READY_SCRIPT = """() => {
    const emptyMessage = 'floor plan details not available for this property';
    // Canvas deliberately hides this server-rendered empty-result paragraph.
    // Scope textContent to that paragraph: scripts also contain this wording,
    // even on pages with available listings, so body.textContent is unsafe.
    const hiddenEmptyResult = [...document.querySelectorAll(
        '#floorPlanDataContainer #FPhideMap > p')]
        .some(e => e.textContent.trim().toLowerCase().startsWith(emptyMessage));
    const visibleText = document.body.innerText.toLowerCase();
    return hiddenEmptyResult || visibleText.includes(emptyMessage) ||
        [...document.querySelectorAll('a, button, input[type=submit], input[type=button]')]
        .some(e => /^(availability|check availability|view availability|contact us|contact)$/i
            .test((e.value || e.textContent).trim()));
}"""


class PageChangedError(RuntimeError):
    """The response is not a recognized availability page; preserve old state."""


class VerificationRequiredError(PageChangedError):
    """The browser is still on a website verification page."""


def is_verification_page(title: str, text: str) -> bool:
    title = normalized(title).lower()
    text = normalized(text).lower()
    return (title.startswith("just a moment")
            or "performing security verification" in text
            or "verify you are human" in text
            or "verifying you are human" in text
            or "enable javascript and cookies to continue" in text)


def normalized(value: str) -> str:
    return " ".join(value.split()).strip()


@dataclass(frozen=True)
class Listing:
    key: str
    name: str
    url: str
    count: int | None = None


def clean_link(href: str) -> str:
    """Keep stable listing parameters, excluding advertising and session tokens."""
    parts = urlsplit(urljoin(URL, href))
    if parts.scheme != "https" or parts.netloc != urlsplit(URL).netloc:
        return URL
    allowed = {"floorplanid", "floorplan", "unitid", "apartmentid", "propertyid"}
    query = [(k.lower(), v) for k, v in parse_qsl(parts.query) if k.lower() in allowed]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))


def is_hidden(element) -> bool:
    for node in [element, *element.parents]:
        style = re.sub(r"\s+", "", node.get("style", "").lower())
        if (node.has_attr("hidden") or node.get("aria-hidden") == "true"
                or "display:none" in style or "visibility:hidden" in style):
            return True
    return False


def parse_listings(html: str) -> list[Listing]:
    """Require a booking action inside a named card, never a keyword in page copy."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, template"):
        node.decompose()
    title = normalized(soup.title.get_text(" ")) if soup.title else ""
    text = normalized(soup.get_text(" ")).lower()
    if "canvas utrecht" not in title.lower() or "floor plans" not in text:
        raise PageChangedError("Canvas page not recognized (possibly a bot challenge or error page).")

    found: dict[str, Listing] = {}
    recognized_cards = 0
    for action in soup.select("a, button, input[type=button], input[type=submit]"):
        label = normalized(action.get("value") or action.get_text(" ")).lower()
        if label not in AVAILABLE | CONTACT or is_hidden(action):
            continue
        if action.has_attr("disabled") or action.get("aria-disabled") == "true":
            continue
        card = None
        name = None
        for parent in action.parents:
            if parent.name in {"body", "html", "[document]"}:
                break
            names = {normalized(h.get_text(" ")) for h in parent.select(NAME_SELECTOR)
                     if re.search(r"\b(studio|apartment)\b", h.get_text(" "), re.I)
                     and not is_hidden(h)}
            # Do not associate one card's button with names in adjacent cards.
            if len(names) > 1:
                break
            if len(names) == 1:
                card, name = parent, names.pop()
                break
        if card is None:
            # Navigation links are not listing cards. A real card we cannot map
            # should not silently be interpreted as unavailable.
            if label in AVAILABLE and not action.find_parent(["nav", "header", "footer"]):
                raise PageChangedError("An availability button has an unfamiliar card layout.")
            continue
        recognized_cards += 1
        if label not in AVAILABLE or not re.search(r"\bstudio\b", name, re.I):
            continue
        link = clean_link(action.get("href", ""))
        params = dict(parse_qsl(urlsplit(link).query))
        identity = (params.get("unitid") or params.get("apartmentid")
                    or params.get("floorplanid") or params.get("floorplan")
                    or card.get("data-floorplanid") or card.get("data-floorplan-id")
                    or name.casefold())
        key = hashlib.sha256(str(identity).encode()).hexdigest()[:24]
        card_text = normalized(card.get_text(" "))
        count_match = re.search(r"\b(\d+)\s+(?:(?:units?|apartments?|studios?)\s+)?available\b",
                                card_text, re.I)
        if not count_match:
            count_match = re.search(r"\bavailable\s+(?:units?|apartments?|studios?)\s*:?\s*(\d+)\b",
                                    card_text, re.I)
        count = int(count_match.group(1)) if count_match else None
        if count == 0:
            continue
        found[key] = Listing(key, name, link, count)
    if EMPTY_MESSAGE in text and found:
        raise PageChangedError("Page contains conflicting empty and available results.")
    if not recognized_cards and EMPTY_MESSAGE not in text:
        raise PageChangedError("No recognized listing cards or explicit empty-results message.")
    return sorted(found.values(), key=lambda item: item.name)


def new_listings(current: list[Listing], previous: dict) -> list[Listing]:
    result = []
    for item in current:
        old = previous.get(item.key)
        if old is None or (item.count is not None and old.get("count") is not None
                           and item.count > old["count"]):
            result.append(item)
    return result


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "listings": {}}
    state = json.loads(path.read_text())
    if state.get("version") != 1 or not isinstance(state.get("listings"), dict):
        raise ValueError("Unrecognized state file; inspect it before restarting.")
    for key, item in state["listings"].items():
        if (not isinstance(key, str) or not isinstance(item, dict)
                or not {"name", "url", "count"} <= item.keys()
                or (item["count"] is not None and type(item["count"]) is not int)):
            raise ValueError("Damaged listing state; inspect it before restarting.")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another monitor is using this state file.") from exc
        yield


def validate_email_config() -> None:
    for name in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        value = os.environ.get(name, "")
        if not value or "replace-with" in value or "your-address" in value:
            raise ValueError(f"Set {name} in .env before sending email.")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} must be a single line.")


def create_tls_context() -> ssl.SSLContext:
    # Python.org macOS installs can have no default root certificates. Add
    # Mozilla's roots while retaining any configured system/organization roots.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def send_email(subject: str, body: str) -> None:
    validate_email_config()
    message = EmailMessage()
    message["From"] = os.environ["SMTP_USER"]
    message["To"] = os.environ["EMAIL_TO"]
    message["Subject"] = subject
    message.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    password = os.environ["SMTP_PASSWORD"]
    if host == "smtp.gmail.com":
        password = password.replace(" ", "")
    context = create_tls_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if port != 465:
            server.starttls(context=context)
        server.login(os.environ["SMTP_USER"], password)
        refused = server.send_message(message)
        if refused:
            raise RuntimeError("The mail server refused a recipient.")


def process_snapshot(current: list[Listing], state: dict, path: Path, sender=send_email) -> dict:
    added = new_listings(current, state["listings"])
    if added:
        lines = ["there is a studio available", "", "Canvas Utrecht:"]
        for item in added:
            suffix = f" ({item.count} available)" if item.count is not None else ""
            lines.extend([f"- {item.name}{suffix}", item.url])
        lines.extend(["", "Check the move-in date and booking details on the website.", URL])
        sender("there is a studio available", "\n".join(lines))
        LOG.info("Sent alert for %d newly available studio listing(s).", len(added))
    # Only acknowledge alerts after SMTP succeeds. Failed sends retry next check.
    updated = {"version": 1, "last_success": datetime.now(timezone.utc).isoformat(),
               "listings": {item.key: asdict(item) for item in current}}
    save_state(path, updated)
    return updated


def check_and_email_status(fetch, sender=send_email) -> int:
    """Email one live result, including empty results and failed checks."""
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        current = fetch()
    except Exception as exc:
        LOG.error("Website check failed: %s: %s", type(exc).__name__, exc)
        sender("Canvas check: availability unknown",
               "The website could not be checked. Availability is unknown.\n"
               "This does not mean that no studios are available.\n"
               "Possible causes include verification, a network error, or a changed page.\n"
               f"Error type: {type(exc).__name__}. See the terminal for details.\n"
               f"Checked at: {checked_at}\n{URL}")
        LOG.info("Failure report accepted by the mail server.")
        return 1
    if current:
        subject = f"Canvas check: {len(current)} bookable studio listing(s)"
        details = "\n".join(f"- {item.name}\n{item.url}" for item in current)
    else:
        subject = "Canvas check: no studios available"
        details = "The website was read successfully. No bookable studio listings were found."
    sender(subject, f"{details}\n\nChecked at: {checked_at}\n{URL}")
    LOG.info("Status report accepted by the mail server: %s", subject)
    return 0


def simulate_alert(sender=send_email) -> None:
    """Exercise parsing and notifications with a fake card and disposable state."""
    fixture = Path(__file__).resolve().parent / "tests" / "fixtures" / "studio_available.html"
    current = parse_listings(fixture.read_text())
    if not current:
        raise ValueError("The simulation fixture did not produce a studio listing.")

    def send_simulated(subject, body):
        sender("TEST ONLY: " + subject,
               "SIMULATION: this is a made-up studio, not a real vacancy.\n\n" + body)

    with tempfile.TemporaryDirectory(prefix="canvas-simulation-") as temporary:
        path = Path(temporary) / "state.json"
        process_snapshot(current, load_state(path), path, send_simulated)
    LOG.info("Simulated studio alert accepted by the mail server. Real listing state was untouched.")


class BrowserFetcher:
    def __init__(self, playwright, *, headed=False):
        self.playwright = playwright
        self.headed = headed
        self.browser = None
        self.page = None

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            finally:
                self.browser = self.page = None

    def record_failure(self, error: Exception) -> bool:
        """Save the last public page locally; return whether verification is visible."""
        title, body, html = "", "", ""
        if self.page is None:
            return False
        try:
            title = self.page.title()
            html = self.page.content()
            body = self.page.locator("body").inner_text(timeout=3000)
        except Exception as capture_error:
            LOG.warning("Could not read all page diagnostics: %s", type(capture_error).__name__)
        verification = is_verification_page(title, body)
        LOG.error("Browser stopped at title=%r; verification page=%s", title, verification)
        directory = Path("data/diagnostics")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            metadata = {"checked_at": datetime.now(timezone.utc).isoformat(),
                        "title": title, "verification_detected": verification,
                        "error_type": type(error).__name__, "body_excerpt": body[:2000]}
            (directory / "last-failure.html").write_text(html)
            (directory / "last-failure.json").write_text(json.dumps(metadata, indent=2))
            LOG.info("Saved page diagnostics to %s", directory.resolve())
            self.page.screenshot(path=str(directory / "last-failure.png"), timeout=5000)
            LOG.info("Saved screenshot to %s", (directory / "last-failure.png").resolve())
        except Exception as capture_error:
            LOG.warning("Could not save all page diagnostics: %s", type(capture_error).__name__)
        return verification

    def fetch(self) -> list[Listing]:
        if self.browser is None:
            self.browser = self.playwright.chromium.launch(
                channel=os.environ.get("BROWSER_CHANNEL", "chromium"), headless=not self.headed)
            self.page = self.browser.new_page()
            self.page.set_default_timeout(30000)
        try:
            self.page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            # Allow the normal browser to render the page. A challenge that
            # remains in place is an error, never an empty availability result.
            self.page.wait_for_function(
                "document.title.toLowerCase().includes('canvas utrecht')",
                timeout=120000 if self.headed else 30000)
            self.page.wait_for_function(RESULTS_READY_SCRIPT, timeout=15000)
            # Strip elements hidden by external CSS before parsing serialized HTML.
            html = self.page.evaluate("""() => {
                const copy = document.documentElement.cloneNode(true);
                const originals = document.querySelectorAll('*');
                const clones = [copy, ...copy.querySelectorAll('*')];
                originals.forEach((e, i) => {
                    const s = getComputedStyle(e);
                    if (s.display === 'none' || s.visibility === 'hidden')
                        clones[i].setAttribute('hidden', '');
                });
                return copy.outerHTML;
            }""")
            return parse_listings(html)
        except Exception as exc:
            verification = self.record_failure(exc)
            try:
                self.close()
            except Exception as close_error:
                LOG.warning("Browser cleanup failed: %s", type(close_error).__name__)
            if verification:
                raise VerificationRequiredError(
                    "Canvas is showing a security verification page instead of listings. "
                    "Availability is unknown. See data/diagnostics for the captured page.") from exc
            raise


def run(args) -> int:
    if args.dry_run and not args.once:
        raise ValueError("Use --dry-run together with --once.")
    if args.test_email:
        send_email("Canvas monitor: test email", "Your Canvas studio alert email is configured.\n"
                   "This is a test, not an availability alert.\n" + URL)
        LOG.info("Test email accepted by the mail server. Check your inbox and spam folder.")
        return 0
    if args.simulate_alert:
        validate_email_config()
        simulate_alert()
        return 0
    if args.status_email:
        validate_email_config()
        with sync_playwright() as playwright:
            fetcher = BrowserFetcher(playwright, headed=args.headed)
            try:
                return check_and_email_status(fetcher.fetch)
            finally:
                fetcher.close()
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
    if interval < 60:
        raise ValueError("CHECK_INTERVAL_SECONDS must be at least 60.")
    if not args.dry_run:
        validate_email_config()
    if not args.once:
        LOG.info("Monitoring every %d seconds; failed checks use a longer retry delay.", interval)
    state_path = Path(os.environ.get("STATE_FILE", "data/state.json"))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with state_lock(state_path), sync_playwright() as playwright:
        state = load_state(state_path)
        fetcher = BrowserFetcher(playwright, headed=args.headed)
        failures = 0
        last_error_email = None
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    current = fetcher.fetch()
                    LOG.info("Successful check: %d bookable studio listing(s).", len(current))
                    if args.dry_run:
                        print(json.dumps([asdict(item) for item in current], indent=2))
                    else:
                        state = process_snapshot(current, state, state_path)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    LOG.error("Check failed (%d): %s: %s", failures, type(exc).__name__, exc)
                    if args.once:
                        return 1
                    if failures >= 5 and (last_error_email is None
                                          or time.monotonic() - last_error_email > 86400):
                        try:
                            send_email("Canvas monitor needs attention",
                                       "Five or more checks failed. Availability is unknown.\n"
                                       "Check the monitor's server logs.\n" + URL)
                            last_error_email = time.monotonic()
                        except Exception as mail_error:
                            LOG.error("Could not send failure alert: %s", type(mail_error).__name__)
                if args.once:
                    return 0
                # Successful checks start at the configured interval. Back off on failures;
                # never hammer a blocked site or run overlapping checks.
                delay = interval if not failures else min(interval * 2 ** min(failures - 1, 4), 900)
                stop.wait(max(1, delay - (time.monotonic() - started)))
        finally:
            fetcher.close()
    return 0


def main() -> int:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Check once, then exit.")
    mode.add_argument("--test-email", action="store_true", help="Send a labeled test email and exit.")
    mode.add_argument("--simulate-alert", action="store_true",
                      help="Send a TEST ONLY studio alert using a fake listing; no website request.")
    mode.add_argument("--status-email", action="store_true",
                      help="Check once and email the result, even if empty or blocked; no state changes.")
    parser.add_argument("--dry-run", action="store_true", help="With --once: no email or state changes.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window for local diagnosis.")
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, OSError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
