#!/usr/bin/env python3
"""Submit an already-uploaded MAS build for App Review (ops#2).

The release pipeline's ``build-mas`` job uploads the signed sandboxed
.pkg to App Store Connect on every release tag; this script finishes
the job via the ASC API so the whole Mac App Store update is hands-off:

    1. resolve the app by bundle id
    2. wait for the uploaded build to finish Apple-side processing
    3. get-or-create the macOS appStoreVersion for this release
       (releaseType AFTER_APPROVAL → goes live on approval, no click)
    4. set the localized "What's New" text
    5. attach the build to the version
    6. create + submit the review submission

Usage (CI)::

    python3 dev/mas_submit.py --version 0.1.9 --whats-new mas_whats_new.txt

Env: APPLE_API_KEY_ID, APPLE_API_ISSUER, and either APPLE_API_KEY_B64
or APPLE_API_KEY_PATH. ``--dry-run`` stops before the submit step (does
create the version + set notes — safe: nothing goes to review).

Apple review (~1 day) remains the only human-time step; approval then
auto-releases. Failure exits non-zero so the CI job surfaces it, but
the build is already uploaded — a manual ASC submit remains possible.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.appstoreconnect.apple.com"
BUNDLE_ID = "io.github.wolfgangwarehaus.jellytoast"
PLATFORM = "MAC_OS"

# States in which an appStoreVersion can still be edited / (re)used for a
# new submission, per the ASC API appStoreVersionState enum.
EDITABLE_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
}


def log(msg: str) -> None:
    print(f"[mas-submit] {msg}", flush=True)


# ── Auth ─────────────────────────────────────────────────────────────────


def make_token() -> str:
    """A short-lived ES256 JWT for the ASC API (20 min, the max Apple
    allows). Requires PyJWT + cryptography (installed by the CI job)."""
    import jwt  # PyJWT

    key_id = os.environ["APPLE_API_KEY_ID"]
    issuer = os.environ["APPLE_API_ISSUER"]
    key_path = os.environ.get("APPLE_API_KEY_PATH")
    if key_path:
        key = open(key_path, "rb").read()
    else:
        key = base64.b64decode(os.environ["APPLE_API_KEY_B64"])
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 19 * 60, "aud": "appstoreconnect-v1"},
        key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


class Client:
    """Minimal ASC API client — refreshes the JWT lazily so long build-
    processing waits don't outlive the 20-minute token."""

    def __init__(self) -> None:
        self._token = ""
        self._token_born = 0.0

    def _auth(self) -> str:
        if not self._token or time.time() - self._token_born > 15 * 60:
            self._token = make_token()
            self._token_born = time.time()
        return self._token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._auth()}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None

    def get(self, path: str, **params: str) -> dict:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.request("GET", path)


# ── Pure helpers (unit-tested; no network) ───────────────────────────────


def pick_editable_version(versions: list[dict], version_string: str) -> dict | None:
    """The existing appStoreVersion to reuse for this release, or None.
    Reuse any version already carrying this versionString, or any version
    still in an editable state (its versionString gets PATCHed) — creating
    a second editable version would 409."""
    for v in versions:
        if v["attributes"].get("versionString") == version_string:
            return v
    for v in versions:
        if v["attributes"].get("appStoreState") in EDITABLE_STATES or (
            v["attributes"].get("appVersionState") in EDITABLE_STATES
        ):
            return v
    return None


def version_create_body(app_id: str, version_string: str) -> dict:
    return {"data": {
        "type": "appStoreVersions",
        "attributes": {
            "platform": PLATFORM,
            "versionString": version_string,
            # Live on approval with zero clicks — the ops#2 end state.
            "releaseType": "AFTER_APPROVAL",
        },
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
    }}


def build_attach_body(build_id: str) -> dict:
    return {"data": {"type": "builds", "id": build_id}}


# ── Flow ─────────────────────────────────────────────────────────────────


def resolve_app(c: Client) -> str:
    apps = c.get("/v1/apps", **{"filter[bundleId]": BUNDLE_ID})["data"]
    if not apps:
        raise RuntimeError(f"no app with bundle id {BUNDLE_ID}")
    return apps[0]["id"]


