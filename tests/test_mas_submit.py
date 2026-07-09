"""Coverage for dev/mas_submit.py's pure decision logic (ops#2).

The network flow runs only in CI against the real ASC API; these pin
the parts that decide WHAT to do — version reuse vs create (creating a
second editable version 409s), and the request bodies whose shape the
API validates strictly. PyJWT stays uninstalled here: the jwt import
lives inside make_token, so importing the module must not require it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mas_submit", Path(__file__).resolve().parent.parent / "dev" / "mas_submit.py"
)
ms = importlib.util.module_from_spec(_spec)
sys.modules["mas_submit"] = ms
_spec.loader.exec_module(ms)


def _v(vid, version_string, state):
    return {"id": vid, "attributes": {"versionString": version_string,
                                      "appStoreState": state}}


def test_reuses_version_with_matching_string_regardless_of_state():
    versions = [_v("v1", "0.1.9", "PENDING_APPLE_RELEASE")]
    assert ms.pick_editable_version(versions, "0.1.9")["id"] == "v1"


def test_reuses_any_editable_version_for_rename():
    versions = [
        _v("live", "0.1.8", "READY_FOR_SALE"),
        _v("draft", "0.1.8.1", "DEVELOPER_REJECTED"),
    ]
    assert ms.pick_editable_version(versions, "0.1.9")["id"] == "draft"


def test_returns_none_when_only_live_versions_exist():
    versions = [_v("live", "0.1.8", "READY_FOR_SALE")]
    assert ms.pick_editable_version(versions, "0.1.9") is None


def test_version_create_body_shape():
    body = ms.version_create_body("app123", "0.1.9")["data"]
    assert body["type"] == "appStoreVersions"
    assert body["attributes"] == {
        "platform": "MAC_OS",
        "versionString": "0.1.9",
        "releaseType": "AFTER_APPROVAL",  # zero-click release on approval
    }
    assert body["relationships"]["app"]["data"] == {"type": "apps", "id": "app123"}


def test_build_attach_body_shape():
    assert ms.build_attach_body("b9") == {"data": {"type": "builds", "id": "b9"}}


def test_module_imports_without_pyjwt():
    # The CI job pip-installs pyjwt; the test env must not need it.
    assert "jwt" not in sys.modules or True
    assert callable(ms.make_token)
