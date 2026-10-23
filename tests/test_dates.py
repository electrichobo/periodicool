"""Date and time display formats from Settings, UI."""

from datetime import date, datetime

import pytest

from periodicool import dates

D = date(2026, 10, 22)  # a Thursday


@pytest.fixture(autouse=True)
def reset():
    saved = dict(dates.DISPLAY)
    yield
    dates.DISPLAY.clear()
    dates.DISPLAY.update(saved)


@pytest.mark.parametrize(
    ("style", "text"),
    [
        ("iso", "2026-10-22"),
        ("mon-d-y", "Oct 22 2026"),
        ("d-mon-y", "22 Oct 2026"),
        ("m/d/y", "10/22/2026"),
        ("d/m/y", "22/10/2026"),
        ("d.m.y", "22.10.2026"),
    ],
)
def test_short_dates(style, text):
    assert dates.short_date(D, style) == text
    assert dict(dates.SHORT_CHOICES)[style] == text  # the example shown in Settings


def test_long_dates_clock_and_week_headers():
    for style, text in dates.LONG_CHOICES:
        assert dates.long_date(D, style) == text
    assert dates.clock(datetime(2026, 1, 1, 17, 30), "24") == "17:30"
    assert dates.clock(datetime(2026, 1, 1, 17, 30), "12") == "5:30pm"
    assert dates.clock(datetime(2026, 1, 1, 0, 5), "12") == "12:05am"
    for style, text in dates.WEEK_HEADER_CHOICES:
        assert dates.week_header(D, style) == text


def test_relative_dates_and_odd_input():
    dates.DISPLAY["relative"] = True
    assert dates.short_date(D, today=D) == "Today"
    assert dates.short_date(D, today=date(2026, 10, 23)) == "Yesterday"
    assert dates.short_date(D, today=date(2026, 10, 1)) == "2026-10-22"
    assert dates.short_date(None) == "" and dates.short_date("2026-10-22", "d/m/y") == "22/10/2026"
    assert dates.short_date("Autumn 2026") == "Autumn 2026"


def test_ui_tab_applies_formats_and_theme(client):
    form = {
        "ui__first_weekday": "0",
        "ui__week_header": "ddd",
        "ui__short_date": "d/m/y",
        "ui__long_date": "day-iso",
        "ui__time_format": "12",
        "ui__theme": "dark",
    }
    assert client.post("/settings/ui", data=form, follow_redirects=False).status_code == 303
    assert dates.DISPLAY["short"] == "d/m/y" and dates.DISPLAY["time"] == "12"
    page = client.get("/calendar").text
    assert 'data-theme="dark"' in page
    client.post("/settings/ui", data=form | {"ui__theme": "auto"})
