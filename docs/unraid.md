# Running PeriodiCool on Unraid

## Install

**From Community Apps:** search for PeriodiCool, once the template is listed there.

**Before it is listed:**
1. Save `unraid/periodicool.xml` from this repository as `/boot/config/plugins/dockerMan/templates-user/my-periodicool.xml`.
2. Docker, Add Container, then pick "periodicool" from the template list.

## Paths and settings

| Setting | Container | Default on Unraid | Notes |
| - | - | - | - |
| WebUI port | `8383` | `8383` | |
| Config | `/config` | `/mnt/user/appdata/periodicool` | Database, settings, backups, logs, API key |
| Library | `/library` | `/mnt/user/media/magazines/` | Your magazines. PeriodiCool moves and renames here, never deletes |
| Downloads | `/data` | `/mnt/user/downloads/` | Mount the **same host folder at the same container path** your download client uses (SABnzbd, qBittorrent and so on). `PERIODICOOL_DOWNLOADS_DIR` tells PeriodiCool where it is (`/data` in the template) |
| Import folder | `/import` | optional | Files dropped here are matched and filed into the library |
| PUID / PGID | | `99` / `100` | Unraid's `nobody` user and `users` group. PeriodiCool never runs as root |
| UMASK | | `002` | |
| TZ | | your time zone | For example `America/New_York` |
| PROWLARR_API_KEY, SABNZBD_API_KEY | | optional | Only a fallback for the first Prowlarr and SABnzbd entry; keys can be entered in Settings instead |

**Why the downloads folder matters.** The download client tells PeriodiCool where a finished download is, as the client sees it. If PeriodiCool sees the same folder under the same path, nothing more is needed. If the paths differ, add a remote path mapping under Settings, Download clients.

## First start

1. Open the WebUI and create the admin login.
2. Settings, Indexers: add Prowlarr, or a Newznab or Torznab indexer.
3. Settings, Download clients: add your client, with its own category (create the category in the client first). PeriodiCool only ever touches jobs in that category that it added itself.
4. Settings, Metadata: the lookups (Library of Congress, Wikidata, Internet Archive) are off by default. Turning them on improves matching; each sends only an ISSN or a title.
5. **Library already has magazines?** Run the first library scan as a preview (System, Tasks, or the Manual import page). Read what it proposes before applying anything.

## The API key

Settings, General shows it. Homepage, scripts and the calendar feed use it. See [api.md](api.md).

## Common problems

- **"Permission denied" creating folders.** A folder Docker created for a missing host path is owned by root. Create the host folder first, or fix it once with `chown -R 99:100 /mnt/user/media/magazines`.
- **"PeriodiCool cannot see the download".** The downloads mount does not match the download client's. Use the same host folder and container path, or add a remote path mapping.
- **Running commands inside the container.** Use `docker exec -it periodicool python -m periodicool ...`. The command line drops to PUID:PGID by itself, so files it creates are not owned by root.
- **Locked out.** `docker exec -it periodicool python -m periodicool user reset-password`.

## Submitting the template to Community Apps (maintainer)

The template lives in `unraid/` in this repository. Its icon is `unraid/periodicool.png` (a placeholder until the real icon is added under the same name). To list it, add the repository through the Community Apps submission form and open a support thread on the Unraid forum, then set the template's `Support` link to that thread.
