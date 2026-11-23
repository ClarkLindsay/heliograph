# Changelog

What changed in each release of Heliograph. Each release's notes on GitHub are taken from its section here.

## Unreleased

## 0.6.2 (2026-11-23)

### Fixed
- The log no longer says "Some updates failed; keeping the Hardcover window open" after every sync when review
  syncing is off. Nothing had failed and nothing was held back; the warning was wrong (since 0.6.0).

## 0.6.1 (2026-11-22)

### Changed
- A new **Tools** tab holds the occasional jobs, out of the way of everyday syncing:
  - **Hardcover cleanup** (was the Hardcover tab);
  - **Linked editions** (was on the Import tab);
  - **BookOrbit** (was the BookOrbit tab).
- **Settings:** BookOrbit is the last section, folded away until you set it up.
- README: the core sync comes first; the optional tools are gathered in one section.

## 0.6.0 (2026-11-22)

### Added
- **Review syncing** (off by default): your Hardcover reviews go to Goodreads, StoryGraph, Fable and Pagebound,
  spoiler flag included. A different review you wrote on a service yourself is kept unless you choose otherwise.
- **Smart matching:** better at telling the right book from box sets, other volumes and other editions, and it learns
  from the links you fix by hand.
- **BookOrbit** (optional):
  - Gets every BookOrbit book onto the right Hardcover book and edition, with suggestions, Hardcover search and
    pasted links.
  - Copies your Hardcover star ratings and reviews into BookOrbit.
  - Switches an unread audiobook to the ebook edition when you add the ebook.
- **Books tab:** a link to each book on Hardcover.
- **Fable:** link a book by hand with its link or id.

### Fixed
- **Hardcover's daily request limit:** Heliograph stops and says when the limit resets, instead of retrying for hours.
- **Goodreads:**
  - A finished read is sent with its real start date.
  - Shelving a book no longer adds an extra read.
- **Check editions:** books recorded at the first run are linked first.

## 0.5.0 (2026-11-22)

### Added
- **Hardcover tab:** fills in reading dates that a Goodreads import left out of Hardcover, only when you press Apply.

### Fixed
- **Finish dates:**
  - The latest finished read is used.
  - A finish date is never made up.
- **Fable and Pagebound:** ratings you'd already given can be changed.
- **Fable:**
  - String ratings are read correctly.
  - A removal is given a few seconds to show.

## 0.4.2 (2026-11-22)

### Changed
- Audiobook progress is synced as a percentage.

## 0.4.1 (2026-11-22)

### Added
- **Dashboard:** the service icons open your profile on each service.
- **Settings:** the Hardcover icon links to your @username page straight away.

### Fixed
- `--reset-password` keeps the account name.

## 0.4.0 (2026-11-22)

### Added
- **Multiple users:**
  - Accounts, a sign-in page and invite codes.
  - The Goodreads cookie refresher handles several accounts, and others can share the admin's.
- **Home Screen icon** for iPhone and Android.

## 0.3.0 (2026-11-21)

### Added
- **Editions:**
  - Links editions in your preferred language (a new setting, English by default).
  - The format follows your Hardcover edition: audiobooks if you listened, print or ebook if you read.
- **Linked editions (Import tab):** checks every linked book and fixes the ones on the wrong edition.
- **Hardcover editions (Import tab):** switches books on an edition that isn't in your language, only when you press
  Apply.
- **Removing books** (off by default): a book removed from Hardcover can be removed from your services too. Removals
  are verified, and more than 5 at once wait for your confirmation.

### Fixed
- **StoryGraph:**
  - Finds the entries it missed on books you'd finished.
  - Moves a wrong edition with its own "switch to this edition".

## 0.2.0 (2026-11-21)

### Changed
- One lock per service: a Goodreads challenge no longer holds up the other services.
- **Fewer Goodreads challenges:**
  - A steady gap between requests.
  - Fewer page fetches.
  - A cool-down after a challenge the refresher can't clear.
- **Pagebound:**
  - Better search.
  - Link a book by pasting its URL or id.
- **Matching:** co-written books and subtitles are handled better.

## 0.1.0 (2026-11-21)

- First release.
