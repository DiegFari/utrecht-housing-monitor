import copy
import base64
from dataclasses import asdict
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import fizz
import github_state
import housing
from monitor import PageChangedError, save_state

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FizzTests(unittest.TestCase):
    def test_captured_empty_response(self):
        self.assertEqual(fizz.parse_listings(fixture("fizz_empty.json")), [])

    def test_offer_alert_matches_widget_booking_condition(self):
        result = fizz.parse_listings(fixture("fizz_available.json"))
        self.assertEqual(len(result), 1)
        self.assertIn("Single Studio", result[0].name)
        self.assertEqual(result[0].url, fizz.BOOKING_URL)

    def test_other_building_does_not_trigger(self):
        data = fixture("fizz_available.json")
        data["locations"][0]["buildings"][0]["lookupValue"] = "THE_FIZZ_LEIDEN"
        with self.assertRaises(PageChangedError):
            fizz.parse_listings(data)

    def test_static_description_is_not_an_offer(self):
        data = fixture("fizz_empty.json")
        data["locations"][0]["buildings"][0]["roomTypes"][0]["details"] = [
            {"type": "DESCRIPTION", "value": "Book now, available from EUR 999"}]
        self.assertEqual(fizz.parse_listings(data), [])

    def test_changed_missing_and_error_payloads_are_unknown(self):
        original = fixture("fizz_available.json")
        for data in ({}, {"status": "ERROR"}, {**original, "locations": []},
                     {**original, "settings": {"booking.strictGenders": True}}):
            with self.subTest(data=data), self.assertRaises(PageChangedError):
                fizz.parse_listings(data)
        for rent in ({}, "EUR 999", True):
            data = copy.deepcopy(original)
            data["locations"][0]["buildings"][0]["roomTypes"][0]["marketingRent"] = rent
            with self.subTest(rent=rent), self.assertRaises(PageChangedError):
                fizz.parse_listings(data)


class MultiSiteTests(unittest.TestCase):
    def setUp(self):
        self.sites = housing.load_sites(housing.ROOT / "sites.json")
        self.state = housing.empty_state()
        self.mail, self.persist = Mock(), Mock()
        self.items = fizz.parse_listings(fixture("fizz_available.json"))
        self.fetchers = {s["id"]: Mock(return_value=[]) for s in self.sites}

    def run_checks(self, **kwargs):
        with self.assertLogs("housing", level="INFO"):
            return housing.run_checks(self.sites, self.fetchers, self.state,
                                      self.persist, sender=self.mail, **kwargs)

    def test_one_site_failing_does_not_prevent_other_alert(self):
        self.fetchers["canvas-utrecht"].side_effect = PageChangedError("blocked")
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.assertEqual(self.run_checks(now=1000), 1)
        self.mail.assert_called_once()
        self.assertIn("THE FIZZ Utrecht", self.mail.call_args.args[1])
        self.assertEqual(self.state["sites"]["canvas-utrecht"]["failures"], 1)

    def test_restart_deduplication_and_reappearance(self):
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.run_checks(now=1000)
        self.state = json.loads(github_state.serialize(self.state))
        self.run_checks(now=1300)
        self.mail.assert_called_once()
        self.fetchers["fizz-utrecht"].return_value = []
        self.run_checks(now=1600)
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.run_checks(now=1900)
        self.assertEqual(self.mail.call_count, 2)

    def test_mail_failure_keeps_old_history_for_retry(self):
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.mail.side_effect = smtplib.SMTPException("private@example.com")
        self.assertEqual(self.run_checks(now=1000), 1)
        self.assertEqual(self.state["sites"]["fizz-utrecht"]["listings"], {})
        self.mail.side_effect = None
        self.run_checks(now=1300)
        self.assertTrue(self.state["sites"]["fizz-utrecht"]["listings"])

    def test_public_logs_and_state_do_not_include_smtp_error_details(self):
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.mail.side_effect = smtplib.SMTPException("private@example.com secret-value")
        with self.assertLogs("housing") as logs:
            housing.run_checks(self.sites, self.fetchers, self.state, self.persist,
                               sender=self.mail, now=1000)
        for output in (" ".join(logs.output), github_state.serialize(self.state)):
            self.assertNotIn("private@example.com", output)
            self.assertNotIn("secret-value", output)

    def test_backoff_and_daily_health_mail_survive_restarts(self):
        self.fetchers["canvas-utrecht"].side_effect = PageChangedError("blocked")
        for now in (1000, 1300, 1900, 2800, 3700):
            self.run_checks(now=now)
            self.state = json.loads(github_state.serialize(self.state))
        self.mail.assert_called_once()
        self.assertIn("needs attention", self.mail.call_args.args[0])
        self.run_checks(now=4000)
        self.assertEqual(self.fetchers["canvas-utrecht"].call_count, 5)
        self.run_checks(now=4600)
        self.mail.assert_called_once()

    def test_status_email_includes_empty_and_unknown_without_persistence(self):
        self.fetchers["canvas-utrecht"].side_effect = PageChangedError("blocked")
        self.assertEqual(self.run_checks(status_email=True), 1)
        self.persist.assert_not_called()
        self.assertIn("availability unknown", self.mail.call_args.args[1])
        self.assertIn("THE FIZZ Utrecht: 0", self.mail.call_args.args[1])

    def test_dry_run_never_sends_or_persists(self):
        self.fetchers["fizz-utrecht"].return_value = self.items
        self.run_checks(dry_run=True)
        self.persist.assert_not_called()
        self.mail.assert_not_called()

    def test_simulation_covers_both_sites_without_fetching(self):
        with patch("fizz.fetch") as live_fetch:
            housing.simulate(self.sites, self.mail)
        live_fetch.assert_not_called()
        self.assertEqual(self.mail.call_count, 2)
        self.assertTrue(all(c.args[0].startswith("TEST ONLY:") for c in self.mail.call_args_list))

    def test_corrupt_state_or_extra_credentials_are_rejected(self):
        for state in ({"version": 2, "sites": {}, "SMTP_PASSWORD": "secret"},
                      {"version": 1, "sites": {}}):
            with self.assertRaises(ValueError):
                housing.validate_state(state)


class GitHubStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        for name, value in (("STATE_FILE", self.directory / "state.json"),
                            ("METADATA", self.directory / "metadata.json")):
            patcher = patch.object(github_state, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/monitor", "GITHUB_TOKEN": "fake"}):
            self.store = github_state.GitHubState()
        self.store.request = Mock()

    def test_unchanged_state_does_not_commit(self):
        state = housing.empty_state()
        save_state(github_state.STATE_FILE, state)
        save_state(github_state.METADATA, {"sha": "original-sha", "original": github_state.serialize(state)})
        self.store.save()
        self.store.request.assert_not_called()

    def test_first_run_creates_branch_then_validated_empty_file(self):
        serialized = github_state.serialize(housing.empty_state())
        self.store.request.side_effect = [
            HTTPError("url", 404, "missing", {}, None),
            {"default_branch": "main"}, {"object": {"sha": "code-sha"}}, {}, {},
            {"sha": "file-sha", "content": base64.b64encode(serialized.encode()).decode()}]
        self.store.restore()
        self.assertEqual(housing.load_state(github_state.STATE_FILE), housing.empty_state())
        self.assertEqual(json.loads(github_state.METADATA.read_text())["sha"], "file-sha")
        self.assertEqual(self.store.request.call_args_list[3].args[2],
                         {"ref": "refs/heads/monitor-state", "sha": "code-sha"})

    def test_permission_failure_does_not_start_new_history(self):
        self.store.request.side_effect = HTTPError("url", 403, "forbidden", {}, None)
        with self.assertRaises(HTTPError):
            self.store.restore()
        self.store.request.assert_called_once()
        self.assertFalse(github_state.STATE_FILE.exists())

    def test_save_uses_original_sha_to_reject_concurrent_updates(self):
        state = housing.empty_state()
        save_state(github_state.METADATA, {"sha": "original-sha", "original": github_state.serialize(state)})
        housing.entry_for(state, "canvas-utrecht")
        save_state(github_state.STATE_FILE, state)
        self.store.save()
        method, path, body = self.store.request.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(body["sha"], "original-sha")
        self.assertEqual(body["branch"], "monitor-state")

    def test_missing_file_on_existing_branch_is_not_reset(self):
        self.store.request.side_effect = [{"object": {"sha": "old"}},
                                         HTTPError("url", 404, "missing", {}, None)]
        with self.assertRaises(HTTPError):
            self.store.restore()
        self.assertEqual(self.store.request.call_count, 2)

    def test_unknown_field_cannot_be_published(self):
        save_state(github_state.METADATA, {"sha": "old", "original": ""})
        save_state(github_state.STATE_FILE, {"version": 2, "sites": {}, "password": "secret"})
        with self.assertRaises(ValueError):
            self.store.save()
        self.store.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
