"""Identify by sound: fpcalc output, AcoustID matching, and how it plugs into
Fix metadata. No test touches the network or runs a real fpcalc."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from musicstudio.config import get_settings
from musicstudio.core import fingerprint, tag_fix
from musicstudio.core import tags as T

from .test_tag_fix import FakeClient, FakeResponse


class _NoWait:
    def wait(self):
        pass


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(fingerprint, "_limiter", _NoWait())


def _result(score, title="Cheques", artists=(("Shubh", ""),), groups=()):
    return {
        "score": score,
        "recordings": [{
            "title": title,
            "artists": [{"name": n, "joinphrase": j} for n, j in artists],
            "releasegroups": list(groups),
        }],
    }


# -- fpcalc ---------------------------------------------------------------------

def test_parse_fpcalc_output():
    assert fingerprint.parse_fpcalc_output(
        json.dumps({"duration": 215.3, "fingerprint": "AQAD"})) == (215.3, "AQAD")
    for bad in ("", "not json", json.dumps({"duration": 1}), json.dumps({"duration": 1, "fingerprint": ""})):
        with pytest.raises(fingerprint.FingerprintError):
            fingerprint.parse_fpcalc_output(bad)


# -- matching -------------------------------------------------------------------

def test_best_match_takes_the_most_confident_named_result():
    response = {"status": "ok", "results": [
        _result(0.90, "Lower"),
        _result(0.97, "Cheques", groups=[{"title": "Still Rollin", "type": "Album"}]),
        _result(0.50, "Too unsure"),
    ]}
    match = fingerprint.best_match(response)
    assert (match.title, match.artist, match.album, match.score) == (
        "Cheques", "Shubh", "Still Rollin", 0.97)


def test_best_match_ignores_weak_and_unnamed_results():
    assert fingerprint.best_match({"results": [_result(0.8)]}) is None
    assert fingerprint.best_match({"results": []}) is None
    assert fingerprint.best_match({}) is None
    nameless = _result(0.99, title="")
    assert fingerprint.best_match({"results": [nameless]}) is None
    no_artist = _result(0.99, artists=())
    assert fingerprint.best_match({"results": [no_artist]}) is None


def test_artist_credit_joins_collaborators_and_album_prefers_studio_albums():
    result = _result(
        0.95, artists=(("A", " feat. "), ("B", "")),
        groups=[
            {"title": "Greatest Hits", "type": "Album", "secondarytypes": ["Compilation"]},
            {"title": "The Real Album", "type": "Album"},
        ],
    )
    match = fingerprint.best_match({"results": [result]})
    assert match.artist == "A feat. B"
    assert match.album == "The Real Album"
    # With only a compilation to go on, that is still better than nothing.
    only = _result(0.95, groups=[{"title": "Now 99", "type": "Album", "secondarytypes": ["Compilation"]}])
    assert fingerprint.best_match({"results": [only]}).album == "Now 99"


def _install(monkeypatch, response):
    client = FakeClient({"acoustid": response})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return client


def test_lookup_sends_the_fingerprint_and_returns_the_match(monkeypatch):
    client = _install(monkeypatch, FakeResponse(json_data={
        "status": "ok", "results": [_result(0.96)]}))
    match = fingerprint.lookup(215.4, "AQAD", "KEY123")
    assert match.title == "Cheques"
    _, params = client.calls[0]
    assert params["client"] == "KEY123" and params["duration"] == 215
    assert params["fingerprint"] == "AQAD"


def test_lookup_errors_are_errors_but_no_match_is_not(monkeypatch):
    _install(monkeypatch, FakeResponse(json_data={"status": "ok", "results": []}))
    assert fingerprint.lookup(100, "x", "k") is None

    _install(monkeypatch, FakeResponse(json_data={
        "status": "error", "error": {"message": "invalid API key"}}))
    with pytest.raises(fingerprint.FingerprintError, match="invalid API key"):
        fingerprint.lookup(100, "x", "k")

    _install(monkeypatch, FakeResponse(status_code=503))
    with pytest.raises(fingerprint.FingerprintError):
        fingerprint.lookup(100, "x", "k")

    def down(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", down)
    with pytest.raises(fingerprint.FingerprintError):
        fingerprint.lookup(100, "x", "k")


# -- identify() -----------------------------------------------------------------

def test_identify_needs_a_key_and_fpcalc(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(fingerprint, "fingerprint_file",
                        lambda p: called.append(p) or (100.0, "fp"))
    monkeypatch.setattr(fingerprint, "lookup", lambda d, f, k: fingerprint.Match(0.9, "T", "A", "L"))

    monkeypatch.setattr(fingerprint, "find_fpcalc", lambda: Path("fpcalc"))
    assert fingerprint.identify(tmp_path / "x.flac") is None  # no key yet
    assert not fingerprint.is_configured()

    get_settings().acoustid_api_key = "KEY"
    assert fingerprint.is_configured()
    tags = fingerprint.identify(tmp_path / "x.flac")
    assert (tags.title, tags.artist, tags.album) == ("T", "A", "L")

    monkeypatch.setattr(fingerprint, "find_fpcalc", lambda: None)
    called.clear()
    assert fingerprint.identify(tmp_path / "x.flac") is None  # no fpcalc
    assert called == []


def test_identify_swallows_failures(monkeypatch, tmp_path):
    get_settings().acoustid_api_key = "KEY"
    monkeypatch.setattr(fingerprint, "find_fpcalc", lambda: Path("fpcalc"))

    def broken(path):
        raise fingerprint.FingerprintError("boom")

    monkeypatch.setattr(fingerprint, "fingerprint_file", broken)
    assert fingerprint.identify(tmp_path / "x.flac") is None

    monkeypatch.setattr(fingerprint, "fingerprint_file", lambda p: (100.0, "fp"))
    monkeypatch.setattr(fingerprint, "lookup", lambda *a: (_ for _ in ()).throw(
        fingerprint.FingerprintError("offline")))
    assert fingerprint.identify(tmp_path / "x.flac") is None


# -- Fix metadata ----------------------------------------------------------------

@pytest.fixture
def no_online(monkeypatch):
    monkeypatch.setattr(tag_fix, "guess_from_online", lambda tags, settings=None: T.TagSet())


def _heard(monkeypatch, tags):
    calls = []
    monkeypatch.setattr(fingerprint, "identify", lambda path: calls.append(path) or tags)
    return calls


def test_fingerprint_fills_a_file_the_filename_cannot_name(monkeypatch, tmp_path, no_online):
    calls = _heard(monkeypatch, T.TagSet(title="Cheques", artist="Shubh", album="Still Rollin"))
    path = tmp_path / "track01.flac"
    fixed = tag_fix.fix_file_tags(path, baseline=T.TagSet(), use_fingerprint=True)
    # The sound wins over the junk the filename would have given as the title.
    assert (fixed.title, fixed.artist, fixed.album) == ("Cheques", "Shubh", "Still Rollin")
    assert calls == [path]


def test_fingerprint_is_off_by_default_and_never_overwrites(monkeypatch, tmp_path, no_online):
    calls = _heard(monkeypatch, T.TagSet(title="Wrong", artist="Wrong", album="Wrong"))
    path = tmp_path / "track01.flac"
    tag_fix.fix_file_tags(path, baseline=T.TagSet())
    assert calls == []  # default off

    tagged = T.TagSet(title="Mine", artist="Me")
    fixed = tag_fix.fix_file_tags(path, baseline=tagged, use_fingerprint=True)
    assert calls == []  # already has artist and title: no lookup at all
    assert (fixed.title, fixed.artist) == ("Mine", "Me")

    half = T.TagSet(title="Mine")
    fixed = tag_fix.fix_file_tags(path, baseline=half, use_fingerprint=True)
    assert (fixed.title, fixed.artist) == ("Mine", "Wrong")  # fills the blank only


def test_a_miss_falls_back_to_the_filename(monkeypatch, tmp_path, no_online):
    _heard(monkeypatch, None)
    fixed = tag_fix.fix_file_tags(
        tmp_path / "Shubh - Cheques.flac", baseline=T.TagSet(), use_fingerprint=True)
    assert (fixed.artist, fixed.title) == ("Shubh", "Cheques")


def test_library_batch_reports_what_was_identified(monkeypatch, tmp_path, tone_flac, no_online):
    named = tmp_path / "track01.flac"
    unknown = tmp_path / "track02.flac"
    for path in (named, unknown):
        path.write_bytes(tone_flac.read_bytes())
    monkeypatch.setattr(
        fingerprint, "identify",
        lambda path: T.TagSet(title="Cheques", artist="Shubh") if path == named else None)

    results = tag_fix.fix_library_tags([named, unknown], use_fingerprint=True)
    by_name = {r.path.name: r for r in results}
    assert by_name["track01.flac"].identified and by_name["track01.flac"].updated
    assert not by_name["track02.flac"].identified
    assert T.read(named).artist == "Shubh"

    # The preference is the default when the caller does not say.
    get_settings().identify_by_sound = False
    again = tag_fix.fix_library_tags([unknown])
    assert not any(r.identified for r in again)
