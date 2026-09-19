"""Persist only validated, public alert state on the monitor-state branch.

Uses the workflow's short-lived GITHUB_TOKEN, never a personal access token.
No credentials, diagnostics, HTML, environment dumps, or email addresses are
included in the state schema. State commits happen only when state changes.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from housing import STATE_FILE, empty_state, validate_state
from monitor import create_tls_context, save_state

BRANCH = "monitor-state"
FILE = "availability.json"
METADATA = Path("data/github-state-metadata.json")


class GitHubState:
    def __init__(self):
        repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository name.")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = os.environ["GITHUB_TOKEN"]

    def request(self, method, path, body=None):
        request = Request(self.base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, method=method)
        with urlopen(request, timeout=30, context=create_tls_context()) as response:
            return json.load(response)

    def restore(self):
        # Distinguish a genuinely absent branch from a missing/deleted state
        # file on an existing branch. Only the former is a first run.
        try:
            self.request("GET", f"/git/ref/heads/{BRANCH}")
        except HTTPError as error:
            if error.code != 404:
                raise
            default = self.request("GET", "")["default_branch"]
            ref = self.request("GET", "/git/ref/heads/" + quote(default, safe=""))
            self.request("POST", "/git/refs", {"ref": f"refs/heads/{BRANCH}",
                                             "sha": ref["object"]["sha"]})
            self.request("PUT", f"/contents/{FILE}", {
                "branch": BRANCH, "message": "Initialize public housing availability state",
                "content": base64.b64encode(serialize(empty_state()).encode()).decode()})
        content = self.request("GET", f"/contents/{FILE}?ref={BRANCH}")
        state = validate_state(json.loads(base64.b64decode(content["content"])))
        save_state(STATE_FILE, state)
        save_state(METADATA, {"sha": content["sha"], "original": serialize(state)})
        print("Restored housing alert history.")

    def save(self):
        metadata = json.loads(METADATA.read_text())
        text = serialize(validate_state(json.loads(STATE_FILE.read_text())))
        if text == metadata["original"]:
            print("Alert history unchanged; no state commit needed.")
            return
        self.request("PUT", f"/contents/{FILE}", {
            "branch": BRANCH, "message": "Update housing availability state",
            "sha": metadata["sha"], "content": base64.b64encode(text.encode()).decode()})
        print("Saved housing alert history.")


def serialize(state):
    return json.dumps(validate_state(state), indent=2, sort_keys=True) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["restore", "save"])
    args = parser.parse_args()
    try:
        getattr(GitHubState(), args.operation)()
        return 0
    except Exception as error:
        # Never print request headers, response bodies, or credential values.
        print(f"GitHub state {args.operation} failed ({type(error).__name__}). "
              "Check Actions write permissions and the monitor-state branch.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