def wait_for_build(c: Client, app_id: str, version: str, timeout_s: int = 45 * 60) -> str:
    """The id of the processed build whose CFBundleVersion == version.
    The upload just happened in the same pipeline run, so poll until
    Apple finishes processing it (typically 5–30 minutes)."""
    deadline = time.time() + timeout_s
    while True:
        builds = c.get(
            "/v1/builds",
            **{
                "filter[app]": app_id,
                "filter[version]": version,
                "sort": "-uploadedDate",
                "limit": "5",
            },
        )["data"]
        for b in builds:
            state = b["attributes"].get("processingState")
            if state == "VALID":
                log(f"build {version} processed (id {b['id']})")
                return b["id"]
            if state in ("FAILED", "INVALID"):
                raise RuntimeError(f"build {version} processing state: {state}")
        if time.time() > deadline:
            raise RuntimeError(
                f"build {version} not processed after {timeout_s // 60} min "
                "(it may still appear later — submit manually in ASC or re-run)"
            )
        log(f"build {version} still processing — waiting 60s "
            f"({int(deadline - time.time()) // 60} min left)")
        time.sleep(60)


def ensure_version(c: Client, app_id: str, version_string: str) -> str:
    versions = c.get(
        f"/v1/apps/{app_id}/appStoreVersions",
        **{"filter[platform]": PLATFORM, "limit": "10"},
    )["data"]
    existing = pick_editable_version(versions, version_string)
    if existing is None:
        log(f"creating appStoreVersion {version_string}")
        return c.request("POST", "/v1/appStoreVersions",
                         version_create_body(app_id, version_string))["data"]["id"]
    vid = existing["id"]
    if existing["attributes"].get("versionString") != version_string:
        log(f"reusing editable version {existing['attributes'].get('versionString')} "
            f"-> renaming to {version_string}")
        c.request("PATCH", f"/v1/appStoreVersions/{vid}", {"data": {
            "type": "appStoreVersions", "id": vid,
            "attributes": {"versionString": version_string,
                           "releaseType": "AFTER_APPROVAL"},
        }})
    else:
        log(f"reusing appStoreVersion {version_string} (id {vid})")
    return vid


def set_whats_new(c: Client, version_id: str, text: str) -> None:
    locs = c.get(f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations")["data"]
    for loc in locs:
        c.request("PATCH", f"/v1/appStoreVersionLocalizations/{loc['id']}", {"data": {
            "type": "appStoreVersionLocalizations", "id": loc["id"],
            "attributes": {"whatsNew": text},
        }})
        log(f"whatsNew set for {loc['attributes'].get('locale')}")


def submit_for_review(c: Client, app_id: str, version_id: str) -> None:
    """Create (or reuse) the review submission, add the version, submit."""
    subs = c.get(
        "/v1/reviewSubmissions",
        **{"filter[app]": app_id, "filter[platform]": PLATFORM,
           "filter[state]": "READY_FOR_REVIEW", "limit": "1"},
    )["data"]
    if subs:
        sub_id = subs[0]["id"]
        log(f"reusing open review submission {sub_id}")
    else:
        sub_id = c.request("POST", "/v1/reviewSubmissions", {"data": {
            "type": "reviewSubmissions",
            "attributes": {"platform": PLATFORM},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }})["data"]["id"]
        log(f"created review submission {sub_id}")
    c.request("POST", "/v1/reviewSubmissionItems", {"data": {
        "type": "reviewSubmissionItems",
        "relationships": {
            "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub_id}},
            "appStoreVersionForReview": {"data": {"type": "appStoreVersions", "id": version_id}},
        },
    }})
    c.request("PATCH", f"/v1/reviewSubmissions/{sub_id}", {"data": {
        "type": "reviewSubmissions", "id": sub_id,
        "attributes": {"submitted": True},
    }})
    log("submitted for review — releaseType AFTER_APPROVAL releases it on approval")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="X.Y.Z (== CFBundleVersion)")
    ap.add_argument("--whats-new", type=argparse.FileType("r", encoding="utf-8"),
                    help="file with the localized What's New text")
    ap.add_argument("--dry-run", action="store_true",
                    help="prepare the version but stop before submitting")
    args = ap.parse_args()

    c = Client()
    app_id = resolve_app(c)
    log(f"app id {app_id}")
    build_id = wait_for_build(c, app_id, args.version)
    version_id = ensure_version(c, app_id, args.version)
    if args.whats_new:
        text = args.whats_new.read().strip()
        if text:
            set_whats_new(c, version_id, text)
    c.request("PATCH", f"/v1/appStoreVersions/{version_id}/relationships/build",
              build_attach_body(build_id))
    log(f"build attached to version {args.version}")
    if args.dry_run:
        log("dry run — NOT submitting for review")
        return 0
    submit_for_review(c, app_id, version_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
