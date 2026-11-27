# Changelog

What changed in each release of Heliograph. Each release's notes on GitHub are taken from its section here.

## Unreleased

### Added
- **Favorites sync** (on by default): books on your Hardcover **Favorites** list go to StoryGraph's favorites, your
  Fable **Favorites** list, and a **favorites** shelf on Goodreads and Pagebound (made the first time a book goes on
  it; the Goodreads shelf name is a setting). Take one off the list and it comes off where Heliograph put it, never
  where you did. Turn it off with **Settings → Sync favorites**.

## 0.6.4 (2026-11-27)

### Added
- **Progress that goes backwards waits for you.** If Hardcover says you're further back in a book you're still reading
  (an app that reset its position, say), Heliograph doesn't send it. The book is flagged: a banner on the Dashboard,
  **↩ Progress went back** in the Books filter, and on the book itself **Yes, send it** or **No, keep my services'
  progress**. Starting a re-read isn't counted, and the book's status and rating still sync as usual.
- **Owned books sync** (on by default): books on your Hardcover **Owned** list are marked as owned on Goodreads
  ("I own a copy of this edition"), StoryGraph and Pagebound. Take one off the list and it's unmarked, but only
  where Heliograph marked it: a book you marked owned on a service yourself is left alone. Nothing else about the
  book changes, and Fable has no way to mark a book as owned. Turn it off with **Settings → Sync owned books**.
- **Tools → BookOrbit → Mark as owned:** your BookOrbit books are copies you own, so Heliograph can add each one
  that's in your Hardcover library to your Owned list and mark it as owned on your services. A new setting under
  **Settings → BookOrbit** does the same for books you add later (off by default).

### Fixed
- **Switching between the audiobook and the ebook** no longer compares a percentage with a page number. Before, going
  from page 50 to 10% of the audiobook looked like a big jump and was sent; now both are compared as percentages.

## 0.6.3 (2026-11-25)

### Fixed
- **Tools → BookOrbit:** when your Hardcover library has a book under a different Hardcover record (Hardcover often
  has several), the scan uses your library's record instead of linking another one. BookOrbit's own sync would
  otherwise add the book to your Hardcover library a second time. If the two are only a subtitle apart, it asks
  you under *Needs a look* instead of deciding.

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
