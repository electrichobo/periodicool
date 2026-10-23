"""Page layout from the maintainer's redesign: navigation, moved pages, issue rows."""

import re

from periodicool.models import Magazine, MonitorMode

from test_web_magazines import fakes, mags  # noqa: F401


def test_library_is_gone_from_the_nav_and_redirects(client):
    page = client.get("/").text
    nav = page[page.index('aria-label="Main"') : page.index("</nav>")]
    assert ">Library<" not in nav and ">Settings<" in nav
    resp = client.get("/library?scan=4", follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/import?scan=4"
    assert "Manual import" in client.get("/import").text


def test_magazines_toolbar_has_manual_import_and_scan(client):
    page = client.get("/").text
    assert 'href="/import">Manual import</a>' in page
    assert 'hx-post="/library/scan"' in page and "Scan for changes" in page
    assert "Sector 01" in page


def test_media_management_holds_import_and_profiles(client):
    page = client.get("/settings/media").text
    assert 'href="/import">Manual import</a>' in page
    assert 'id="profiles"' in page and "Format profiles" in page
    tabs = page[
        page.index('aria-label="Settings"') : page.index(
            "</nav>", page.index('aria-label="Settings"')
        )
    ]
    assert ">Profiles<" not in tabs
    resp = client.get("/settings/profiles?edit=1", follow_redirects=False)
    assert resp.headers["location"] == "/settings/media?edit=1#profiles"


def test_status_includes_health_and_more_info(client):
    page = client.get("/system/status").text
    assert 'id="health"' in page and "Test connections" in page
    assert (
        "More info" in page and 'href="https://github.com/electrichobo/periodicool/issues"' in page
    )
    tabs = page[page.index('aria-label="System"') :]
    assert ">Health<" not in tabs[: tabs.index("</nav>")]
    resp = client.get("/system/health", follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/system/status#health"


def test_issue_rows_have_search_icons_and_open_details(client, mags):  # noqa: F811
    page = client.get(f"/magazines/{mags['American Cinematographer']}").text
    assert "Pick</button>" not in page and "Details</button>" not in page
    assert 'title="Automatic search"' in page and 'title="Interactive search"' in page
    assert re.search(r'<tr class="is-clickable" data-open="/issues/\d+">', page)
    assert "&larr; Back</a>" in page
    wanted = client.get("/wanted/missing").text
    assert "Pick</button>" not in wanted and 'title="Interactive search"' in wanted


def test_year_jump_links_to_each_year(client, mags):  # noqa: F811
    mag_id = mags["American Cinematographer"]
    with client.app.state.sessions() as s:
        mag = s.get(Magazine, mag_id)
        mag.monitor_mode, mag.monitor_from_year = MonitorMode.FROM_YEAR, 2022
        s.commit()
    client.post("/magazines/refresh-all")
    page = client.get(f"/magazines/{mag_id}").text
    years = re.findall(r'href="#year-(\d{4})" data-open-fold', page)
    assert len(years) > 1
    for year in years:
        assert f'id="year-{year}"' in page
    assert page.index('class="year-jump"') < page.index('id="years-title"')


def test_wanted_filter(client, mags):  # noqa: F811
    page = client.get("/wanted/missing").text
    assert 'role="search"' in page and "All magazines" in page
    people = mags["People"]
    only = client.get("/wanted/missing", params={"magazine": people}).text
    titles = set(re.findall(r'<td><a href="/magazines/(\d+)">', only))
    assert titles == {str(people)}
    named = client.get("/wanted/missing", params={"q": "fortean"}).text
    assert set(re.findall(r'<td><a href="/magazines/(\d+)">', named)) == {
        str(mags["Fortean Times"])
    }
    none = client.get("/wanted/missing", params={"q": "no such magazine"}).text
    assert re.findall(r'<td><a href="/magazines/(\d+)">', none) == []
    assert 'href="?">Clear</a>' in none
    cutoff = client.get("/wanted/cutoff", params={"q": "x", "magazine": "abc"}).text
    assert 'value="x"' in cutoff


def test_wanted_pages_keep_the_filter(client, mags):  # noqa: F811
    page = client.get("/wanted/missing", params={"magazine": mags["People"]}).text
    if 'class="pager"' in page:
        assert f"?magazine={mags['People']}&amp;page=2" in page


def test_interactive_search_shows_torrents(client, mags, fakes):  # noqa: F811
    from fake_qbit import QB_URL, FakeQbit
    from rows import add_client

    from fake_prowlarr import release_json

    fake_p, _ = fakes
    client.app.state.qbit_transport = FakeQbit()
    with client.app.state.sessions() as s:
        add_client(s, QB_URL, name="qBit", kind="qbittorrent", protocol="torrent", category="mags")
        s.commit()
    fake_p.search_results = [
        release_json("Linux Magazine USA - Issue 311, October 2026", indexer_id=3)
        | {"protocol": "torrent", "seeders": 8, "leechers": 2}
    ]
    page = client.get(f"/magazines/{mags['Linux Magazine']}/interactive").text
    assert '<span class="tag">Torrent</span> 8 seeders, 2 peers' in page
