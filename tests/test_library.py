"""Library scan, organize and queue. Every test works on a throwaway tmp tree."""

import errno
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from periodicool import db, library
from periodicool.cli import magazine_from_dict
from periodicool.library import FIRST_SCAN_KEY, apply_scan, plan_scan, process_import_folder
from periodicool.models import (
    FileMove,
    FormatProfile,
    ImportItem,
    Issue,
    IssueStatus,
    ItemAction,
    ItemStatus,
    Magazine,
    ReasonCode,
    ScanKind,
)

EXAMPLES = Path(__file__).parent.parent / "examples" / "magazines.json"


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.upgrade(url)
    engine = db.make_engine(url)
    with db.make_session_factory(engine)() as s:
        profile = s.scalars(select(FormatProfile)).one()
        for entry in json.loads(EXAMPLES.read_text(encoding="utf-8")):
            mag = magazine_from_dict(entry)
            mag.format_profile = profile
            s.add(mag)
        s.flush()
        yield s
    engine.dispose()


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


def put(root: Path, rel: str, data: bytes = b"magazine") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def items(session, scan) -> dict[str, ImportItem]:
    rows = session.scalars(select(ImportItem).where(ImportItem.scan_id == scan.id))
    return {Path(i.source_path).name: i for i in rows}


def mag_id(session, title):
    return session.scalars(select(Magazine.id).where(Magazine.title == title)).one()


# Preview


def test_preview_is_read_only(session, lib):
    put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    put(lib, "Fortean Times/2023/2023-11.pdf")
    put(lib, "random/MAKE Magazine vol 05.pdf")
    put(lib, "random/cover.jpg")
    before = tree(lib)
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    assert tree(lib) == before
    assert scan.counts["move"] == 3 and scan.counts["ignored"] == 1
    assert session.scalars(select(FileMove)).all() == []


def test_plan_targets_and_reasons(session, lib):
    put(lib, "Fortean Times/2023/2023-11.pdf")  # folder names supply the title
    put(lib, "2600.Magazine.The.Hacker.Quarterly-Winter.2015.pdf")
    put(lib, "Linux Magazine #04 (2024).pdf")
    put(lib, "People.Australia.TruePDF-08.April.2019.pdf")
    put(lib, "Some Other Mag - 2020-01.pdf")
    put(lib, "Fortean.Times.TruePDF-October.pdf")
    by_name = items(session, plan_scan(session, lib, ScanKind.LIBRARY))

    assert by_name["2023-11.pdf"].target_path == "Fortean Times/2023/Fortean Times - 2023-11.pdf"
    assert by_name["2600.Magazine.The.Hacker.Quarterly-Winter.2015.pdf"].target_path == (
        "2600 - The Hacker Quarterly/2015/2600 - The Hacker Quarterly - 2015-Winter.pdf"
    )
    dutch = by_name["Linux Magazine #04 (2024).pdf"]
    assert dutch.reason_code is ReasonCode.UNIDENTIFIED and "does not fit 2024" in dutch.reason
    au = by_name["People.Australia.TruePDF-08.April.2019.pdf"]
    assert au.reason_code is ReasonCode.NO_MAGAZINE and "region Australia" in au.reason
    assert by_name["Some Other Mag - 2020-01.pdf"].reason_code is ReasonCode.NO_MAGAZINE
    assert by_name["Fortean.Times.TruePDF-October.pdf"].reason_code is ReasonCode.LOW_CONFIDENCE


# Apply


