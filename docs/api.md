# PeriodiCool API

PeriodiCool has a REST API under `/api/v1` and sends webhook events. Both follow Sonarr's conventions where they fit, so tools built for Sonarr's API (dashboards such as Homepage or Organizr, scripts) need little change.

## Authentication

Every call needs the API key, shown under Settings, General and stored in `/config/api.key`.

Send it as a header:

```
curl -H "X-Api-Key: YOUR_KEY" http://periodicool:8383/api/v1/system/status
```

or in the query string, for tools that cannot set headers:

```
http://periodicool:8383/api/v1/system/status?apikey=YOUR_KEY
```

- A missing or wrong key gets `401` and `{"message": "API key missing or wrong"}`.
- The key opens the API only: it is not a login for the web pages.
- API calls need no login session and no CSRF token.
- Regenerating the key under Settings, General makes the old one stop working at once. The calendar feed's address changes too.

With a URL base (Settings, General), the API is under it, for example `/periodicool/api/v1/...`.

## Conventions

- **JSON:** names are camelCase.
- **Dates:** dates are `YYYY-MM-DD`; times are ISO 8601 in UTC.
- **Paged lists:** `{"page", "pageSize", "totalPages", "totalRecords", "records"}`. Pages hold 50 records; pass `?page=2` for the next.
- **Errors:** `{"message": "..."}`. A request whose fields fail the checks also carries `"errors": {"field": "what is wrong"}`.
- **Status codes:** `400` bad input, `401` key, `404` not found, `409` name taken.
- **What answers never contain:** indexer or download client keys and passwords, webhook secrets, download links, or absolute paths. Issue files are given relative to the library.
- **Schema:** the OpenAPI schema is at `/api/v1/openapi.json` (key needed). PeriodiCool serves no Swagger page, because those load scripts from a CDN.

## Endpoints

### System

| Request | Answer |
| - | - |
| `GET /api/v1/system/status` | `appName`, `version`, `startTime`, `urlBase`, `timeZone`, `jobsEnabled` |
| `GET /api/v1/health` | The checks System, Status shows: `[{"name", "level", "message", "fix"}]`. Level is `ok`, `info`, `warn` or `bad`. Nothing remote is contacted. |

### Magazines

| Request | Answer |
| - | - |
| `GET /api/v1/magazine` | Every magazine, by title |
| `GET /api/v1/magazine/{id}` | One magazine |
| `POST /api/v1/magazine` | Add one; answers `201` with it |
| `PUT /api/v1/magazine/{id}` | Change the fields you send; the rest stay |
| `DELETE /api/v1/magazine/{id}` | Remove it from PeriodiCool. Its files stay on disk |

**Fields:**
- `title`
- `aliases`, a list
- `region`, `languages`, `issn`
- `frequency`: `weekly`, `biweekly`, `monthly`, `thirteen_per_year`, `bimonthly`, `quarterly`, `seasonal` or `irregular`
- `numbering`: `date`, `issue`, `volume` or `season`
- `anchorDate`, `anchorNumber`
- `monitored`
- `monitorMode`: `new_only`, `from_year` or `all`; plus `monitorFromYear`
- `formatProfileId`, `graceDays`, `onSaleDays`, `scheduleStart`, `publishWeekday`
- `searchTerm`, `mustContain`, `mustNotContain`, `tags`

The checks are the Add form's own; for example, a numbered magazine needs `anchorNumber` and `anchorDate`. Answers also carry `id`, `added` and `statistics` (`issueCount`, `fileCount`, `missingCount`, `monitoredCount`).

```
curl -X POST -H "X-Api-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"title": "Linux Magazine", "frequency": "monthly", "numbering": "issue",
       "anchorNumber": 311, "anchorDate": "2026-10-01", "monitorMode": "new_only"}' \
  http://periodicool:8383/api/v1/magazine
```

Adding a magazine sends the `MagazineAdd` webhook event, as the web form does.

### Issues

| Request | Answer |
| - | - |
| `GET /api/v1/issue?magazineId=3` | The magazine's issues |
| `PUT /api/v1/issue/{id}` | Only `{"monitored": true}` or `{"monitored": false}` |

Each issue has these fields:
- `id`, `magazineId`, `key` and `label`;
- `coverDate`, `year`, `month`, `season`, `issueNumber`, `volumeNumber`, `special`;
- `monitored` and `state`: `imported`, `downloaded`, `grabbed`, `wanted`, `missing`, `failed`, `upcoming` or `unmonitored`;
- `file`: `path` (relative to the library), `format`, `size` and `imported`, or `null` when there is no file.

### Wanted, queue, history, calendar

