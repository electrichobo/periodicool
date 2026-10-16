"""Title specs for the dev test magazines, keyed by fixture slug."""

from periodicool.matching import TitleSpec

AMERICAN_CINEMATOGRAPHER = TitleSpec("American Cinematographer")
AMAZONAS = TitleSpec("AMAZONAS Magazine")
HACKER_QUARTERLY = TitleSpec("2600: The Hacker Quarterly")
# The North American edition appears as "Linux Magazine USA", which parses to
# region USA, so no alias is needed and any region is accepted.
LINUX_MAGAZINE = TitleSpec("Linux Magazine", filler_optional=False)
MAKE = TitleSpec("Make:")
FORTEAN_TIMES = TitleSpec("Fortean Times")
TABLETOP_GAMING = TitleSpec("Tabletop Gaming Magazine")
PEOPLE = TitleSpec("People", region="USA", special_prefix=False)

BY_FIXTURE = {
    "american-cinematographer": AMERICAN_CINEMATOGRAPHER,
    "amazonas": AMAZONAS,
    "2600": HACKER_QUARTERLY,
    "linux-magazine": LINUX_MAGAZINE,
    "linux-pro-magazine": LINUX_MAGAZINE,
    "make": MAKE,
    "fortean-times": FORTEAN_TIMES,
    "tabletop-gaming": TABLETOP_GAMING,
    "people": PEOPLE,
}
