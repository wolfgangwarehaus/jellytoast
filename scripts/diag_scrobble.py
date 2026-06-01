"""One-off diagnostic: why is the double-scrobble guard not firing?

Prints the persisted scrobble flags, then runs the Navidrome native-API
detection against the live server and dumps WHICH fields the
``/api/user/{id}`` record exposes (redacting secret values) — so we can see
whether Navidrome surfaces the per-user ListenBrainz/Last.fm linkage at all,
and under what field name, vs. the names the detector currently looks for.

Read-only: one native-API login + one user-record GET. Run:
    python scripts/diag_scrobble.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from modules.scrobble import server_scrobble_detect as ssd  # noqa: E402
from modules.settings import get_settings  # noqa: E402


def _redact(v):
    if isinstance(v, str) and v:
        return f"<non-empty str len={len(v)}>"
    return v


def main() -> int:
    s = get_settings()
    print("=== persisted scrobble flags ===")
    for key in (
        "server_is_navidrome",
        "server_scrobble_check_done",
        "server_scrobbles_listenbrainz",
        "server_scrobbles_lastfm",
        "listenbrainz_enabled",
        "listenbrainz_username",
        "lastfm_enabled",
    ):
        print(f"  {key:32} = {getattr(s, key, '<missing>')!r}")

    username = s.username or s.user_id
    password = s.access_token  # plaintext (dual-store getter)
    server = s.server_url
    print("\n=== creds available for the native probe ===")
    print(f"  server   = {server!r}")
    print(f"  username = {username!r}")
    print(f"  password = {'<set>' if password else '<EMPTY>'} (len={len(password or '')})")
    if not (server and username and password):
        print("  → missing creds; the login-path detect couldn't run either.")
        return 1

    print("\n=== detector Result (modules.scrobble.server_scrobble_detect.detect) ===")
    res = ssd.detect(s.listenbrainz_url, s.listenbrainz_username, s.listenbrainz_token)
    print(f"  checked            = {res.checked}")
    print(f"  foreign_clients    = {res.foreign_clients}")
    print(f"  server_scrobbles   = {res.server_scrobbles}")

    # Raw native-API probe — kept to show Navidrome exposes NO scrobble field
    # (the reason the old native-API detector could never work).
    print("\n=== raw /api/user/{id} field names (values redacted) ===")
    base = server.rstrip("/")
    try:
        r = requests.post(
            f"{base}/auth/login",
            json={"username": username, "password": password},
            headers={"User-Agent": "jellytoast-diag/0", "Content-Type": "application/json"},
            timeout=6.0,
        )
        print(f"  /auth/login HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:200]!r}")
            return 0
        body = r.json()
        jwt = body.get("token") or body.get("jwt") or ""
        uid = body.get("id") or body.get("userId") or body.get("user_id") or ""
        print(f"  login keys: {sorted(body.keys())}")
        print(f"  jwt present: {bool(jwt)}   user_id: {uid!r}")
        if not (jwt and uid):
            return 0
        # Navidrome's native API rejects the standard Authorization header;
        # its web UI sends the JWT in X-ND-Authorization (+ a client id).
        r2 = requests.get(
            f"{base}/api/user/{uid}",
            headers={
                "User-Agent": "jellytoast-diag/0",
                "x-nd-authorization": f"Bearer {jwt}",
                "x-nd-client-unique-id": "jellytoast-diag",
            },
            timeout=6.0,
        )
        print(f"  /api/user/{{id}} HTTP {r2.status_code}  (using x-nd-authorization)")
        if r2.status_code != 200:
            print(f"  body: {r2.text[:200]!r}")
            return 0
        user = r2.json()
        if not isinstance(user, dict):
            print(f"  unexpected user-record type: {type(user)}")
            return 0
        print("  user-record fields (name = redacted value):")
        for k in sorted(user.keys()):
            lname = k.lower()
            if any(t in lname for t in ("brainz", "lastfm", "scrobble")):
                print(f"    *** {k} = {_redact(user[k])!r}   <-- scrobble-related")
            else:
                print(f"        {k} = {_redact(user[k])!r}")
    except requests.RequestException as e:
        print(f"  native probe raised: {e!r}")

    # ── The viable detection: who submits this user's LB listens? ──────────
    # Every listen records its submitting client in additional_info.
    # submission_client. If a client OTHER than "jellytoast" appears, a second
    # scrobbler (the server, or another app) is active on this LB account.
    print("\n=== recent ListenBrainz listens — submission_client per listen ===")
    lb_user = s.listenbrainz_username
    lb_token = s.listenbrainz_token
    lb_base = (s.listenbrainz_url or "https://api.listenbrainz.org").rstrip("/")
    if not lb_user:
        print("  no listenbrainz_username configured.")
        return 0
    hdrs = {"User-Agent": "jellytoast-diag/0"}
    if lb_token:
        hdrs["Authorization"] = f"Token {lb_token}"
    try:
        lr = requests.get(
            f"{lb_base}/1/user/{lb_user}/listens",
            params={"count": 25},
            headers=hdrs,
            timeout=8.0,
        )
        print(f"  HTTP {lr.status_code}")
        if lr.status_code != 200:
            print(f"  body: {lr.text[:200]!r}")
            return 0
        listens = (lr.json().get("payload") or {}).get("listens") or []
        clients = {}
        for ln in listens:
            md = ln.get("track_metadata") or {}
            ai = md.get("additional_info") or {}
            client = ai.get("submission_client") or ai.get("music_service") or "<none>"
            clients[client] = clients.get(client, 0) + 1
            print(
                f"  {ln.get('listened_at')}  client={client!r:22} "
                f"{(md.get('artist_name') or '')[:18]:18} – {(md.get('track_name') or '')[:28]}"
            )
        print(f"\n  distinct submitters in last {len(listens)}: {clients}")
        others = [c for c in clients if c not in ('jellytoast', '<none>')]
        print(f"  → non-jellytoast submitters present: {others or 'NONE'}")
    except requests.RequestException as e:
        print(f"  LB listens query raised: {e!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
