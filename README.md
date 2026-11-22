<p align="center"><img src="heliograph/heliograph.svg" width="96" alt="Heliograph icon"></p>

<h1 align="center">Heliograph</h1>

<p align="center"><b>A self-hosted, one-way reading sync for <a href="https://hardcover.app">Hardcover</a> users.</b><br>
Hardcover is the source of truth, and your changes are pushed to Goodreads, StoryGraph, Fable, Pagebound and LibraryThing.</p>

> [!IMPORTANT]
> **Heliograph is an unofficial, independent hobby project.** It is **not affiliated with, endorsed by, sponsored by,
> or supported by** Hardcover, Goodreads (Amazon), The StoryGraph, Fable, Pagebound or LibraryThing.
> Please don't contact those services for help with it. See [Disclaimers](#disclaimers) before using it.

Heliograph is a port of the request logic in **[ShelfSync](https://github.com/Lyfts/ShelfSync)**, a KOReader plugin (MIT
licensed), into a standalone Docker container with a web UI. KOReader is not needed: if anything updates your Hardcover
library (the Hardcover app, a reading app that syncs to it, or the website), Heliograph pushes those changes to your
other services. All credit for working out how to talk to each service goes to the ShelfSync project.

> 📖 **The [wiki](https://github.com/ClarkLindsay/heliograph/wiki)** has step-by-step guides: installation, your first
> sync, setting up each service, fixing unmatched books, every setting, and troubleshooting.

## Contents
- [What gets synced](#what-gets-synced)
- [Disclaimers](#disclaimers)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Changing the web UI port](#changing-the-web-ui-port)
- [Getting credentials for each service](#getting-credentials-for-each-service)
- [Web UI](#web-ui)
- [Accounts and sign-in](#accounts-and-sign-in)
- [Protecting secrets](#protecting-secrets)
- [Command line](#command-line)
- [Config and storage](#config-and-storage)
- [Goodreads cookie refresher](#goodreads-cookie-refresher)
- [LibraryThing](#librarything)
- [Behaviour notes](#behaviour-notes)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Adding another service](#adding-another-service)
- [Development and tests](#development-and-tests)
- [Credits and license](#credits-and-license)

## What gets synced

| In Hardcover | Pushed to each service |
|---|---|
| Book added as Currently Reading | Currently-reading shelf |
| Want to Read | Want/Interested shelf (toggle) |
| Progress changes | Page (mapped to that service's edition page count) or %, throttled with step sizes. Audiobooks tracked by time are sent as % |
| Marked Read | Read shelf, finish date (Goodreads/Pagebound), then your rating |
| Rating changed later | Rating re-pushed |
| Paused / DNF | Off by default (Goodreads/StoryGraph/Pagebound take both, Fable DNF only) |

Ratings: Goodreads quarter stars, StoryGraph quarter stars (review form, other review fields untouched), Fable as-is,
Pagebound and LibraryThing half stars (floored), the same as ShelfSync.

Sync is **one way only**. Heliograph reads Hardcover through its official API and never writes to it while syncing.
The exceptions are the optional data corrections on the **Hardcover** tab, which only write when you press **Apply**:
**Reading dates from Goodreads** fills in start dates (and other date fixes you pick), and **Hardcover editions**
changes a book's edition, for example from a translation you never read to the English edition.

By default the first run is a **baseline**: it records your current library and pushes nothing, so only *later* changes
are sent. That's the safe choice if you've already imported your history into each service. Use the **Import** tab
(or `initial_sync: import_all`) to send your whole library.

## Disclaimers

**Please read this before using Heliograph.** Use it at your own risk.

### Unofficial project, trademarks
- Heliograph is not an official product of Hardcover or of any other service it talks to, and none of them have
  reviewed or approved it.
- "Hardcover", "Goodreads", "The StoryGraph", "Fable", "Pagebound" and "LibraryThing" are trademarks of their
  respective owners. They are used here only to say which services Heliograph works with.
- This repository does **not** include any of their logos. The app ships with its own original icon and plain
  monogram icons for each service. If you add real logos to your own install (see [Icons](#web-ui)), they stay on
  your machine.

### Official vs. unofficial APIs

| Service | How Heliograph talks to it |
|---|---|
| Hardcover | **Official** public GraphQL API with your personal API token. Read-only, except the edition cleanup you apply by hand |
| Fable | **Official** API, signed in with your email and password |
| Pagebound | The **same API its own website uses**, signed in with your email and password |
| Goodreads | **Unofficial**: replays your browser session cookie and scrapes or posts to website pages |
| StoryGraph | **Unofficial**: replays your browser session cookies and scrapes or posts to website pages |
| LibraryThing | **Unofficial**: app sign-in endpoint plus website pages |

- **Any integration may break** whenever a service changes its website or API. The unofficial ones (Goodreads,
  StoryGraph, LibraryThing) are the most fragile.
- Automated access through unofficial means may go against a service's terms of use. **You are responsible for
  making sure your use complies with each service's terms.** Heliograph only acts on your own accounts, one request at
  a time, at a modest pace (default: one check every 5 minutes).

### Service-specific limitations (inherited from ShelfSync)
- **Goodreads**
  - Goodreads doesn't expose your current page in a way that can be read back reliably, so progress is always pushed
    forward. Heliograph can't tell when Goodreads is ahead.
  - Paused and Did Not Finish can be set but aren't reliably read back.
  - Session cookies can go stale when Goodreads shows an AWS WAF bot challenge. Sync to Goodreads pauses until the
    cookie is refreshed, by hand or by the [cookie refresher](#goodreads-cookie-refresher).
- **LibraryThing**
  - It doesn't track reading progress, so only status, dates and rating are synced. Paused and DNF aren't sent.
  - Cloudflare can serve a browser check that Heliograph can't complete. Sync fails until it clears.
  - The sign-in method is unofficial and may change. **Your password is sent in the sign-in URL** (over HTTPS), and
    URLs like that may be recorded in server logs. Pasting session cookies instead avoids sending the password.
- **Fable and Pagebound:** accounts created with Google or Apple sign-in need an email and password set up on that
  service before Heliograph can log in.
- **StoryGraph** uses session tokens, not your password.

### Credentials
- Session cookies, tokens and passwords are **credentials**. Heliograph sends each one only to the service it
  belongs to. They are stored in the container's data volume (`config.json`), **in plain text unless you set up
  [encryption](#protecting-secrets)**.
- Fable, Pagebound and LibraryThing keep your password so they can sign in again when a session expires.
- The web UI can make changes in your accounts. By default it only listens on `127.0.0.1`. If you expose it on your
  network, set a password, and don't expose it to the internet: it uses plain HTTP.

### No warranty
This software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It has unit tests with mocked
HTTP, but services change without notice. Start with a **preview / dry run**, and keep your own exports or backups of
your reading data.

## Requirements
- **Docker** with Docker Compose v2: Docker Desktop on Windows (WSL2) or macOS, or Docker Engine on Linux.
- A **Hardcover** account and API token.
- An account on at least one target service.
- About 100 MB of disk space. Heliograph is a single small Python 3.12 container.

## Quick start

1. **Get the compose file.** Heliograph ships as a prebuilt image,
   `ghcr.io/clarklindsay/heliograph`, for linux/amd64 and linux/arm64 (Raspberry Pi, Apple Silicon, most NAS boxes).
   You only need `docker-compose.yml`. Pick one of these:
   - **Download it**: make an empty folder and save `docker-compose.yml` (and optionally `env.example`, saved as
     `.env`) into it from the [latest release](https://github.com/ClarkLindsay/heliograph/releases/latest).
   - **Or clone the repo** (you need this to build from source; see [Building from source](#building-from-source)):
     ```sh
     git clone https://github.com/ClarkLindsay/heliograph.git
     cd heliograph
     ```

   To try the image without compose, run `docker pull ghcr.io/clarklindsay/heliograph:latest`.

2. **(Optional, recommended) turn on encryption** so the passwords and cookies you save are encrypted at rest. See
   [Protecting secrets](#protecting-secrets). It's easiest to do this before the first start.

3. **(Optional) pick a different port** if 8080 is taken. See [Changing the web UI port](#changing-the-web-ui-port).

4. **Set your time zone** in `docker-compose.yml` (`TZ: Etc/UTC`, e.g. `America/New_York`). Finish dates are
   calculated in this time zone.

5. **Start it**
   ```sh
   docker compose up -d
   docker compose logs -f heliograph
   ```
   This pulls the newest release. To pin a version, set `HELIOGRAPH_VERSION=0.1.0` in `.env`. Use `edge` for the
   newest `main` branch build.

6. **Open the web UI** at <http://localhost:8080> (or your chosen port) and **create your account**. The first account
   is the admin ([Accounts and sign-in](#accounts-and-sign-in)). Then go to **Settings**, paste your
   **Hardcover API token**, enable the services you use and fill in their credentials
   ([how to get them](#getting-credentials-for-each-service)). Use **Test** next to each service on the Dashboard.

   You can set everything as `HELIOGRAPH__<section>__<key>` environment variables in `docker-compose.yml` instead. Those
   win over the web UI settings and show as locked there.

7. **Check everything works**
   ```sh
   docker compose run --rm heliograph python -m heliograph --check
   ```
   This tests the Hardcover token and every enabled service.

8. **Preview before anything is sent.** This logs exactly what would be pushed and changes nothing:
   ```sh
   docker compose run --rm -e HELIOGRAPH__common__initial_sync=import_all heliograph python -m heliograph --once --dry-run
   ```
   Or use the **Import** tab with **Preview only** ticked.

9. **(Recommended)** Set a web UI password if you'll expose the UI beyond this computer, and see
   [Protecting secrets](#protecting-secrets) to keep plain-text secrets out of the compose file.

From then on it runs by itself, checking Hardcover every 5 minutes by default. It restarts with Docker
(`restart: unless-stopped`).

## Changing the web UI port

The web UI listens on port **8080** inside the container and is published on **8080** on your computer by default.
If something else already uses 8080, `docker compose up` fails with an error like *"Bind for 127.0.0.1:8080 failed:
port is already allocated"* or *"Only one usage of each socket address…"*.

**To fix it, change only the host port.** The container side stays at 8080.

**Option A: a `.env` file (recommended).** Your repo copy stays unmodified, so `git pull` works cleanly.

1. Copy the example file:
   ```sh
   cp .env.example .env            # PowerShell: Copy-Item .env.example .env
   ```
2. Edit `.env` and set a free port, e.g.:
   ```
   HELIOGRAPH_PORT=8090
   ```
3. Apply it:
   ```sh
   docker compose up -d
   ```
4. Open <http://localhost:8090>.

**Option B: edit `docker-compose.yml` directly.** Change the first number in the `ports:` line:
```yaml
    ports:
      - "127.0.0.1:8090:8080"     # host 8090 -> container 8080
```

**Finding a free port:**
- Windows (PowerShell): `Get-NetTCPConnection -LocalPort 8080 -State Listen` shows what holds 8080. Pick a port that
  returns nothing, e.g. 8090, 8181 or 18080.
- macOS / Linux: `lsof -i :8080` or `ss -ltnp | grep 8080`.

> Don't change `HELIOGRAPH__web__port`. That's the port the app listens on *inside* the container, and the compose file
> maps to 8080. If you do change it, change the right-hand side of the `ports:` mapping to match.

## Getting credentials for each service

All of these can be entered on the web UI's **Settings** page. The matching environment variable is shown in brackets.

| Service | What you need |
|---|---|
| **Hardcover** (required) | Your API token from <https://hardcover.app/account/api>. Paste it without the `Bearer ` prefix if one is shown. [`HELIOGRAPH__hardcover__token`] |
| **Goodreads** | Either a **cookie refresher** URL (recommended, see [below](#goodreads-cookie-refresher)) or your full browser **Cookie header**: log in at goodreads.com, open Developer Tools → **Network**, reload, click a `www.goodreads.com` request, and copy the whole `Cookie` value under *Request Headers*. [`HELIOGRAPH__goodreads__cookie`] |
| **StoryGraph** | Two cookies: log in at thestorygraph.com, open Developer Tools → **Application** (Chrome/Edge) or **Storage** (Firefox) → **Cookies**, and copy `_storygraph_session` [`HELIOGRAPH__storygraph__session_cookie`] and `remember_user_token` [`HELIOGRAPH__storygraph__remember_user_token`]. |
| **Fable** | Account email and password. [`HELIOGRAPH__fable__email`, `HELIOGRAPH__fable__password`] |
| **Pagebound** | Account email and password. [`HELIOGRAPH__pagebound__email`, `HELIOGRAPH__pagebound__password`] |
| **LibraryThing** | Username and password, **or** paste the session cookies `cookie_userid`, `cookie_usernum` and `cookie_userchecksum`. [`HELIOGRAPH__librarything__username`, `…__password`, `…__session_cookie`] |

Every service starts **off**. Nothing is sent anywhere until you turn a service on (a checkbox in Settings, or
`HELIOGRAPH__<service>__enabled: "true"`).

## Web UI

Open **http://localhost:8080** (or [your port](#changing-the-web-ui-port)).

- **Dashboard:** last cycle result, next run, **Sync now**, a login test for each service, and linked / no-match /
  error counts, plus the recent log.
- **Books:** every Hardcover book with its status, progress and a chip per service (linked, recorded, no match, error,
  skipped). Click a book to fix a service:
  - **Find edition…** does a live search on that service, or you can paste its URL or id.
  - **Use this** links that edition.
  - **Push current status now**: books recorded at the first run are assumed to be in sync, so push after linking.
  - **Unlink** and **Skip**.
- **Push to all services** (top of a book): sends that book's current status, progress and rating to every enabled service.
- **Cover previews:** the edition picker shows each candidate's cover next to your Hardcover cover.
- **Import** tab: re-sends every Hardcover book (filter by status and service) in the background, one book at a
  time, with progress and Cancel.
  - **Skip books that are already up to date** (on by default) reads what each service already holds and only writes
    what differs. A book already on the right shelf with the same rating and finish date is skipped, a missing one is
    written, and a wrong rating only gets the rating. What can be compared depends on the service:
    - shelf on all services;
    - rating on StoryGraph, Fable and Goodreads (when its page shows it);
    - finish date on Goodreads, Pagebound and LibraryThing.

    Anything a service doesn't let Heliograph read back is assumed correct when the book is already on the right shelf.
    Reading books on the right shelf are skipped too, because progress follows from later updates.
  - **Preview only:** tick it first. It does the searching and matching, then reports what would be sent and what has
    no match, without changing anything.
  - **Log:** *Download log (.txt)* has everything the run logged (also saved in the data volume under `imports/`).
    *Download results (.csv)* has one row per book and service (updated / would update / skipped / unchanged /
    no match / error).
  - **Automatic retries:**
    - A failed book is retried twice, with a pause. The pause is longer for login, Goodreads WAF or refresher problems.
    - Five login failures in a row pause that service.
    - At the end of the run, failed items get up to two more rounds, each only after the service passes a login check.
    - Anything that still fails is listed with its error. **Retry failed** re-runs just those.
  - **Unmatched books:** books with no confident match on a service (even in a preview) are listed here and flagged ⚑
    in the Books tab (filter: *Flagged: no match*). Click one, then **Find edition…** and **Use this**. It drops off
    the list once linked.
- **Hardcover** tab (0.5.0+): corrections to your Hardcover data. These are the only things in Heliograph that write
  to Hardcover, and only when you press **Apply**.
  - **Reading dates from Goodreads:** for people who came to Hardcover with a Goodreads import, which brings finish
    dates but no start dates. **Scan Goodreads** reads your Goodreads *My Books* list (one page per 100 books) and
    your Hardcover reads, matches the books (the linked edition, then ISBN, then title and author) and pairs up each
    reading session by its finish date. It then lists what differs:
    - **Start date:** Goodreads has a start date that Hardcover is missing (pre-selected) or has differently (not
      selected). Same-day start and finish dates are not selected either: they're often placeholders.
    - **Change dates:** one read on each side with different finish dates (not selected).
    - **Add a read:** a Goodreads read Hardcover doesn't have. Pre-selected only when Hardcover has no reads of the book.

    Tick what you want and press **Apply**. Each read is checked again just before it's written, and skipped if
    Hardcover's dates changed since the scan. Dates Goodreads only has as a month or year are left out.
  - **Hardcover editions:** finds books whose Hardcover edition isn't in your preferred language and switches the ones
    you pick (moved here from the Import tab).
- **Settings:** everything from the config, including the poll interval. Secrets are never shown: leave a field blank
  to keep its value, and **Clear** removes it. Settings set through `HELIOGRAPH__...` environment variables are shown
  locked. Changes apply on the next cycle.
- **Icons:** Heliograph ships with its own icon and plain monogram icons for each service. Clicking a service icon
  opens **your profile** on that service. Heliograph looks the profile up from the session it already has, about once a
  week, and also right after **Test**. Until it's known, the icon opens the service's homepage. To send it somewhere
  else, set **Dashboard icon link** in that service's settings; your own link always wins.
  - **Service logos (optional, local only):** put files named like `goodreads.png` (`.svg`, `.png`, `.webp` or `.jpg`)
    in the `icons/` folder next to `docker-compose.yml`. The keys are `hardcover`, `goodreads`, `storygraph`, `fable`,
    `pagebound` and `librarything`.
  - **Your own app icon / favicon:** add `icons/heliograph.png` to replace the built-in Heliograph icon.

  The `icons/` folder is gitignored, so logos you add are never committed.

## Accounts and sign-in

One Heliograph can sync several people. Each **account** has its own:
- Hardcover token, services and credentials;
- settings and library;
- links, logs and imports;
- sync loop.

Nobody sees anyone else's.

- **The first account** becomes the **admin**. Create it from the machine running Heliograph: open
  <http://localhost:8080>, or another name listed in `HELIOGRAPH__web__allowed_hosts`. That way nobody who finds your
  address first can claim the install. If you set a web password before 0.4 (`HELIOGRAPH__web__password`), you can
  instead enter it as the **setup code** from anywhere.
- **More people:** as admin, open **Users → Create invite code** and give the code to the person you want to let in.
  They register on the sign-in page with it. Each code works once and expires after 7 days.
- **Users tab (admin):** lists accounts, deletes them (their data folder is kept aside on the server, not destroyed)
  and revokes unused codes. Everyone can change their own password under **Account**, which signs out their other
  devices.
- **Upgrading from 0.3 or earlier:** your existing settings, library and links are kept and handed to the first account
  you create. A copy of the old files stays in the data volume under `backup-before-0.4/`.
- **Environment variables** (`HELIOGRAPH__…`) apply to the admin's settings only. Other users set theirs in the web UI.

**Security:** the UI can push changes to your accounts, so by default it's only reachable from this computer. To use
it from other devices, remove `127.0.0.1:` from the `ports:` line. It's plain HTTP, so use it on your LAN only, or put
it behind a reverse proxy or tunnel with TLS (Cloudflare Tunnel, Caddy, Traefik).

How sign-in is protected:
- Sign-ins use a session cookie (`HttpOnly`, `SameSite=Lax`, and `Secure` behind HTTPS) that lasts 30 days.
- Repeated wrong passwords for an account are slowed down.
- Passwords are stored as salted PBKDF2 hashes. Session tokens and invite codes are stored only as hashes.

All users' stored secrets are encrypted with the server's key, if you set one up ([Protecting secrets](#protecting-secrets)).
Whoever runs the server can therefore read them, as with any self-hosted app.

## Protecting secrets

Cookies, passwords and tokens can't be *hashed*, because the app has to send the real value to each service. They can be
**encrypted**, with a key that lives outside the compose file and the data volume. Without a key they are stored as
plain text in `config.json` (including anything you save in the web UI). Values written directly in the compose file
are always plain text.

1. **Create the key.** This one-liner makes a random key without starting the app.

   PowerShell:
   ```powershell
   mkdir secrets
   docker run --rm python:3.12-slim python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" | Out-File -Encoding ascii -NoNewline secrets\heliograph.key
   ```
   macOS / Linux:
   ```sh
   mkdir -p secrets
   docker run --rm python:3.12-slim python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" > secrets/heliograph.key
   ```
   Check that `secrets/heliograph.key` is a **file** containing one line of about 44 characters.
2. **Turn on the secret.** In `docker-compose.yml`, uncomment both `secrets:` blocks (the one under the service and the
   top-level one), or add them to your `docker-compose.override.yml` (see
   [below](#keeping-personal-settings-out-of-git)). Then run `docker compose up -d`.
   **Create the key file first:** if it's missing, Docker creates a folder with that name instead, and the app won't
   start.
   - Existing secrets in `config.json` are encrypted on startup, and the log says so.
   - Anything saved from the web UI is stored encrypted from then on.
   - The Settings page shows whether encryption is on.
3. **Keep plain secrets out of compose.** Encrypt each value and paste the token:
   ```sh
   docker compose run --rm heliograph python -m heliograph --encrypt
   ```
   The input is hidden, and the command prints `enc:v1:...`. Use that as the value of, for example,
   `HELIOGRAPH__fable__password`. Or point at a file: `HELIOGRAPH__fable__password_FILE: /run/secrets/fable_password`.
4. **Web UI passwords** are per account now ([Accounts and sign-in](#accounts-and-sign-in)); nothing to set here.

**Keep a backup of `secrets/heliograph.key`.** Without it, encrypted values can't be read and must be entered again.
Encryption protects against a leaked compose file, `config.json` or volume backup. It doesn't protect against someone who
can run commands inside the container, because they can read the key.

### Keeping personal settings out of git
The repo's `.gitignore` already excludes `.env`, `docker-compose.override.yml`, `secrets/`, `*.key` and your `icons/`.
To keep personal values (your time zone, web password hash, the secrets block) out of the tracked
`docker-compose.yml`, put them in a **`docker-compose.override.yml`**. Docker Compose merges it automatically:

```yaml
# docker-compose.override.yml  (gitignored)
services:
  heliograph:
    secrets:
      - heliograph_key                            # encryption on (see Protecting secrets)
    environment:
      TZ: America/New_York
      HELIOGRAPH__web__password: "pbkdf2:..."     # optional: setup code for creating the first account remotely
secrets:
  heliograph_key:
    file: ./secrets/heliograph.key
```

## Command line

Run any of these with `docker compose run --rm heliograph python -m heliograph <flag>`:

| Flag | What it does |
|---|---|
| *(none)* | Run the poll loop and web UI (the container's default) |
| `--check` | Test the Hardcover token and every enabled service, then exit |
| `--once` | Run one sync cycle and exit |
| `--dry-run` | With `--once`: log what would be pushed, change nothing |
| `--inspect` | Show raw Hardcover progress data for your Currently Reading books |
| `--init` | Write a default `config.json` and exit |
| `--gen-key` | Print a new encryption key |
| `--encrypt` | Prompt for a value (hidden) and print an `enc:v1:...` token |
| `--hash-password` | Prompt for a password (hidden) and print its hash, for `HELIOGRAPH__web__password` (the setup code) |
| `--user NAME` | With `--check`, `--once` or `--inspect`: which account (default: the admin) |
| `--reset-password NAME` | Set a new web UI password for an account (prompted, hidden); restart afterwards |
| `--healthcheck` | Exit 0 if the loop has completed a cycle recently (used by Docker's `HEALTHCHECK`) |

## Config and storage

Everything is in the `/data` named volume: `config.json`, `state.db` (SQLite), `heliograph.log` (rotated) and `imports/`.

- Use a **named volume**, not a `C:\` bind mount. SQLite locking and file permissions are unreliable on Windows paths.
- **Environment variables:** any setting can be set as `HELIOGRAPH__<section>__<key>`. It wins over the file and is never
  written back. Add `_FILE` to read the value from a file, e.g. `HELIOGRAPH__fable__password_FILE`.
- Refreshed cookies and tokens are written to `config.json`.
- Secrets in environment variables show up in `docker inspect`. If that matters, put them in `config.json` (via the
  web UI) with encryption on instead.
- The poll interval is `HELIOGRAPH__common__poll_interval_minutes` (minimum 1), or set it in Settings.

**Settings mirrored from ShelfSync:**
- link by ISBN, and by title + author;
- progress mode (pages or percent);
- minimum update interval;
- progress step (% and pages);
- first-run behaviour: `baseline` records your library and pushes nothing, `import_all` pushes it.

Each service gets its own baseline the first time it's enabled.

## Goodreads cookie refresher

Goodreads cookies expire and get hit by WAF challenges. ShelfSync's companion project
**[goodreads-cookie-refresher](https://github.com/Lyfts/goodreads-cookie-refresher)** keeps a logged-in browser
session alive in its own container and hands out fresh cookies. Heliograph uses it the same way ShelfSync does.

The refresher is its own compose project and publishes `REFRESHER_PORT` (default 5080) on the host. From this
container, use:
```yaml
HELIOGRAPH__goodreads__cookie_refresh_url: "http://host.docker.internal:5080"   # base URL only
HELIOGRAPH__goodreads__cookie_refresh_token: "..."                              # REFRESHER_AUTH_TOKEN, if you set one
```

How it's used:
- If the cookie is empty, Heliograph calls `GET /cookie`.
- If it hits a WAF challenge or a signed-out page, it calls `POST /refresh` (75 s timeout), saves the new cookie to
  `config.json`, and retries once. The refresher is tried up to 3 times, 10 s apart.

Alternatively, join the refresher's compose network and use its service name. Check which port it listens on inside
its container.

**Several people (multi-account refresher).** The refresher can keep several Goodreads logins: set
`ACCOUNTS=default,sam,alex` in its `.env`, then log each one in to Goodreads in its own Chrome window on the noVNC page.
The window titles show `[ShelfSync: <name>]`; switch with Alt+Tab.
- **Your own refresher:** set **Cookie refresher account** (Settings → Goodreads) to your name from that list. Leave it
  blank for `default`. Heliograph then uses `/accounts/<name>/cookie` and `/accounts/<name>/refresh`.
- **Sharing the admin's refresher:** users with no refresher of their own get cookies from the admin's. The admin gives
  each of them their account name in **Users → Refresher account**. Each user gets a different one, never the admin's.
  Users can't pick it themselves: the refresher gives any account to anyone with its token, so letting users choose
  would let one read another's Goodreads session. The admin's token is never shown to them.
- **Test** next to Goodreads shows whether that refresher account is logged in.

## LibraryThing

LibraryThing has no reading status or progress: a "status" is which collection a copy is in, plus its reading dates.

| Hardcover | LibraryThing |
|---|---|
| Want to Read | "To read" collection |
| Currently Reading | "Currently reading" collection + start date |
| Read | Your read collection + finish date (Hardcover's) |
| Rating | Half stars (floored) |

- The read collection defaults to "Your library" (`read_collection_id: 0`). Use `5` for "Read but unowned".
- Paused/DNF and progress aren't sent.
- Books not yet in your library are added.
- Sign in with `username` + `password` (renewed automatically), or paste the three session cookies.
- If Cloudflare challenges the requests, you'll see an error. Retry later, or set `user_agent`.

## Behaviour notes
- **Matching:**
  - **Smart matching (0.6.0+)** decides whether a search result is your book. It runs entirely on your server: no AI
    service, no download, nothing sent anywhere.
    - It understands volume numbers (*Vol. 2*, *Volume II*, *Book Two*, *#2*), series tags such as *(Mistborn, #2)*,
      subtitles and edition notes, so *"White Sand, Vol. 1"* matches *"White Sand, Volume 1"* but never *Volume 2*.
    - It never links a box set, omnibus, study guide, summary or dramatised adaptation to a single book, or a book by
      someone else.
    - When the first search finds nothing usable it tries again (main title + surname, then the title alone; one
      extra search on Goodreads).
    - A service title that only adds a subtitle (*"Green"* vs *"Green: The Beginning and the End"*) links on its own
      when the ISBN or page count agrees; otherwise it becomes a suggestion.
    - Unconfident matches are never guessed. The best near miss is shown as a **suggestion** with how sure it is and
      why, on the book and in the Import tab's *Not matched* list, with a one-click **Use this**. Matching is retried
      every 6 hours.
    - **Find edition…** shows its verdict on every result. The editions you pick there teach it what you prefer: a
      small model (a handful of weights, kept close to its defaults) is stored per account. **Settings → Smart
      matching** shows what it has learned and can reset it.
  - **Editions:** StoryGraph, Goodreads and Fable say which language and format each edition is. Heliograph only links
    editions in your **Preferred edition language** (default English; blank = any). The format follows your Hardcover
    edition: for a book you **listened** to (an audiobook edition in Hardcover), an audiobook edition is preferred, with
    print as the fallback when there isn't one. For books you read, audiobook editions are avoided, unless you turn
    off **Avoid audiobook editions**. On StoryGraph it follows to the edition you already have a status on if that one
    is acceptable. If your entry is on an audiobook or another-language edition, it links the right
    edition of the same work and moves your entry there with StoryGraph's own "switch to this edition", keeping your
    dates, rating and review. That never happens in a preview.
  - **Fixing existing links:** the Import tab's **Linked editions** card checks every linked book and lists the ones on
    the wrong edition. **Fix them** relinks those books: on StoryGraph your entry is moved, and elsewhere the current
    status is sent to the new edition. The old entry stays on Goodreads or Fable for you to remove.
  - Pagebound has no ISBN lookup, and Pagebound and LibraryThing don't say which edition is which.
- **Retries:** if any push fails, the Hardcover change window is held open so it's retried next cycle (for up to 7 days).
- **Removing books:** taking a book out of your Hardcover library does nothing on your services unless you set
  **When you remove a book from Hardcover, also remove it from your services**:
  - `want` removes Want to Read books;
  - `all` removes any status, which also deletes your rating and review there.

  Removed books are spotted within about 30 minutes, on StoryGraph, Goodreads, Fable and Pagebound.

  Safety:
  - An entry another book in your library still uses is never removed.
  - More than 5 books gone at once are held for you to confirm on the Import tab.
  - An empty library from Hardcover is never treated as "everything removed".
- **Progress:** audiobooks you track by time in Hardcover are sent as a percentage (0.4.2+); everything else follows
  the progress mode setting.
- **Fragility:** Goodreads, StoryGraph and LibraryThing are unofficial and can break when the sites change.

## Updating
```sh
docker compose pull
docker compose up -d
```
If you pinned `HELIOGRAPH_VERSION` in `.env`, change it to the new version first. To be told about new versions, go to
the GitHub repo and choose **Watch → Custom → Releases**.

Building from source? Use `git pull` and then `docker compose up -d --build`.

Your data, settings and links are in the `heliograph-data` volume and survive updates and rebuilds.

### Building from source
Clone the repo and add `build: .` under `services: heliograph:`, either in `docker-compose.yml` or in a gitignored
`docker-compose.override.yml` ([see above](#keeping-personal-settings-out-of-git)). Then run `docker compose up -d --build`.
Compose builds the image locally and gives it the same name, so nothing is pulled.

> The compose file pins the project name (`name: heliograph`), so the volume is always `heliograph_heliograph-data`,
> whatever the folder is called. You can move or rename the folder freely.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `port is already allocated` / `Only one usage of each socket address` | [Change the web UI port](#changing-the-web-ui-port). |
| Can't create the first account ("open Heliograph at localhost…") | Open it at `http://localhost:<port>` on the machine running it, enter your old web password as the setup code, or add your host name to `HELIOGRAPH__web__allowed_hosts`. |
| Someone needs an account | As admin: **Users → Create invite code**, and give them the code. |
| Forgot your password | On the server: `docker compose exec -it heliograph python -m heliograph --reset-password NAME`, then `docker compose restart`. |
| Goodreads: WAF / signed-out errors | Refresh the cookie, or set up the [cookie refresher](#goodreads-cookie-refresher). |
| Goodreads: frequent WAF challenges | Raise **Seconds between Goodreads requests** in Settings (default 3; try 5–10 for big imports). After a challenge the refresher can't clear, Heliograph leaves Goodreads alone for **Pause after a WAF challenge** minutes (default 15) instead of retrying straight away. Other services keep syncing in the meantime. |
| StoryGraph: auth errors | Copy fresh `_storygraph_session` and `remember_user_token` cookies. |
| LibraryThing: Cloudflare errors | Wait and retry, or set `user_agent`. |
| `Is a directory: '/run/secrets/heliograph_key'` | Docker created a **folder** because `secrets/heliograph.key` didn't exist. Delete the `secrets/heliograph.key` folder, create the key file ([Protecting secrets](#protecting-secrets) step 1), then `docker compose up -d`. |
| "Can't decrypt" errors after moving | Your `secrets/heliograph.key` is missing or different. Restore it, or re-enter the secrets. |
| Book shows ⚑ no match | Open it in the Books tab, **Find edition…**, **Use this**, then **Push current status now**. |
| Want to see what's happening | `docker compose logs -f heliograph`, or turn on `verbose_logging` in Settings. |

## Adding another service
1. Copy `heliograph/providers/librarything.py` (or the closest existing provider) and subclass `Provider`
   (`providers/base.py`).
   - Implement `check_auth`, `search` and/or `lookup_isbn`, `set_status`, `set_progress` and `set_rating`. Set
     `tracks_progress = False` to skip progress.
   - List the statuses you accept in `SUPPORTED`.
   - `find()` already tries ISBN first, then ShelfSync-style scored title matching.
2. Add its defaults (including `enabled: False`) to `config.DEFAULTS`, the class to `providers/__init__.py` (`ALL`), and
   its settings form to `SPEC` in `web.py`.
3. Providers get:
   - `self.cfg`: their config section, saved with `self.config.save()`;
   - `self.log`;
   - `self.state`: a small key-value store (`.get(key)` / `.set(key, value)`) for anything they need to remember.

   Environment variables `HELIOGRAPH__<section>__<setting>` work for the new section automatically.
4. Add a mocked-HTTP test like `tests/test_librarything.py`.

Pull requests welcome!

## Development and tests

Tests use mocked HTTP, so no accounts are needed. Python **3.12** is required. The easiest way to run them is in the
same image the app uses:
```sh
docker run --rm -v "$PWD:/src:ro" python:3.12-slim sh -c \
  'cp -r /src /w && cd /w && pip install -q -r requirements.txt && for t in tests/test_*.py; do python "$t" || exit 1; done && echo ALL OK'
```
Or locally on Linux or macOS with Python 3.12:
```sh
pip install -r requirements.txt
python tests/test_engine.py && python tests/test_providers.py && python tests/test_librarything.py \
 && python tests/test_web.py && python tests/test_crypto.py && python tests/test_import_skip.py && python tests/test_import_retry.py
```

The [Build and publish](.github/workflows/docker.yml) GitHub Action runs these tests on every push and pull request.

### Releasing
Images are built by GitHub Actions and published to `ghcr.io/clarklindsay/heliograph`:

| Event | Image tags |
|---|---|
| Push to `main` | `edge`, `sha-<commit>` |
| Push a tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `X` (from 1.0 on), `latest`. Also creates a GitHub Release with `docker-compose.yml` and `env.example` attached |
| Pull request | Tests and a test build only, nothing is published |

To cut a release:
1. Set `__version__` in `heliograph/__init__.py` (the workflow fails if it doesn't match the tag).
2. Commit and push.
3. Run `git tag v0.2.0` and then `git push origin v0.2.0`.

**Stack:**
- Python 3.12 standard-library HTTP server with a JSON API, and a single-file HTML/JS UI (`heliograph/web_index.html`).
- SQLite for state.
- Dependencies: `requests`, `beautifulsoup4` and `cryptography`.

## Credits and license

- **[ShelfSync](https://github.com/Lyfts/ShelfSync)**: the original KOReader plugin. Heliograph ports its request
  logic for every service, its title/author matching, and its settings model. ShelfSync is MIT licensed,
  © 2024 Billiam, and its notice is kept in [LICENSE](LICENSE) as that license requires.
- **[goodreads-cookie-refresher](https://github.com/Lyfts/goodreads-cookie-refresher)**: the optional companion
  service for Goodreads cookies.
- Book data comes from your own accounts. Cover images are loaded directly from the services in your browser and are
  not stored or redistributed by this project.

Heliograph is released under the **MIT License**, © 2026 Clark Lindsay. See [LICENSE](LICENSE).

*Heliograph is an independent, free, open-source hobby project with no commercial purpose. It is not affiliated with or
endorsed by Hardcover or any other service named here.*
