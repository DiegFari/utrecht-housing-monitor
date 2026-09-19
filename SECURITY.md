# Public repository and private credentials

- Gmail values belong in **Settings → Secrets and variables → Actions → Secrets**:
  `SMTP_USER`, `SMTP_PASSWORD` and `EMAIL_TO`. Never enter these as ordinary
  repository variables, workflow inputs, source code, issue text or comments.
- Use a Google app password. Your ordinary Google password is not needed.
- `.env` is for local use only. `.gitignore` excludes it, but GitHub's web upload
  does not enforce `.gitignore`. Use the allowlisted ZIP or Git-aware upload.
- Logs, source code, commits and the `monitor-state` branch are public.
  The state contains only public listing information and retry metadata.
  Browser diagnostics and local environment files are never uploaded or cached.
- The monitoring workflow runs only on the default branch, on a timer or a
  manual request from someone with repository write access. Pull-request tests
  receive no email secrets. No `pull_request_target` workflow is used.
- The short-lived `GITHUB_TOKEN` is available only to state restore/save steps.
  Gmail secrets are provided only to the checking step. Actions are pinned to
  commit hashes; dependency update pull requests require review.
- A public visitor cannot read your Actions secrets. Someone who can modify
  and run your trusted workflow can write code to extract them. Keep write
  access restricted and review code and dependency changes before merging.
- If a real credential is ever published, revoke it in Google immediately;
  deleting a file does not remove the credential from Git history.

`scripts/package_public.py` compares selected public files with local Gmail
values without printing them, and checks several common private-key/token
patterns. This is a useful publication check, not a guarantee that every kind
of secret is detectable. Do not add personal data to source files.