def test_apply_moves_links_and_logs(session, lib):
    put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"ft351")
    put(lib, "random/MAKE Magazine vol 05.pdf", b"make5")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    result = apply_scan(session, scan.id, lib)

    assert result == {"moved": 2, "kept": 0, "failed": 0, "review": 0}
    assert (lib / "Fortean Times/2017/Fortean Times - 2017-03.pdf").read_bytes() == b"ft351"
    assert (lib / "Make/Make - Vol 05.pdf").read_bytes() == b"make5"
    assert not (lib / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").exists()

    issue = session.scalars(select(Issue).where(Issue.key == "2017-03")).one()
    assert issue.file_path == "Fortean Times/2017/Fortean Times - 2017-03.pdf"
    assert issue.status is IssueStatus.IMPORTED and issue.issue_number == 351
    assert not issue.expected
    make = session.scalars(select(Issue).where(Issue.key == "v5")).one()
    assert make.cover_date is None and make.volume_number == 5

    moves = session.scalars(select(FileMove)).all()
    assert len(moves) == 2 and all(m.ok and m.method == "link" for m in moves)
    assert library.get_setting(session, FIRST_SCAN_KEY) is True


def test_file_already_in_place_is_kept(session, lib):
    put(lib, "Fortean Times/2017/Fortean Times - 2017-03.pdf")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    assert scan.counts["keep"] == 1
    assert apply_scan(session, scan.id, lib)["kept"] == 1
    assert session.scalars(select(FileMove)).all() == []
    issue = session.scalars(select(Issue).where(Issue.key == "2017-03")).one()
    assert issue.file_path == "Fortean Times/2017/Fortean Times - 2017-03.pdf"


# Never overwrite


def test_existing_file_wins_and_newcomer_waits(session, lib):
    put(lib, "Fortean Times/2017/Fortean Times - 2017-03.pdf", b"incumbent")
    put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"newcomer")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    by_name = items(session, scan)
    newcomer = by_name["Fortean.Times.Issue.351.TruePDF-March.2017.pdf"]
    assert newcomer.action is ItemAction.REVIEW and newcomer.reason_code is ReasonCode.DUPLICATE
    apply_scan(session, scan.id, lib)
    assert (lib / "Fortean Times/2017/Fortean Times - 2017-03.pdf").read_bytes() == b"incumbent"
    assert (lib / "Fortean.Times.Issue.351.TruePDF-March.2017.pdf").read_bytes() == b"newcomer"


def test_target_appearing_after_plan_is_not_overwritten(session, lib):
    src = put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"ours")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    target = put(lib, "Fortean Times/2017/Fortean Times - 2017-03.pdf", b"someone else")
    result = apply_scan(session, scan.id, lib)

    assert result["review"] == 1 and result["moved"] == 0
    assert target.read_bytes() == b"someone else"
    assert src.read_bytes() == b"ours"
    item = items(session, scan)[src.name]
    assert item.action is ItemAction.REVIEW and item.reason_code is ReasonCode.COLLISION
    move = session.scalars(select(FileMove)).one()
    assert not move.ok and move.error == "target exists"


def test_same_target_claimed_twice(session, lib):
    put(lib, "a/Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"one")
    put(lib, "b/Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"two")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    codes = {i.reason_code for i in items(session, scan).values()}
    assert codes == {ReasonCode.DUPLICATE}  # equal format: both wait, neither guessed
    apply_scan(session, scan.id, lib)
    assert len(tree(lib)) == 2


def test_source_changed_after_plan(session, lib):
    src = put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"v1")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    src.write_bytes(b"version two")
    assert apply_scan(session, scan.id, lib)["failed"] == 1
    assert src.exists()


def test_link_unsupported_falls_back_to_rename(session, lib, monkeypatch):
    put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)

    def no_links(*a, **k):
        raise OSError(errno.EPERM, "links not supported")

    monkeypatch.setattr(os, "link", no_links)
    assert apply_scan(session, scan.id, lib)["moved"] == 1
    assert session.scalars(select(FileMove)).one().method == "rename"


def _cross_device(*a, **k):
    raise OSError(errno.EXDEV, "cross-device link")


