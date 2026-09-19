# Utrecht housing monitor

Checks **Canvas Utrecht** and **THE FIZZ Utrecht**, then emails **there is a studio
available** when a new studio category becomes bookable. GitHub Actions runs one
check of each enabled site on a five-minute schedule while your laptop is off.
No paid API or housing account is required.

**Prepared for deployment; GitHub execution has not been verified yet.** Gmail
delivery and a visible Chrome check of Canvas worked locally. THE FIZZ's live
public feed returned no offers during development. Positive listing fixtures
are synthetic. Complete the cloud tests below before relying on alerts.
The final local dry run successfully checked both sites and reported zero
bookable categories; this does not establish access from GitHub's runners.

## 1. Create the public repository

Use your own GitHub account. The code and logs will be public; Gmail values will
be stored separately as Actions secrets.

The prepared upload package is `dist/utrecht-housing-monitor.zip`. It contains
only the selected source files, including the hidden `.github` folder. It does
**not** contain `.env`, your virtual environment, browser diagnostics, local
alert history, or old hosting instructions.

To regenerate the package from the development folder:

```sh
.venv/bin/python scripts/package_public.py
```

Extract the ZIP, then create an empty **public** repository named
`utrecht-housing-monitor` at <https://github.com/new>. Do not initialize the
remote with a README, `.gitignore`, or license; these files are already prepared.

Open a terminal in the **extracted `utrecht-housing-monitor` folder**, replace
`YOUR_USERNAME` below with your GitHub username, and run:

```sh
git init -b main
git add .
git diff --cached --name-only
git commit -m "Add Canvas and THE FIZZ housing monitors"
git remote add origin https://github.com/YOUR_USERNAME/utrecht-housing-monitor.git
git push -u origin main
```

Use your usual GitHub authentication. GitHub Desktop can also publish the
extracted folder; make sure the repository is public and `.github/workflows`
appears on GitHub afterwards. A local Git commit needs your chosen Git name and
email; use your GitHub-provided `noreply` email if you want to keep your personal
email out of public commits.

Do not upload the entire original development folder in GitHub's browser UI:
web upload does not enforce `.gitignore`. Do not upload the ZIP itself as the
repository's only file—GitHub needs the extracted workflow files to run them.

## 2. Add three GitHub Secrets

In the repository, open **Settings → Secrets and variables → Actions → Secrets
→ New repository secret**. Add these exact names:

| Secret | Value |
| --- | --- |
| `SMTP_USER` | The Gmail address that sends alerts |
| `SMTP_PASSWORD` | Your Google app password, not your normal password |
| `EMAIL_TO` | The email address receiving alerts; it can be the same Gmail address |

Use the working values from your local `.env`; do not upload that file or paste
values into a commit, chat, or workflow input. Spaces in Gmail app passwords are
handled automatically. To generate a new app password, enable Google 2-Step
Verification and use <https://myaccount.google.com/apppasswords>.

These must be **Secrets**, not ordinary repository Variables. Website names and
availability are public. The Gmail address, recipient, and password are not
written to saved alert state or normal logs. See [SECURITY.md](SECURITY.md).

If your account or organization restricts workflow permissions, ensure this
workflow can request **Contents: write**. It needs this to store alert history
on its separate `monitor-state` branch. It does not need permission to approve
pull requests, a personal access token, or any additional service account.

## 3. Test from GitHub before enabling the schedule

Open **Actions → Housing monitor → Run workflow**. Keep the branch set to
**main**. Run these modes one at a time:

| Mode | Expected result |
| --- | --- |
| `test-email` | One “Housing monitor: test email”; no website requests |
| `simulate-alert` | Two clearly labeled **TEST ONLY** emails, one per site; no live requests or saved history changes |
| `status-email` | One report covering both live sites, even when nothing is available |
| `dry-run` | Live checks in the Actions log, without email or saved history changes |
| `check` | A real check that saves alert history and sends mail for newly available studios |

The simulation uses made-up offers. It tests the notification and parsing paths
without waiting for rare real vacancies. It does not prove that future website
markup will match the fixtures.

A status report saying **availability unknown** means the corresponding check
failed, not that the building is fully booked. The workflow will be red if a
site fails, while still checking the other site. The public log shows the site
and error type. Diagnostics remain on the temporary runner and are not uploaded.
Missing Gmail secrets or an SMTP failure can prevent a report from being sent.

Canvas may show verification from GitHub's IP addresses even when it works on
your laptop. The workflow runs ordinary Chromium through Xvfb, a virtual display.
It does not solve CAPTCHAs or bypass verification. If repeated cloud tests fail,
that site's unattended monitoring is not working. You can disable Canvas in
`sites.json` and continue with THE FIZZ while investigating.

## 4. Enable and stop scheduled checks

After the tests, run `check` once to initialize the alert history. Then open
**Settings → Secrets and variables → Actions → Variables → New repository
variable**, and add:

