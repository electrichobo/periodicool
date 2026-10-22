"""Grab, monitor, import and failure handling, end to end against fakes. No network."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import db, downloads, library, search
from periodicool.cli import magazine_from_dict
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.sabnzbd import SabnzbdClient
from periodicool.config import load_settings
from periodicool.models import (
    BlockedRelease,
    FileMove,
    FormatProfile,
    Grab,
    GrabStatus,
    ImportItem,
    Issue,
    IssueStatus,
    ItemAction,
    Release,
)
from periodicool.prowlarr import ProwlarrClient, ProwlarrError

from fake_prowlarr import KEY, URL, FakeProwlarr
from fake_sab import SAB_KEY, SAB_URL, FakeSab, make_nzb

EXAMPLES = Path(__file__).parent.parent / "examples" / "magazines.json"
TODAY = date(2026, 10, 16)
L311 = "Linux Magazine USA - Issue 311, October 2026"
L311_ALT = "Linux.Magazine.No.311.2026.HYBRID.MAGAZINE"
TARGET = "Linux Magazine/2026/Linux Magazine - 2026-10.pdf"


@pytest.fixture
def env(tmp_path):
    settings = load_settings(
        {
            "PERIODICOOL_CONFIG_DIR": str(tmp_path / "config"),
            "PERIODICOOL_LIBRARY_DIR": str(tmp_path / "library"),
            "PERIODICOOL_DOWNLOADS_DIR": str(tmp_path / "downloads"),
        }
    )
    for d in (settings.config_dir, settings.library_dir, settings.downloads_dir):
        d.mkdir(parents=True)
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    session = db.make_session_factory(engine)()
    profile = session.scalars(select(FormatProfile)).one()
    for entry in json.loads(EXAMPLES.read_text(encoding="utf-8")):
        mag = magazine_from_dict(entry)
        mag.format_profile = profile
        session.add(mag)
    library.set_setting(session, search.URL_KEY, URL)
    library.set_setting(session, downloads.SAB_URL_KEY, SAB_URL)
    library.set_setting(session, downloads.CATEGORY_KEY, "mags")
    complete = settings.downloads_dir / "complete" / "mags"
    complete.mkdir(parents=True)
    library.set_setting(session, downloads.PATH_MAP_KEY, [["/data/complete/mags", str(complete)]])
    session.flush()

    fake_p, fake_s = FakeProwlarr(), FakeSab()
    prowlarr = search.make_client(session, transport=fake_p, api_key=KEY)
    prowlarr.min_interval = 0
    sab = downloads.make_download_client(session, transport=fake_s, api_key=SAB_KEY)
    sab.min_interval = 0

    class Env:
        pass

    e = Env()
    e.settings, e.session, e.prowlarr, e.sab = settings, session, prowlarr, sab
    e.fake_p, e.fake_s, e.complete = fake_p, fake_s, complete
    yield e
    session.close()
    engine.dispose()


def seen(e, *titles):
    """Releases arrive by RSS from indexer 1."""
    e.fake_p.rss_titles[1] = list(titles)
    search.rss_sync(e.session, e.prowlarr, TODAY)
    return {r.title: r for r in e.session.scalars(select(Release))}


def finish(e, grab, files: dict, folder="Linux Magazine 311", status="Completed", msg=""):
    job_dir = e.complete / folder
    job_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (job_dir / name).write_bytes(data)
    e.fake_s.complete(grab.job_id, f"/data/complete/mags/{folder}", status, msg)
    return job_dir


def issue(e, key="n311") -> Issue:
    return e.session.scalars(select(Issue).where(Issue.key == key)).one()


# SABnzbd client


def test_sab_client_checks_category():
    fake = FakeSab(categories=("movies",))
    client = SabnzbdClient(SAB_URL, SAB_KEY, transport=fake, min_interval=0)
    with pytest.raises(ClientError, match="does not exist"):
        client.check("mags")
    with pytest.raises(ClientError, match="no SABnzbd category"):
        client.check("")


def test_sab_client_only_returns_our_jobs():
    fake = FakeSab()
    client = SabnzbdClient(SAB_URL, SAB_KEY, transport=fake, min_interval=0)
    job = client.add_nzb(make_nzb("a.pdf"), "Linux 311", "mags")
    cat, name, body = fake.added[0]
    assert (cat, name) == ("mags", "Linux 311") and b'filename="Linux 311.nzb"' in body
    [state] = client.jobs([job], "mags")
    assert state.job_id == job and state.status is JobStatus.QUEUED
    query = fake.calls[-1]["query"]
    assert query["nzo_ids"] == job and query["cat"] == "mags"
    # Foreign jobs come back from the fake, but never out of the client.
    assert client.jobs(["SABnzbd_nzo_movie", "SABnzbd_nzo_tv"], "mags") == []


def test_sab_errors_are_redacted():
    fake = FakeSab()
    client = SabnzbdClient(SAB_URL, "wrong-key", transport=fake, min_interval=0)
    with pytest.raises(ClientError) as exc:
        client.version()
    assert "wrong-key" not in str(exc.value)


# NZB checks and fetching


def test_check_nzb():
    assert downloads.check_nzb(make_nzb("Linux.311.pdf", "Linux.311.par2")) is None
    assert downloads.check_nzb(make_nzb("x.part01.rar", "x.part02.rar")) is None
    assert downloads.check_nzb(make_nzb("4f8a9c2e1b")) is None  # obfuscated: unknown
    assert "no pdf/epub/cbz/cbr" in downloads.check_nzb(make_nzb("cover.jpg", "info.nfo"))
    assert downloads.check_nzb(b"not xml") == "not a valid NZB"


def test_fetch_nzb_only_from_prowlarr():
    fake = FakeProwlarr()
    client = ProwlarrClient(URL, KEY, transport=fake, min_interval=0)
    client.fetch_nzb(f"{URL}/1/download?link=abc")
    assert fake.calls[-1]["query"]["apikey"] == [KEY]
    with pytest.raises(ProwlarrError, match="not on the configured Prowlarr"):
        client.fetch_nzb("http://evil.test/1/download?link=abc")
    assert len(fake.calls) == 1


# Grab, monitor, import


def test_grab_monitor_import_and_cleanup(env):
    e = env
    rel = seen(e, L311)[L311]
    result = downloads.grab(e.session, rel, e.prowlarr, e.sab, TODAY)
    assert result.ok, result.reasons
    g = result.grab
    assert e.fake_s.added[0][0] == "mags"
    assert issue(e).status is IssueStatus.GRABBED
    assert any(c["path"] == "/1/download" for c in e.fake_p.calls)

    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert r.updated == 1 and g.status is GrabStatus.QUEUED

    job_dir = finish(e, g, {"Linux311.pdf": b"magazine", "Linux311.nfo": b"x"})
    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert r.imported == [g] and g.status is GrabStatus.IMPORTED
    final = e.settings.library_dir / TARGET
    assert final.read_bytes() == b"magazine"
    assert issue(e).status is IssueStatus.IMPORTED and issue(e).file_path == TARGET
    assert not job_dir.exists()  # leftovers removed
    assert e.complete.exists()  # the category folder itself stays
    assert e.session.scalars(select(FileMove)).one().ok


def test_leftovers_kept_when_turned_off(env):
    e = env
    library.set_setting(e.session, downloads.LEFTOVERS_KEY, False)
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    job_dir = finish(e, g, {"Linux311.pdf": b"magazine", "Linux311.nfo": b"x"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED and (job_dir / "Linux311.nfo").exists()


def test_best_format_is_imported(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, g, {"Linux311.cbz": b"comic", "Linux311.pdf": b"pdf"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert (e.settings.library_dir / TARGET).read_bytes() == b"pdf"


def test_grab_refusals(env):
    e = env
    rels = seen(e, L311, L311_ALT)
    assert downloads.grab(e.session, rels[L311], e.prowlarr, e.sab, TODAY).ok
    second = downloads.grab(e.session, rels[L311_ALT], e.prowlarr, e.sab, TODAY)
    assert not second.ok and any("already grabbed" in r for r in second.reasons)
    assert len(e.fake_s.added) == 1


def test_grab_refused_without_category(env):
    e = env
    library.set_setting(e.session, downloads.CATEGORY_KEY, None)
    res = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    assert not res.ok and "category" in res.reasons[0] and e.fake_s.added == []


def test_grab_limit_respected(env):
    e = env
    rel = seen(e, L311)[L311]
    e.fake_p.stats = {
        "indexers": [
            {"indexerId": 1, "numberOfQueries": 0, "numberOfRssQueries": 0, "numberOfGrabs": 5}
        ]
    }
    import fake_prowlarr

    fake_prowlarr.INDEXERS[0]["fields"].append({"name": "baseSettings.grabLimit", "value": 5})
    try:
        res = downloads.grab(e.session, rel, e.prowlarr, e.sab, TODAY)
    finally:
        fake_prowlarr.INDEXERS[0]["fields"].pop()
    assert not res.ok and "grab limit reached" in res.reasons[0]


def test_bad_nzb_is_blocklisted(env):
    e = env
    e.fake_p.nzb = make_nzb("cover.jpg", "info.nfo")
    rel = seen(e, L311)[L311]
    res = downloads.grab(e.session, rel, e.prowlarr, e.sab, TODAY)
    assert not res.ok and e.fake_s.added == []
    assert e.session.scalars(select(BlockedRelease)).one().guid == rel.guid
    again = downloads.grab(e.session, rel, e.prowlarr, e.sab, TODAY)
    assert any(r.startswith("blocklisted") for r in again.reasons)


def test_failed_download_blocklists_and_tries_next(env):
    e = env
    rels = seen(e, L311, L311_ALT)
    first = downloads.grab(e.session, rels[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, first, {}, status="Failed", msg="Repair failed")
    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert first.status is GrabStatus.FAILED and "Repair failed" in first.error
    [retry] = r.retried
    assert retry.title == L311_ALT and issue(e).status is IssueStatus.GRABBED
    blocked = e.session.scalars(select(BlockedRelease)).one()
    assert blocked.guid == rels[L311].guid

    # The retry fails too; with no candidates left, it stops.
    finish(e, retry, {}, folder="alt", status="Failed", msg="Out of retention")
    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert r.retried == [] and issue(e).status is IssueStatus.WANTED
    assert len(e.fake_s.added) == 2


def test_download_without_magazine_file_fails(env):
    e = env
    rels = seen(e, L311, L311_ALT)
    g = downloads.grab(e.session, rels[L311], e.prowlarr, e.sab, TODAY).grab
    finish(e, g, {"readme.txt": b"x"})
    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.FAILED and "no pdf" in g.error
    assert len(r.retried) == 1


def test_collision_goes_to_review(env):
    e = env
    existing = e.settings.library_dir / TARGET
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already here")
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    job_dir = finish(e, g, {"Linux311.pdf": b"new"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.REVIEW
    assert existing.read_bytes() == b"already here"
    assert (job_dir / "Linux311.pdf").read_bytes() == b"new"
    item = e.session.get(ImportItem, g.import_item_id)
    assert item.action is ItemAction.REVIEW


def test_file_for_another_issue_goes_to_review(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    job_dir = finish(e, g, {"Linux.Magazine.No.305.2026.HYBRID.MAGAZINE.pdf": b"wrong"})
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.REVIEW and "looks like n305" in g.error
    assert (job_dir / "Linux.Magazine.No.305.2026.HYBRID.MAGAZINE.pdf").exists()


def test_path_outside_downloads_is_not_touched(env, tmp_path):
    e = env
    library.set_setting(e.session, downloads.PATH_MAP_KEY, [])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "Linux311.pdf").write_bytes(b"x")
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    e.fake_s.complete(g.job_id, str(outside))
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.REVIEW and "outside" in g.error
    assert (outside / "Linux311.pdf").exists()


def test_removed_job_is_not_blocklisted(env):
    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    e.fake_s.queue.clear()
    r = downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert r.removed == [g] and g.status is GrabStatus.REMOVED
    assert e.session.scalars(select(BlockedRelease)).all() == []
    assert issue(e).status is IssueStatus.WANTED


def test_safe_to_remove_guards(tmp_path):
    downloads_dir, library_dir = tmp_path / "dl", tmp_path / "lib"
    (downloads_dir / "job").mkdir(parents=True)
    library_dir.mkdir()
    assert downloads._safe_to_remove(downloads_dir / "job", downloads_dir, library_dir) is None
    assert downloads._safe_to_remove(downloads_dir, downloads_dir, library_dir)
    assert downloads._safe_to_remove(tmp_path / "other", downloads_dir, library_dir)
    assert "library" in downloads._safe_to_remove(
        downloads_dir / "job", downloads_dir, downloads_dir / "job" / "lib"
    )
    try:
        (downloads_dir / "job" / "link").symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert "symlink" in downloads._safe_to_remove(downloads_dir / "job", downloads_dir, library_dir)


def test_map_path():
    pm = [["/data/complete/mags", "/downloads"], ["/data", "/other"]]
    assert (
        downloads.map_path("/data/complete/mags/Job 1", pm).replace("\\", "/") == "/downloads/Job 1"
    )
    assert downloads.map_path("/data/x", pm).replace("\\", "/") == "/other/x"
    assert downloads.map_path("/nomatch/y", pm) == "/nomatch/y"


# Auto-grab


def test_auto_grab_off_by_default(env):
    e = env
    seen(e, L311)
    assert downloads.auto_grab(e.session, e.prowlarr, e.sab, TODAY) == []
    assert e.fake_s.added == []


def test_auto_grab_respects_limit(env):
    e = env
    library.set_setting(e.session, downloads.AUTO_KEY, True)
    library.set_setting(e.session, downloads.MAX_PER_RUN_KEY, 1)
    seen(e, L311, "Tabletop.Gaming.No.119.2026.HYBRiD.MAGAZiNE.eBook-LiBRiCiDE")
    results = downloads.auto_grab(e.session, e.prowlarr, e.sab, TODAY)
    assert sum(r.ok for r in results) == 1 and len(e.fake_s.added) == 1
    assert downloads.auto_grab(e.session, e.prowlarr, e.sab, TODAY)[0].ok
    assert len(e.fake_s.added) == 2
    assert downloads.auto_grab(e.session, e.prowlarr, e.sab, TODAY) == []


def test_downloads_job_skips_without_sab(tmp_path):
    from periodicool.jobs import run_downloads

    settings = load_settings({"PERIODICOOL_CONFIG_DIR": str(tmp_path / "c")})
    settings.config_dir.mkdir()
    db.upgrade(settings.db_url)
    engine = db.make_engine(settings.db_url)
    with db.make_session_factory(engine)() as s:
        assert run_downloads(s, settings, None).startswith("skipped")
    engine.dispose()


def test_grab_rows_never_hold_keys(env):
    e = env
    downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY)
    g = e.session.scalars(select(Grab)).one()
    for value in vars(g).values():
        assert KEY not in str(value) and SAB_KEY not in str(value)


def test_refused_cleanup_still_counts_as_imported(env, monkeypatch):
    """A job folder owned by another user: the file reaches the library, the original
    and the leftovers stay, and the import is still recorded."""
    import shutil

    e = env
    g = downloads.grab(e.session, seen(e, L311)[L311], e.prowlarr, e.sab, TODAY).grab
    job_dir = finish(e, g, {"Linux311.pdf": b"magazine", "Linux311.nfo": b"x"})
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.is_relative_to(e.settings.downloads_dir):
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **k)

    def refuse_tree(path, *a, **k):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr(shutil, "rmtree", refuse_tree)
    downloads.monitor(e.session, e.sab, e.settings, e.prowlarr, TODAY)
    assert g.status is GrabStatus.IMPORTED, g.error
    assert (e.settings.library_dir / TARGET).read_bytes() == b"magazine"
    assert (job_dir / "Linux311.pdf").exists() and (job_dir / "Linux311.nfo").exists()
    move = e.session.scalars(select(FileMove)).one()
    assert move.ok and move.method.endswith("-kept")
