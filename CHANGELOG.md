# Changelog

## v1.6.1 — Mac scan-folder check

A small fix on top of v1.6.

- **System folders stay off the scan list on a Mac.** `/etc` on macOS is a shortcut to `/private/etc`. The app now checks both the path you typed and the real folder it points to, and it knows the Mac locations those shortcuts open (`/private/etc`, `/private/var`, `/private/tmp`, and system roots such as `/System`, `/Library`, and `/Applications`). A folder you add under your home directory is unchanged. Linux and Windows behave as before.

## v1.6 — scheduled auto-scan

A features release on top of the v1.5 finder and the v1.4 hardening. Every v1.4 safety check is still in place.

- **A scheduled auto-scan, per computer.** In settings you can turn on a scan that runs daily, weekly on a chosen weekday, or every N days, at a time of day you pick. While the app is open, a background check looks at the clock about once a minute. The last run is remembered, and a missed run is caught up the next time you open the app. Turning the schedule off and on again starts a new waiting period, so it will not immediately fire from an old last-run date.
- **Standing approval, said plainly.** Turning the schedule on is your approval to download models that match the conditions you saved, without a separate Approve click each time. The page says this. "Run now" is a one-off of the same job, and "Dry run / preview" shows what would be downloaded and what would be retired or deleted, without changing anything.
- **Conditions and limits.** You choose the categories and/or Top picks, the sort (most downloaded, most liked, trending, or newest), the formats, whether models must fit the computer's size cap, optional minimum downloads and likes, and whether to skip models already on this computer (using the v1.5 local scan). Each run is capped by a maximum number of new models, a maximum total of auto-managed models, and a maximum amount of disk for those models.
- **Cleanup of out-of-date models, with a safe default.** Cleanup only applies to models this schedule downloaded, or to a model you explicitly opted in (from the "Downloaded with this app" list). Models the local scan merely found, and ordinary manual downloads, are left alone. You pick a policy: keep the newest N per category, older than a number of days, or superseded by a newer version of the same family. The default action is **Retire** (move to the retired folder). **Delete permanently** requires you to type DELETE, tick a confirmation, and confirm in a dialog; the same confirmation is required by the local API. Path checks on delete are as strong as on retire, and a delete is refused if it would leave the intended folder.
- **Safeguards.** At least one model is kept in each category, so you are not left with nothing usable offline. A model is never removed while a download is in progress. Downloads finish and are checked before cleanup starts. If a search or download fails, including when there is no internet, cleanup is skipped; an offline run is retried later and does not move the next scheduled time.
- **Activity log.** The page shows recent runs: what was added, retired, or deleted, and any errors.
- **Keeping the app open.** The schedule only runs while ModelRepoFinder is open. The README explains, in plain language, how to add the launcher to macOS Login Items or the Windows Startup folder if you want. The app does not add itself there.
- This release is about Hugging Face models. GitHub repositories are not auto-downloaded.


## v1.5 — fresh suggestions, local scan, and top picks

A features release on top of the v1.4 hardening. Every v1.4 safety check is still in place.

- **Fresh suggestions on Refresh.** The same Refresh button now shows a new batch each time, instead of repeating the same models and repositories. The app remembers what it already showed (on this computer only), looks further into a larger pool of results, and skips items you have already downloaded or approved. When you have seen everything in the current pool, the list starts over and tells you so. A **Show earlier suggestions again** button clears that memory.
- **Sort options.** Hugging Face suggestions can be sorted by most downloaded, most liked, trending, or newest. GitHub suggestions can be sorted by most stars, recently updated, or a fresh mix that picks from a larger pool with a preference for more-starred projects. Trending uses Hugging Face’s `trendingScore` sort; if a server ever rejects that, the app falls back to likes and says so.
- **Models already on this computer.** A read-only scan looks in the Hugging Face cache, Ollama, LM Studio, this app’s own folders, and any extra folders you add. It reports size and path. Search results that match a model already here (by repository name or GGUF filename) are marked **Already on this computer**. The scan is bounded (depth, file count, and a time limit) and will not follow a path outside the folder it was asked to look at. Retire still only moves files this app manages, and still never deletes.
- **Top picks.** A no-keyword view lists popular Hugging Face models — most downloaded, most liked, or trending — filtered to ones that should fit the computer you picked, overall and for the categories you have ticked. The page clearly says that popularity is a community signal, not a guarantee of quality.

## v1.4 — hardening

A hardening release. No new features, just a tighter, more trustworthy v1.3.

- **Safer downloads.** Category names from the browser are checked before anything is written. Slashes, `..`, absolute paths, and unknown categories are refused, and the final folder is confirmed to sit under the intended directory.
- **A local API boundary.** The service already listened only on this computer. It now also checks the `Host` header, checks `Origin` when a browser sends one, limits request size to 1 MB, and requires a random token that is issued when the app starts and included by its own page. A web page from somewhere else should not be able to poke at a running downloader.
- **License files stay with the model.** Whole-folder downloads still skip `.git`, but they keep `LICENSE`, `license*`, and README model cards, so the license sits next to the weights.
- **Companion files for multimodal models.** `mmproj`, projector, CLIP, and vision-encoder files are no longer skipped globally. When a repo has them, the matching companion is offered alongside the recommended GGUF file and downloaded with it by default, and the interface shows it.
- **Clearer size wording.** The interface now distinguishes on-disk size from a *rough estimate* of the memory needed to run a model. The computer’s size cap is still a first approximation, not a guarantee.
- **Safer GitHub zip fallback.** If git is not available and a zip is used, every file in the archive is checked so it cannot extract outside the staging folder (absolute paths, `..`, and escaping symbolic links are refused).
- **Tighter token files on Windows.** Optional tokens are still stored only on this computer. On macOS and Linux they keep `0600` permissions. On Windows the app now asks the system to limit the file to the current user (best effort; a failure is logged and is not fatal). If the optional `keyring` package is already installed, the system keychain is preferred; it is not a required dependency.
- **Pinned helper libraries.** A `requirements.lock` file records the exact versions tested with this release. The Windows and Mac launchers only reinstall when the toolkit folder is new or the lock file has changed, so the same v1.4 should behave the same next month.
- **Automated tests.** A `tests/` folder covers link parsing, path checks, format detection, size warnings, retries and the Xet fallback, GitHub names, retire/restore, zip extraction, and hostile API inputs. A GitHub Actions workflow runs the suite on Ubuntu, Windows, and macOS, with Python 3.9 and 3.12.

## v1.3

Resilient Hugging Face downloads: automatic retries after brief connection hiccups, a fallback away from Hugging Face’s Xet path, a Retry failed button, and one failure in a batch no longer stopping the rest.

## v1.2

Paste any Hugging Face model link, preview files, size, format, and license, pick a GGUF quality setting or download the whole folder, and handle gated models with a local token.

## v1.1

Category ticks (including custom categories) remembered per computer, more Hugging Face categories, non-GGUF models in music and speech, and a GitHub tab.

## v1.0

First public release: local search and download for Hugging Face models, with an approve-before-download rule and Retire that moves files instead of deleting them.
