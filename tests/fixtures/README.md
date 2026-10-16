# Fixtures

Real release names used by the parser tests. Names only: no content, no download links, no GUIDs, no indexer names, no keys.

## releases/

One JSON file per search, collected from Prowlarr category 7010 (Books/Mags) by `scripts/collect_fixtures.py`. Each file holds the magazine, the query, the collection date and the de-duplicated results (`title`, `size`, `publish_date`, `categories`). Titles are kept exactly as the indexers returned them, including junk (poster spam, truncation, HTML entities such as `&amp;`, mojibake). Results include plenty of other magazines that matched the query; those are the negative cases.

To refresh (costs one API hit per query per indexer):

```bash
PROWLARR_API_KEY=... python scripts/collect_fixtures.py --url http://prowlarr:9696 --refresh
```

## What the names show (collected 2026-10-16)

| Fixture | Formats seen | Traps |
| - | - | - |
| american-cinematographer | `Title-Month.YYYY`, `Title.TruePDF-Month.YYYY`, `Title.Magazine-...`, `Title.YYYY-MM` | Year-only (`TruePDF-2016`), month without year, `..` and `??` noise, `NN - MAGAZINE ... .par2 - 1958 MB` post subjects, `- SPIDAROT.SHARE -` tags |
| amazonas | `Amazonas[.Magazine\|.TruePDF]-Month.Month.YYYY` (bimonthly pairs) | No German edition seen; no names carry a language. Region filtering is untested by these fixtures |
| 2600 | `2600.Magazine.The.Hacker.Quarterly-Season.YYYY` | No volume or issue numbers in names. Query "2600" also matches post subjects with `2600 MB` |
| linux-magazine | `Linux.Magazine.No.NNN.YYYY...`, `Linux.Magazine.USA-Month.YYYY`, `...USA.Issue.NNN-Month.YYYY`, `Linux Magazine USA - Issue NNN, Month YYYY`, `Linux_Magazine_-_Month_YYYY` | Dutch `Linux Magazine #NN (YYYY)`, German `Linux-Magazin`, Linux Format, Linux Journal, Linux User, Specials |
| linux-pro-magazine | none for the English title | Italian `Linux.Pro.No.NNN` is a different magazine. The North American edition shows up as "Linux Magazine USA", not "Linux Pro Magazine" |
| make | `MAKE Magazine vol NN`, `Vol.NN True PDF English`, `Volume.NN.YYYY`, `Issue.NN.YYYY`, `Make.No.NN.YYYY`, `Month.Month.YYYY` | Packs (`01-45`, `Volumes.01-37`), WineMaker, Musicmaker, MovieMaker, Make Your Own PC, par2/sfv/nfo/rar file subjects |
| fortean-times | `Fortean.Times[.TruePDF]-Month.YYYY`, `.Issue.NNN[.TruePDF]-Month.YYYY`, `.No.NNN.YYYY` | Handbook special, `.jpg` cover posts, yEnc subjects, month without year |
| tabletop-gaming | `Tabletop.Gaming-Month.YYYY`, `-DD.Month.YYYY`, `.Issue.NN-Month.YYYY`, `.No.NNN.YYYY`, `.Issue.NN.YYYY-MM` | Early issues were seasonal and bimonthly (`Issue.2-Autumn.2015`, `Issue.9-April.May.2017`); `08.09.TruePDF-2017` |
| people | `People.USA[.TruePDF]-DD.Month.YYYY`, `People - Vol. NN No. NN DD Mon YYYY`, `People USA 02 10 2025` | Regional editions (USA, Australia, South Africa), `People.en.Espaol`, specials and yearbooks, Real People, Sunday People, The People's Friend, People Matters, StyleWatch, truncated `People.USA.TruePDF-12` |
