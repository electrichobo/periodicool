# Configuration

Only paths, process settings and secrets come from the environment. Everything else lives in the database and is edited in the web UI, and it is included in backups.

## Environment variables

| Variable | Default | Purpose |
| - | - | - |
| `PERIODICOOL_CONFIG_DIR` | `/config` | Database and app data |
| `PERIODICOOL_LIBRARY_DIR` | `/library` | Magazine library |
| `PERIODICOOL_DOWNLOADS_DIR` | `/downloads` | The download client's completed folder for PeriodiCool's category, mounted where the client sees it |
| `PERIODICOOL_IMPORT_DIR` | unset | Optional drop-in import folder |
| `PERIODICOOL_HOST` | `0.0.0.0` | Bind address |
| `PERIODICOOL_PORT` | `8383` | Port. Also in Settings, General; the variable wins when set |
| `PERIODICOOL_URL_BASE` | unset | Serve under a path such as `/periodicool`, for a reverse proxy. Also in Settings, General |
| `PERIODICOOL_ALLOWED_HOSTS` | unset | Comma-separated host names to answer to (localhost always works). Also in Settings, General |
| `PUID` / `PGID` | `99` / `100` | User and group the container runs as (never root) |
| `UMASK` | `002` | File creation mask |
| `TZ` | `UTC` | Time zone |
| `PROWLARR_API_KEY` | unset | Optional key for the first Prowlarr entry when it has none saved |
| `SABNZBD_API_KEY` | unset | Optional key for the first SABnzbd entry when it has none saved |
| `PERIODICOOL_JOBS` | `on` | `off` turns the background jobs off |
| `PERIODICOOL_SECRET` | unset | Signing secret for CSRF tokens; generated into `/config/secret.key` when unset |

## Settings tabs

- **Media management:**
  - the naming template, with examples (default `{Title}/{Year}/{Title} - {YYYY-MM}.{ext}`);
  - leftover cleanup and the import folder;
  - format profiles: which formats count, which is preferred, upgrades.
- **Indexers:**
  - Prowlarr, Newznab (with Sonarr's presets) and Torznab entries;
  - per entry, Enable RSS, Automatic Search and Interactive Search, categories (Load categories), priority, an optional download client, daily limits and torrent options (under Show advanced);
  - below the list: release size and age limits, retention, the default categories, RSS and search timing.
- **Download clients:**
  - SABnzbd, NZBGet, Usenet Blackhole, qBittorrent, Transmission, Deluge, rTorrent and Torrent Blackhole, each with its own fields;
  - remote path mappings;
  - automatic grabbing (off by default).
- **Metadata:**
  - the opt-in lookups: Library of Congress, Wikidata, Internet Archive. All are off by default; turning them on improves matching.
  - Each sends only an ISSN or a title, results are cached, and nothing changes until you confirm.
- **Connect:** webhook connections. See [api.md](api.md#webhooks).
- **Tags:** tags used on magazines.
- **General:**
  - login mode (required, local network, off), trust the reverse proxy, log level;
  - port, URL base, allowed hosts, Application URL;
  - the API key.
- **UI:** date and time formats, theme (light, dark, system), effects.

## Backups and logs

System, Backups:
- makes a copy of the database on a schedule (weekly by default) or on demand, and restores one;
- backups hold every saved key and password, so keep them private.

System, Log files keeps the rotated logs. Secrets are scrubbed from every log line.

## Safety rules

The safety rules don't change with any setting:
- **Download clients:** PeriodiCool only adds jobs in its own category, and only reads, changes or removes jobs it created.
- **Library files:** never deleted, only moved and renamed. A name collision keeps both files and sends the newcomer to review.
- **Torrents:** a torrent's files are never moved or removed.
