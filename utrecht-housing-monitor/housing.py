"""Check multiple housing sites once; scheduling is handled by GitHub Actions."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import re
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import fizz
import monitor

LOG = logging.getLogger("housing")
ROOT = Path(__file__).resolve().parent
STATE_FILE = Path("data/housing-state.json")
PROVIDERS = {"canvas": monitor.URL, "fizz": fizz.URL}


def load_sites(path: Path) -> list[dict]:
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {"sites"} or not isinstance(config["sites"], list):
        raise ValueError("sites.json must contain a sites list.")
    sites, ids = [], set()
    for site in config["sites"]:
        if (not isinstance(site, dict) or set(site) != {"id", "provider", "name", "enabled"}
                or not isinstance(site["id"], str) or not re.fullmatch(r"[a-z0-9-]{1,60}", site["id"])
                or site["provider"] not in PROVIDERS
                or not isinstance(site["name"], str) or not site["name"].strip()
                or len(site["name"]) > 100 or "\n" in site["name"]
                or type(site["enabled"]) is not bool or site["id"] in ids):
            raise ValueError("Invalid or duplicate site configuration.")
        ids.add(site["id"])
        if site["enabled"]:
            sites.append(site)
    if not sites:
        raise ValueError("Enable at least one site in sites.json.")
    return sites


def empty_state() -> dict:
    return {"version": 2, "sites": {}}


def validate_state(state: dict) -> dict:
    """Allow only public listing data and numeric retry metadata in cloud state."""
    if (not isinstance(state, dict) or set(state) != {"version", "sites"}
            or state["version"] != 2 or not isinstance(state["sites"], dict)):
        raise ValueError("Invalid housing state; do not reset it silently.")
    for site_id, entry in state["sites"].items():
        if (not re.fullmatch(r"[a-z0-9-]{1,60}", site_id) or not isinstance(entry, dict)
                or set(entry) != {"listings", "failures", "next_check_at", "last_error_email"}
                or not isinstance(entry["listings"], dict)):
            raise ValueError("Invalid site state.")
        for field in ("failures", "next_check_at", "last_error_email"):
            if type(entry[field]) is not int or entry[field] < 0:
                raise ValueError("Invalid retry state.")
        for key, item in entry["listings"].items():
            if (not isinstance(item, dict) or set(item) != {"key", "name", "url", "count"}
                    or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{24}", key)
                    or item["key"] != key or not isinstance(item["name"], str)
                    or len(item["name"]) > 300 or not isinstance(item["url"], str)
                    or (item["count"] is not None and
                        (type(item["count"]) is not int or item["count"] < 0))):
                raise ValueError("Invalid listing state.")
            if item["url"] != fizz.BOOKING_URL and monitor.clean_link(item["url"]) != item["url"]:
                raise ValueError("Listing state contains an unexpected URL.")
    return state


def load_state(path: Path) -> dict:
    return validate_state(json.loads(path.read_text())) if path.exists() else empty_state()


def entry_for(state: dict, site_id: str) -> dict:
    return state["sites"].setdefault(site_id, {
        "listings": {}, "failures": 0, "next_check_at": 0, "last_error_email": 0})


def run_checks(sites, fetchers, state, persist, *, sender=monitor.send_email,
               dry_run=False, status_email=False, now=None) -> int:
    """Isolate site failures and acknowledge each alert only after SMTP succeeds."""
    now = int(time.time()) if now is None else now
    failed = False
    reports = []
    for site in sites:
        entry = entry_for(state, site["id"])
        if not (dry_run or status_email) and now < entry["next_check_at"]:
            LOG.warning("%s: retry backoff; availability unknown.", site["name"])
            failed = True
            continue
        try:
            current = fetchers[site["id"]]()
            LOG.info("%s: %d bookable studio categories.", site["name"], len(current))
            reports.append(f"{site['name']}: {len(current)} bookable studio categories.\n"
                           + "\n".join(f"- {item.name}\n{item.url}" for item in current)
                           + "\n" + PROVIDERS[site["provider"]])
            if dry_run or status_email:
                continue
            added = monitor.new_listings(current, entry["listings"])
            if added:
                details = "\n".join(f"- {item.name}\n{item.url}" for item in added)
                sender("there is a studio available",
                       f"there is a studio available\n\n{site['name']}\n{details}\n\n"
                       "Check move-in dates, eligibility and booking details on the website.\n"
                       "This is a category-level availability signal, not a reservation.")
                LOG.info("%s: availability email accepted by the mail server.", site["name"])
            entry.update(listings={item.key: asdict(item) for item in current},
                         failures=0, next_check_at=0, last_error_email=0)
        except Exception as error:
            failed = True
            # SMTP errors can contain recipient addresses. Keep exception
            # messages and tracebacks out of the public Actions log and state.
            LOG.error("%s: check or delivery failed (%s); availability unknown.",
                      site["name"], type(error).__name__)
            reports.append(f"{site['name']}: availability unknown ({type(error).__name__}).\n"
                           + PROVIDERS[site["provider"]])
            if dry_run or status_email:
                continue
            entry["failures"] += 1
            entry["next_check_at"] = now + min(300 * 2 ** min(entry["failures"] - 1, 2), 900)
            if entry["failures"] >= 5 and (not entry["last_error_email"]
                                              or now - entry["last_error_email"] >= 86400):
                try:
                    sender(f"Housing monitor needs attention: {site['name']}",
                           "Five or more checks or email deliveries failed. Availability is unknown.\n"
                           "Review the GitHub Actions run summary.\n" + PROVIDERS[site["provider"]])
                    entry["last_error_email"] = now
                except Exception as mail_error:
                    LOG.error("Could not send health email (%s).", type(mail_error).__name__)
        if not (dry_run or status_email):
            # A persistence failure must stop the run; otherwise subsequent
            # alerts could be sent with no durable acknowledgment.
            persist(validate_state(state))
    if status_email:
        sender("Housing monitor: status report", "\n\n".join(reports))
        LOG.info("Status email accepted by the mail server.")
    return int(failed)


def simulate(sites, sender=monitor.send_email):
    payload = json.loads((ROOT / "tests/fixtures/fizz_available.json").read_text())
    snapshots = {"canvas": monitor.parse_listings(
        (ROOT / "tests/fixtures/studio_available.html").read_text()),
        "fizz": fizz.parse_listings(payload)}

    def send_test(subject, body):
        sender("TEST ONLY: " + subject, "SIMULATION: made-up availability, not a real vacancy.\n\n" + body)

    fetchers = {site["id"]: (lambda provider=site["provider"]: snapshots[provider]) for site in sites}
    return run_checks(sites, fetchers, empty_state(), lambda state: None, sender=send_test)


def fetch_canvas(*, headed=False):
    # Initialize inside the individual check so even a Playwright startup
    # failure cannot prevent the independent FIZZ request.
    with sync_playwright() as playwright:
        browser = monitor.BrowserFetcher(playwright, headed=headed)
        try:
            return browser.fetch()
        finally:
            browser.close()


def main(argv=None):
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="One check of each enabled website (default).")
    mode.add_argument("--test-email", action="store_true")
    mode.add_argument("--simulate-alert", action="store_true")
    mode.add_argument("--status-email", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="No email or state changes.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--site", help="Check a single configured site ID.")
    parser.add_argument("--config", type=Path, default=ROOT / "sites.json")
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    args = parser.parse_args(argv)
    try:
        if args.dry_run and (args.test_email or args.simulate_alert or args.status_email):
            raise ValueError("Dry run cannot be combined with an email testing mode.")
        sites = load_sites(args.config)
        if args.site:
            sites = [site for site in sites if site["id"] == args.site]
            if not sites:
                raise ValueError("The selected site is missing or disabled.")
        if not args.dry_run:
            monitor.validate_email_config()
        if args.test_email:
            monitor.send_email("Housing monitor: test email",
                               "Gmail is configured for the housing monitor.\n"
                               "This test does not check website availability.")
            LOG.info("Test email accepted by the mail server.")
            return 0
        if args.simulate_alert:
            return simulate(sites)
        with monitor.state_lock(args.state):
            fetchers = {}
            # Start Playwright only when needed; FIZZ uses its public JSON feed.
            for site in sites:
                if site["provider"] == "canvas":
                    fetchers[site["id"]] = lambda: fetch_canvas(headed=args.headed)
                else:
                    fetchers[site["id"]] = fizz.fetch
            return run_checks(sites, fetchers, load_state(args.state),
                              lambda state: monitor.save_state(args.state, state),
                              dry_run=args.dry_run, status_email=args.status_email)
    except Exception as error:
        LOG.error("Monitor stopped (%s). Check configuration, secrets and state.", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
