"""Tests for the browser policy writes.

The registry glue is thin, but WHAT it writes is load-bearing: the Shorts URL block,
and a uBO deployment where forcing the install without the list (or the list without
the install) silently does nothing. These pin the written values against a captured
fake of `run`.
"""

from __future__ import annotations

import json

from safehouse_setup import browser


def capture(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        browser, "run",
        lambda args, **kw: (calls.append([str(a) for a in args]), (0, ""))[1])
    return calls


def writes(calls, needle):
    return [c for c in calls if any(needle in part for part in c)]


def test_apply_writes_the_shorts_urlblocklist_to_every_key(monkeypatch):
    calls = capture(monkeypatch)
    browser.apply()
    hits = writes(calls, "URLBlocklist")
    assert len(hits) == len(browser.OPERA_KEYS) + len(browser.CHROMIUM_KEYS)
    for c in hits:
        assert "youtube.com/shorts" in c


def test_apply_forces_ubo_with_the_store_matching_the_browser(monkeypatch):
    calls = capture(monkeypatch)
    browser.apply()
    hits = writes(calls, "ExtensionInstallForcelist")
    assert len(hits) == len(browser.OPERA_KEYS) + len(browser.CHROMIUM_KEYS)
    for c in hits:
        joined = " ".join(c)
        if "Microsoft\\Edge" in joined:
            # Edge installs from its own store by bare id - no update URL
            assert browser.UBO_EDGE_ID in c
            assert browser.UBO_CWS_ID not in joined
        else:
            assert browser.UBO_CWS_UPDATE in c


def test_apply_subscribes_the_shorts_list_without_dropping_the_defaults(monkeypatch):
    calls = capture(monkeypatch)
    browser.apply()
    hits = writes(calls, "toOverwrite")
    assert len(hits) == len(browser.OPERA_KEYS) + len(browser.CHROMIUM_KEYS)
    for c in hits:
        payload = json.loads(c[c.index("/d") + 1])
        lists = payload["filterLists"]
        assert browser.SHORTS_LIST_URL in lists
        # "overwrite" means overwrite: forgetting the defaults would strip the ad
        # blocking to a Shorts-only blocker
        assert "ublock-filters" in lists and "easylist" in lists
        # and the managed-storage key must sit under the id the same browser installs
        joined = " ".join(c)
        expected = browser.UBO_EDGE_ID if "Microsoft\\Edge" in joined else browser.UBO_CWS_ID
        assert rf"3rdparty\extensions\{expected}\policy" in joined