| Request | Answer |
| - | - |
| `GET /api/v1/wanted/missing?page=1&magazineId=3` | Paged missing issues, as on the Wanted page, with `magazineTitle` |
| `GET /api/v1/queue` | Downloads in progress or waiting for review: `title`, `status`, `protocol`, `downloadClient`, `downloadId`, `progress`, `size`, `sizeLeft`, `timeLeft`, `error`, `grabbed` |
| `GET /api/v1/history?page=1&magazineId=3&eventType=imported` | Paged history. Event types: `grabbed`, `download_failed`, `download_removed`, `imported`, `import_failed`, `import_review`, `file_moved`, `file_linked`, `blocklisted` |
| `GET /api/v1/calendar?start=2026-10-01&end=2026-11-30` | Issues by cover date in that range (at most a year), with `onSale` when the magazine has an on-sale window. The default is from a week ago for five weeks |

### Commands

`POST /api/v1/command` with `{"name": "..."}` starts a command and answers `201`. Commands run one at a time, the same way as System, Tasks, Run now. If a scheduled task is running, the command waits for it to finish.

| Name | Does |
| - | - |
| `RssSync` | RSS sync |
| `MissingSearch` | Search for wanted issues |
| `MagazineSearch` | Search one magazine and grab what is wanted; needs `"magazineId"` |
| `RescanLibrary` | Library rescan |
| `ImportFolder` | Process the import folder |
| `CheckDownloads` | Check the download clients and import finished downloads |
| `Backup` | Make a backup |
| `HealthCheck` | Run the health checks (and send webhook health events) |

```
curl -X POST -H "X-Api-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"name": "MagazineSearch", "magazineId": 3}' http://periodicool:8383/api/v1/command
```

```json
{"id": 7, "name": "MagazineSearch", "magazineId": 3, "status": "queued", "message": "",
 "queued": "2026-10-23T18:04:22+00:00", "started": null, "ended": null}
```

`GET /api/v1/command/{id}` follows a command. Its `status` goes from `queued` to `started`, then to `completed` or `failed`; `message` says what happened. `GET /api/v1/command` lists recent commands, newest first. Only the last 50 are kept, and only until a restart.

Indexers, download clients and webhook connections are set up in the web UI only for now, since their keys and passwords are write-only.

## Webhooks

Settings, Connect, Add connection, Webhook: PeriodiCool posts events as JSON to an address you choose, like Sonarr's Webhook connection. Point it at Apprise, ntfy, Home Assistant, n8n, a chat bridge or your own script.

**Settings**

- URL, and method POST or PUT.
- Optional username and password, sent as basic authentication.
- Optional extra headers, under Show advanced; their values are kept out of the logs.

**Events**

- One switch per event. The `eventType` values are `Test`, `Grab`, `Download` (an import, as Sonarr names it), `DownloadFailure`, `ImportFailure`, `ManualInteractionRequired`, `MagazineAdd`, `MagazineDelete`, `Health` and `HealthRestored`.
- Health events come from a check every 5 minutes. Problems are always sent; warnings only when "Include health warnings" is on.

**Sending**

- An event is sent only after the change it reports is saved.
- Redirects are not followed.
- A failed send is logged and shown in the connection list. It never stops a grab or an import.

**Payload**

- Every event carries `eventType`, `instanceName`, `applicationUrl` (Settings, General, Application URL) and `date` (UTC).
- Depending on the event, it also carries:
  - `magazine`: `id`, `title`, `issn`, `frequency`;
  - `issue`: `key`, `coverDate`, `issueNumber`, `volumeNumber`, `special`;
  - `release`: `title`, `indexer`, `size`, `protocol`, `downloadClient`, `downloadId`;
  - `file`: `path` relative to the library, `format`, `size`;
  - `reason`;
  - `health`: `name`, `level`, `message`, `fix`.

An example `Grab`:

```json
{
  "eventType": "Grab",
  "instanceName": "PeriodiCool",
  "applicationUrl": "https://mags.example",
  "date": "2026-10-23T18:04:22+00:00",
  "magazine": {"id": 3, "title": "Linux Magazine", "issn": "1471-5678", "frequency": "monthly"},
  "issue": {"key": "n311", "coverDate": "2026-10-01", "issueNumber": 311, "volumeNumber": null, "special": false},
  "release": {"title": "Linux Magazine USA - Issue 311, October 2026", "indexer": "NZBgeek", "size": 52428800,
              "protocol": "usenet", "downloadClient": "SABnzbd", "downloadId": "SABnzbd_nzo_abc123"}
}
```

`python -m periodicool notify test ID` sends a `Test` event from the command line.