def test_library_files_never_copied_across_devices(session, lib, monkeypatch):
    src = put(lib, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    monkeypatch.setattr(os, "link", _cross_device)
    assert apply_scan(session, scan.id, lib)["failed"] == 1
    assert src.exists()
    assert not (lib / "Fortean Times").exists() or not any((lib / "Fortean Times").rglob("*.pdf"))


def test_import_folder_copy_across_devices(session, lib, tmp_path, monkeypatch):
    drop = tmp_path / "import"
    src = put(drop, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"payload")
    library.set_setting(session, FIRST_SCAN_KEY, True)
    monkeypatch.setattr(os, "link", _cross_device)
    process_import_folder(session, drop, lib)
    target = lib / "Fortean Times/2017/Fortean Times - 2017-03.pdf"
    assert target.read_bytes() == b"payload"
    assert not src.exists()
    assert not list(target.parent.glob(".*periodicool-tmp"))
    assert session.scalars(select(FileMove)).one().method == "copy"


# Duplicates


def test_true_pdf_beats_plain_pdf(session, lib):
    put(lib, "Fortean.Times.TruePDF-March.2017.pdf", b"true")
    put(lib, "Fortean.Times-March.2017.pdf", b"plain")
    by_name = items(session, plan_scan(session, lib, ScanKind.LIBRARY))
    assert by_name["Fortean.Times.TruePDF-March.2017.pdf"].action is ItemAction.MOVE
    loser = by_name["Fortean.Times-March.2017.pdf"]
    assert loser.reason_code is ReasonCode.DUPLICATE and "better format" in loser.reason


def test_pdf_beats_cbz(session, lib):
    put(lib, "MAKE Magazine vol 05.pdf")
    put(lib, "MAKE Magazine vol 05.cbz")
    by_name = items(session, plan_scan(session, lib, ScanKind.LIBRARY))
    assert by_name["MAKE Magazine vol 05.pdf"].action is ItemAction.MOVE
    assert by_name["MAKE Magazine vol 05.cbz"].reason_code is ReasonCode.DUPLICATE


def test_equal_duplicates_both_wait(session, lib):
    put(lib, "People.USA.TruePDF-03.December.2017.pdf")
    put(lib, "People.USA.TruePDF-04.December.2017.pdf")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    assert scan.counts["review"] == 2 and scan.counts["move"] == 0


def test_second_issue_in_a_month(session, lib):
    put(lib, "Fortean.Times.Issue.420-August.2022.pdf")
    put(lib, "Fortean.Times.Issue.421-August.2022.pdf")
    by_name = items(session, plan_scan(session, lib, ScanKind.LIBRARY))
    first = by_name["Fortean.Times.Issue.420-August.2022.pdf"]
    second = by_name["Fortean.Times.Issue.421-August.2022.pdf"]
    assert (first.issue_key, second.issue_key) == ("2022-08", "2022-08-2")
    assert second.target_path == "Fortean Times/2022/Fortean Times - 2022-08-2.pdf"


def test_issue_with_a_file_is_not_replaced(session, lib):
    put(lib, "Fortean Times/2017/Fortean Times - 2017-03.pdf", b"old")
    apply_scan(session, plan_scan(session, lib, ScanKind.LIBRARY).id, lib)
    put(lib, "incoming/Fortean.Times.Issue.351.TruePDF-March.2017.pdf", b"new")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    item = items(session, scan)["Fortean.Times.Issue.351.TruePDF-March.2017.pdf"]
    assert item.reason_code is ReasonCode.DUPLICATE


# Rescans and the queue


def test_rescan_skip_change_and_gone(session, lib):
    a = put(lib, "Some Other Mag - 2020-01.pdf")
    put(lib, "Fortean.Times.TruePDF-October.pdf")
    first = plan_scan(session, lib, ScanKind.LIBRARY)
    ids = {n: i.id for n, i in items(session, first).items()}

    library.skip(session, ids["Some Other Mag - 2020-01.pdf"])
    second = plan_scan(session, lib, ScanKind.LIBRARY)
    assert second.counts["unchanged"] == 1 and second.counts["review"] == 1
    assert {n: i.id for n, i in items(session, second).items()} == ids
    assert session.get(ImportItem, ids["Some Other Mag - 2020-01.pdf"]).status is (
        ItemStatus.SKIPPED
    )

    a.write_bytes(b"a different file now")
    plan_scan(session, lib, ScanKind.LIBRARY)
    assert session.get(ImportItem, ids["Some Other Mag - 2020-01.pdf"]).status is (
        ItemStatus.PENDING
    )

    (lib / "Fortean.Times.TruePDF-October.pdf").rename(lib.parent / "elsewhere.pdf")
    plan_scan(session, lib, ScanKind.LIBRARY)
    assert session.get(ImportItem, ids["Fortean.Times.TruePDF-October.pdf"]).status is (
        ItemStatus.GONE
    )


def test_queue_resolve_apply(session, lib):
    put(lib, "scan0042.pdf", b"mystery")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    [item] = library.queue(session)
    assert item.reason_code is ReasonCode.NO_MAGAZINE

    ft = mag_id(session, "Fortean Times")
    library.resolve(session, item.id, ft, "2020-05", lib)
    assert library.queue(session) == []
    assert plan_scan(session, lib, ScanKind.LIBRARY).counts["unchanged"] == 1

    apply_scan(session, item.scan_id, lib)
    assert (lib / "Fortean Times/2020/Fortean Times - 2020-05.pdf").read_bytes() == b"mystery"
    issue = session.scalars(select(Issue).where(Issue.key == "2020-05")).one()
    assert issue.status is IssueStatus.IMPORTED
    assert scan.id is not None


def test_resolve_refuses_taken_target(session, lib):
    put(lib, "Fortean Times/2020/Fortean Times - 2020-05.pdf")
    put(lib, "scan0042.pdf")
    plan_scan(session, lib, ScanKind.LIBRARY)
    item = next(i for i in library.queue(session) if i.source_path.endswith("scan0042.pdf"))
    with pytest.raises(FileExistsError):
        library.resolve(session, item.id, mag_id(session, "Fortean Times"), "2020-05", lib)


def test_resolve_rejects_bad_key(session, lib):
    put(lib, "scan0042.pdf")
    plan_scan(session, lib, ScanKind.LIBRARY)
    [item] = library.queue(session)
    with pytest.raises(ValueError, match="not an issue key"):
        library.resolve(session, item.id, mag_id(session, "Fortean Times"), "march", lib)


# Import folder


def test_import_folder_waits_for_first_library_apply(session, lib, tmp_path):
    drop = tmp_path / "import"
    src = put(drop, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    put(drop, "scan0042.pdf")

    process_import_folder(session, drop, lib)
    assert src.exists() and tree(lib) == {}

    put(lib, "MAKE Magazine vol 05.pdf")
    apply_scan(session, plan_scan(session, lib, ScanKind.LIBRARY).id, lib)
    scan = process_import_folder(session, drop, lib)
    assert not src.exists()
    assert (lib / "Fortean Times/2017/Fortean Times - 2017-03.pdf").exists()
    assert (drop / "scan0042.pdf").exists()  # unidentified: waits in the queue
    assert scan.counts["review"] == 1


def test_import_auto_apply_can_be_turned_off(session, lib, tmp_path):
    drop = tmp_path / "import"
    src = put(drop, "Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    library.set_setting(session, FIRST_SCAN_KEY, True)
    library.set_setting(session, library.AUTO_APPLY_KEY, False)
    process_import_folder(session, drop, lib)
    assert src.exists()


def test_hidden_and_partial_files_are_left_alone(session, lib):
    put(lib, ".hidden/Fortean.Times.Issue.351.TruePDF-March.2017.pdf")
    put(lib, "Fortean.Times.Issue.352.TruePDF-April.2017.pdf.part")
    put(lib, ".Fortean.Times.Issue.353.TruePDF-May.2017.pdf")
    scan = plan_scan(session, lib, ScanKind.LIBRARY)
    assert scan.counts["move"] == 0 and scan.counts["review"] == 0