```text
Name:  MONITOR_ENABLED
Value: true
```

The timer targets minutes 02, 07, 12, 17, and so on throughout the day. No process
needs to run on your laptop. Each Actions job exits after checking both sites;
it is not an infinite loop on a permanent server.

Set `MONITOR_ENABLED` to `false` to stop scheduled checks. Manual test runs remain
available. You can also use **Actions → Housing monitor → … → Disable workflow**.

GitHub's [standard hosted runners are free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
This workflow uses standard `ubuntu-24.04` runners, a small dependency/browser
cache, and no uploaded diagnostic artifacts. Free pricing is not a guarantee
that GitHub permits or will continue to provide any particular workload; its
[Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
still apply.

The [scheduler can delay or drop runs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
so five-minute polling is a target, not a guarantee. Public-repository schedules
can be disabled after 60 days without repository activity. Check Actions
periodically and manually re-enable the workflow if needed. There is no external
watchdog if GitHub stops running jobs. State commits are made only when history
or retry metadata changes; no artificial keep-alive commits are made.

## Sites and alert behavior

Edit `sites.json` to enable or disable individual sites. Each enabled website is
checked on every normal run. One failure does not stop the other site's checks.

| Site | Availability source | What triggers an alert |
| --- | --- | --- |
| [Canvas Utrecht](https://canvas-student.securerc.co.uk/onlineleasing/canvas-utrecht/floorplans.aspx) | Browser-rendered floorplan cards | Enabled availability button in a named studio card; contact-only and one-bedroom cards are excluded |
| [THE FIZZ Utrecht](https://www.the-fizz.com/en/student-accommodation/utrecht/#apartment) | Public JSON feed used by its embedded booking widget | Utrecht studio category receives a `marketingRent` offer, the condition its widget uses to display “Book” |

THE FIZZ includes Single and Double studios. It uses the free, public
`marketingCollections` lookup with `buildingCode=FIZZ_UTRECHT`; no API key or paid
API subscription is involved. The availability condition follows the site's
[public widget code](https://booking.the-fizz.com/widgets/pex-results-in-building.js).
The page's photos, generic “Book now” navigation,
static price tables, and newsletter banner do not trigger alerts. Move-in dates
and eligibility must be checked in the booking journey.

- First detection, a new category, or a category disappearing and returning
  triggers an email. Canvas also alerts when its displayed available count rises.
- Unchanged availability does not generate an email every five minutes.
- These sources expose categories, not a complete individual-unit inventory.
  Another room appearing in a continuously available category without a changed
  count cannot be detected by this monitor.
- A failed check preserves prior listings. A failed email is retried on a later
  check. Checks back off to 5, 10, then 15 minutes after errors.
- After five consecutive failed attempts, the monitor tries to send a separate
  health email, limited to once per day until the site recovers. This also needs
  working email and running workflows.
- Durable history lives in `availability.json` on the public `monitor-state`
  branch. It contains public listing names/links and numeric retry metadata only.
  Nothing is committed on normal successful checks with unchanged results.
- The workflow saves history even if one site fails. GitHub/SMTP interruptions
  after an email is accepted but before the history is saved can repeat an alert.
  A corrupted or missing history file on an existing state branch stops the run
  instead of silently resetting deduplication. Do not delete that branch casually.

To support an additional website, add a provider adapter that returns
`monitor.Listing` objects and register it in `housing.py`, with fixtures/tests.
Adding an arbitrary URL alone is insufficient: every provider exposes different
availability signals.

## Local use and development

Python 3.10+ on macOS/Linux; the GitHub workflow uses Python 3.13.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
chmod 600 .env
```

Fill in `.env` locally, then:

```sh
.venv/bin/python housing.py --test-email
.venv/bin/python housing.py --simulate-alert
.venv/bin/python housing.py --status-email --headed
.venv/bin/python housing.py --once --dry-run --headed
.venv/bin/python housing.py --once --dry-run --site fizz-utrecht
```

On a Mac with Google Chrome installed, `BROWSER_CHANNEL=chrome` in `.env` avoids
downloading another browser. `housing.py` always exits after one pass; GitHub
provides the schedule. Local history is `data/housing-state.json`, separate from
the legacy single-site `monitor.py` history. The older `monitor.py` commands
remain available, but GitHub runs the new multi-site `housing.py`.

Run offline tests and audit the publishable files:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/package_public.py --check
```

Optional browser regression tests use fixtures and do not contact housing sites:

```sh
RUN_BROWSER_TESTS=1 BROWSER_CHANNEL=chrome .venv/bin/python -m unittest discover -s tests -p test_browser_readiness.py -v
```

Tests cover false positives, empty/changed pages, duplicate prevention, error
isolation across sites, failed email retries, persistent backoff, test modes,
safe state persistence, TLS, and credential exclusion. Live positive offers
still need validation against the providers' current booking pages.
