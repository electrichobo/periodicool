"""bencode, info hashes and the qBittorrent client against a fake. No network."""

import hashlib

import pytest

from fake_qbit import FOREIGN_HASH, QB_PASS, QB_URL, QB_USER, FakeQbit, bencode, make_torrent
from periodicool.bencode import BencodeError, file_names, info_hash, magnet_hash
from periodicool.clients.base import ClientError, JobStatus
from periodicool.clients.qbittorrent import QbittorrentClient


def client(fake, password=QB_PASS):
    return QbittorrentClient(QB_URL, QB_USER, password, transport=fake, min_interval=0)


# bencode


def test_info_hash_and_file_names():
    data = make_torrent(files=("a/Linux 311.pdf", "readme.txt"))
    info = bencode(
        {
            "name": "Linux Magazine 311",
            "piece length": 16384,
            "pieces": b"\x00" * 20,
            "files": [
                {"length": 1000, "path": ["a/Linux 311.pdf"]},
                {"length": 1000, "path": ["readme.txt"]},
            ],
        }
    )
    assert info_hash(data) == hashlib.sha1(info).hexdigest()
    assert file_names(data) == ["a/Linux 311.pdf", "readme.txt"]
    single = bencode(
        {"info": {"name": "One.pdf", "length": 5, "piece length": 1, "pieces": b"x" * 20}}
    )
    assert file_names(single) == ["One.pdf"]


def test_v2_only_torrent_hash():
    info = {
        "name": "x",
        "meta version": 2,
        "piece length": 16384,
        "file tree": {"x.pdf": {"": {"length": 3}}},
    }
    data = bencode({"info": info})
    assert info_hash(data) == hashlib.sha256(bencode(info)).hexdigest()[:40]
    assert file_names(data) == ["x.pdf"]


def test_magnet_hashes():
    hexhash = "0123456789abcdef0123456789abcdef01234567"
    assert magnet_hash(f"magnet:?xt=urn:btih:{hexhash.upper()}&dn=x") == hexhash
    import base64

    b32 = base64.b32encode(bytes.fromhex(hexhash)).decode()
    assert magnet_hash(f"magnet:?dn=x&xt=urn:btih:{b32}") == hexhash
    assert magnet_hash("magnet:?xt=urn:btmh:1220" + "ab" * 32) == ("ab" * 32)[:40]


@pytest.mark.parametrize(
    "bad",
    [b"", b"<nzb>", b"d4:spami3e", b"l4:spame", b"d4:infod4:name3:abc", b"d4:infoi3ee"],
)
def test_bad_torrents_refused(bad):
    with pytest.raises(BencodeError):
        info_hash(bad)


def test_bad_magnets_refused():
    for bad in ("http://x", "magnet:?dn=x", "magnet:?xt=urn:btih:zz"):
        with pytest.raises(BencodeError):
            magnet_hash(bad)


# qBittorrent client


def test_login_check_and_categories():
    fake = FakeQbit()
    qb = client(fake)
    assert qb.check("mags") == "v5.0.1"
    assert fake.calls[0]["path"] == "/api/v2/auth/login"
    assert all(c["headers"]["Referer"] == QB_URL for c in fake.calls)
    with pytest.raises(ClientError, match="does not exist"):
        qb.check("magazines")
    with pytest.raises(ClientError, match="username or password"):
        client(FakeQbit(), password="wrong").version()


def test_add_torrent_and_magnet_with_seed_goals():
    fake = FakeQbit()
    qb = client(fake)
    data = make_torrent()
    job = qb.add_torrent("Linux 311", "mags", torrent=data, seed_ratio=1.5, seed_time_min=4320)
    assert job == info_hash(data)
    form = fake.added[0]
    assert form["category"] == b"mags" and form["ratioLimit"] == b"1.5"
    assert form["seedingTimeLimit"] == b"4320" and form["torrents"] == data
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    assert qb.add_torrent("Linux 311", "mags", magnet=magnet) == "a" * 40
    assert fake.added[1]["urls"] == magnet.encode() and "ratioLimit" not in fake.added[1]
    with pytest.raises(ClientError):
        qb.add_torrent("x", "mags", torrent=b"<nzb/>")
    with pytest.raises(ClientError, match="not NZB"):
        qb.add_nzb(b"<nzb/>", "x", "mags")


def test_jobs_only_ours_and_states():
    fake = FakeQbit()
    qb = client(fake)
    job = qb.add_torrent("Linux 311", "mags", torrent=make_torrent())
    [state] = qb.jobs([job], "mags")
    assert state.status is JobStatus.DOWNLOADING and state.time_left_s is None
    # The foreign torrent comes back from the fake but never out of the client.
    assert qb.jobs([FOREIGN_HASH], "mags") == []
    assert all(s.job_id != FOREIGN_HASH for s in qb.jobs([job, FOREIGN_HASH], "mags"))
    for raw, expected in [
        ("stalledDL", JobStatus.DOWNLOADING),
        ("pausedDL", JobStatus.QUEUED),
        ("stalledUP", JobStatus.COMPLETED),
        ("error", JobStatus.FAILED),
        ("missingFiles", JobStatus.FAILED),
    ]:
        fake.set_state(
            job, raw, 1.0 if raw.endswith("UP") else 0.5, "/data/torrents/mags/Linux 311"
        )
        [s] = qb.jobs([job], "mags")
        assert s.status is expected, raw
    assert s.storage_path == "/data/torrents/mags/Linux 311"


def test_remove_keeps_files_and_only_ours():
    fake = FakeQbit()
    qb = client(fake)
    job = qb.add_torrent("Linux 311", "mags", torrent=make_torrent())
    assert qb.remove(FOREIGN_HASH, "mags") is False
    assert qb.remove(job, "mags") is True
    assert fake.deleted == [(job, "false")]
    assert FOREIGN_HASH in fake.torrents


def test_session_expiry_logs_in_again():
    fake = FakeQbit()
    qb = client(fake)
    qb.version()
    qb._sid = "expired"
    assert qb.version() == "v5.0.1"
    assert [c["path"] for c in fake.calls].count("/api/v2/auth/login") == 2


def test_password_is_redacted():
    qb = client(FakeQbit())
    assert QB_PASS not in qb.redact(f"failed with {QB_PASS} and SID=abc123")
