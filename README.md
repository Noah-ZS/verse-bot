# Verse bot: popular Kreol Morisien verses -> Instagram

GitHub Actions cron -> `run.py` -> YouVersion Platform API (live verse text) -> Gemini (caption + image prompt)
-> Cloudflare FLUX (background) -> Pillow (1080x1350 JPEG, real text) -> Instagram Graph API.

## Files

```
run.py                       the whole pipeline (+ --list-bibles / --check helper modes)
popular_verses.csv           ranked list of well-known verses (USFM refs) + posting state. NO verse text stored
images/                      generated JPEGs (served via raw.githubusercontent.com)
.github/workflows/post.yml   scheduled posting
.github/workflows/refresh-token.yml   monthly Instagram token refresh
```

## Before you build anything: licensing (do this first)

- The Kreol Morisien Bible on YouVersion is bible.com version **344**, "Nouvo Testaman dan Kreol Morisien"
  (2009, Bible Society of Mauritius). Each Bible on the Platform has its own license; "free for non-commercial use"
  is not the same as permission to render the text into images posted on social media.
- Read the YouVersion Platform terms at platform.youversion.com and the `copyright` field of the Bible
  (`python run.py --list-bibles` prints it). Then email the Bible Society of Mauritius for written permission.
- The bot always puts the Bible's title and copyright line in the caption, as the API metadata asks.
- Keep the account non-commercial (no ads, sales or monetisation) while relying on the free Platform tier.

## One-time setup

1. Get an **App Key** at platform.youversion.com. Add it as secret `YVP_APP_KEY`.
2. `export YVP_APP_KEY=...` then `python run.py --list-bibles`. Confirm Kreol Morisien (`mfe`) Bibles are
   enabled for your key and note the ids. Put them in `YV_BIBLE_IDS` (comma-separated, tried in order).
   The listing needs `all_available=true` to show Bibles not yet enabled for your key; if 344 is missing,
   ask YouVersion/the publisher to enable it.
3. `python run.py --check --apply` tests every verse in `popular_verses.csv` against your Bible(s) and marks the
   missing ones `unavailable`. Commit the result. (Kreol Morisien is mostly New Testament, so expect many OT
   verses to drop out; see "How many verses" below.)
4. Other secrets: `GEMINI_API_KEY`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `IG_USER_ID`, `IG_TOKEN`, `GH_PAT`
   (see the Instagram/Cloudflare setup in the previous README section of our chat).
5. Edit `post.yml`: UTC cron times, `POST_TZ_OFFSET_HOURS`, `YV_BIBLE_IDS`.
6. Dry run: `DRY_RUN=1 python run.py` (needs `YVP_APP_KEY` + `GEMINI_API_KEY`; writes `images/dryrun-*.jpg`).
   Then Actions > post-verse > Run workflow (dry run), then once with *force* to publish.

## How verses are chosen

- `popular_verses.csv` is ordered by popularity. Rank 1-3 follow YouVersion's published top verses for 2025;
  the rest is a hand-built list of widely known verses (not YouVersion's data). Edit/reorder freely.
- Each run takes the highest-ranked **unposted** verse that exists in your Bible. Verses missing from the Bible are
  marked `unavailable` and skipped from then on. A verse can repeat after `REPEAT_DAYS` (default 90).
- Text is fetched **live** at post time and not stored in the repo. Multi-verse refs (`PHP.4.6-7`) are fetched
  verse by verse and joined.
- Safeguard: if more than 15 verses would be marked unavailable in a single run, the run aborts without saving
  (that means a config/API problem, not missing verses).

## How many verses

The list has 145 refs (104 NT, 20 Psalms/Proverbs, 21 other OT). If your Bible is NT-only, about 100 are usable:
at 3 posts/day that is ~5 weeks before repeats start. Options: post 1-2/day, set `REPEAT_DAYS` lower, add more
NT references to the CSV, or add the Bible id that includes Psalms/Proverbs to `YV_BIBLE_IDS`.

## Caption language

`CAPTION_LANG` defaults to Kreol Morisien. LLM Kreol quality is uneven: read the first dry-run captions, and
switch to `French` or `English` if they are not good enough. The verse itself always comes from the Bible text.

## Reliability, quotas, tokens

Unchanged from before: guard against double posts (`MIN_GAP_MINUTES`), image fallbacks (Cloudflare -> Pollinations ->
gradient), external cron trigger via cron-job.org for exact timing, monthly Instagram token refresh, 90-day-ish
repeat window. Watch Cloudflare Neurons (10k/day) and your YouVersion quota (rate-limit headers are returned).
