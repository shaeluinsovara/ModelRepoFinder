ModelRepoFinder  v1.6.2
=======================

This little app looks up open-source AI models on Hugging Face, and
open-source projects on GitHub, that should fit the computer you pick.
You choose the categories, preview the suggestions, and download only
the ones you approve. Nothing is downloaded until you say so.

The same folder works on Windows and on a Mac. A longer README.md is
also in this folder (and on GitHub) if you prefer that format.



What is new in v1.6.2
---------------------
  • Scheduled downloads finish again. The app was passing a progress
    option that the tested Hugging Face library (version 0.36.2) does not
    accept, so single-file downloads stopped with "unexpected keyword
    argument 'tqdm_class'". That is fixed.
  • When Hugging Face says "too many requests" (a rate limit), the app
    waits and tries again, spaces its requests out, and stops waiting
    after about two minutes per run. Search results are remembered for
    20 minutes, so a dry run and a real run do not ask twice.
  • If part of a search fails, the run still downloads the models it did
    find (within your limits) and the log says "search incomplete".
    Cleanup is skipped on those runs. If nothing was found because of a
    rate limit, the app tries again in about an hour.
  • Failed downloads show a short, plain reason.
  • A saved Hugging Face token is now used for searches too, which raises
    the limit. Get one free: make an account at huggingface.co, open
    Settings > Access Tokens, create a token with the Read role, and
    paste it into the app.


What was new in v1.6.1
----------------------
  • On a Mac, /etc is a shortcut to /private/etc. The app now checks both
    the folder path you typed and the real folder it points to, so system
    locations such as /etc are still refused. Folders under your home
    directory are unchanged. Linux and Windows behave as before.


What is new in v1.6
-------------------
  • A scheduled auto-scan, set up in the app for each computer. You can
    run it every day, every week on a day you pick, or every N days, at
    a time of day you choose. While the app is open, it checks the clock
    about once a minute. If a run was missed because the app was closed,
    it is caught up the next time you open it.
  • Turning the schedule on is your standing approval to download models
    that match the conditions you saved, without a separate Approve click
    each time. The page says this. A "Dry run / preview" button shows
    what would be downloaded and what would be retired or deleted, without
    changing anything. "Run now" does the job once.
  • You choose the conditions: which categories and/or Top picks, how to
    sort, which formats, whether models must fit the computer's size cap,
    optional minimum downloads or likes, and whether to skip models already
    on this computer. Each run has limits: a maximum number of new models,
    a maximum total of auto-managed models, and a maximum amount of disk.
  • Cleanup only applies to models the schedule itself downloaded, or to
    a model you opted in from the "Downloaded with this app" list. Models
    the app only found on the computer, and ordinary manual downloads, are
    left alone. You can keep the newest N per category, retire ones older
    than a number of days, or retire ones superseded by a newer version
    of the same family. The default is to move them to the retired folder.
    Permanent deletion requires you to type DELETE and confirm. At least
    one model is always kept in each category. Cleanup waits until
    downloads have finished and been checked, and is skipped if a search
    or download fails, including when there is no internet.
  • An activity log shows recent runs and what was added, retired, or
    deleted. The schedule only runs while this app is open. See "Keeping
    the app running at login" below if you want it to start when you
    log in. The app will not add itself there.
  • This schedule is for Hugging Face models. GitHub repositories are
    not auto-downloaded.
  • Every safety check from v1.4, and every finder feature from v1.5,
    is still in place.


What is new in v1.5
-------------------
  • Refresh now shows a new batch of models and GitHub repos each
    time, instead of repeating the same list. The app remembers what
    it already showed (on this computer only), looks further into the
    results, and skips things you have already downloaded or approved.
    When you have seen the current set, the list starts over and tells
    you so. A "Show earlier suggestions again" button clears that
    memory if you want to start over yourself.
  • You can sort Hugging Face suggestions by most downloaded, most
    liked, trending, or newest. You can sort GitHub suggestions by
    most stars, recently updated, or a "fresh mix" from a larger pool.
  • The app can look for models already on this computer — in the
    Hugging Face cache, Ollama, LM Studio, its own folders, and any
    extra folders you add — so you do not download a duplicate.
    Matching suggestions are marked "Already on this computer." This
    scan is read-only. Retire still only moves files this app itself
    downloaded, and still never deletes them.
  • A Top picks button shows popular Hugging Face models (most
    downloaded, most liked, or trending) that should fit the computer
    you picked, without needing search words. Popularity is a
    community signal, not a guarantee of quality.
  • Every safety check from v1.4 is still in place.


What is new in v1.4
-------------------
  • This is a hardening release. The app's job is the same; several
    safety checks and wording fixes were added before wider use.
  • Category names from the browser are checked before anything is
    written. Odd values such as slashes or ".." are refused.
  • The local page now uses a one-time token, and the service checks
    that requests really come from this computer. A web page from
    somewhere else should not be able to poke at the downloader.
  • Whole-folder model downloads keep each model's license and README
    next to the weights. (They still skip the .git folder.)
  • If a model ships a companion file (mmproj, projector, CLIP, or a
    vision-encoder) the app offers it with the recommended GGUF file
    and downloads it too, by default. Vision models usually need that
    file to work. Ordinary text models are unchanged.
  • The interface now shows on-disk size and a rough estimate of the
    memory needed to run a model, as two different numbers. The size
    cap is a first approximation, not a guarantee.
  • If a GitHub project is fetched as a zip (when git is not
    installed), every file in the zip is checked so it cannot land
    outside the intended folder.
  • Optional tokens on Windows are limited to your user account when
    the system allows it. On a Mac they already were.
  • Helper libraries are pinned to exact versions, so this release
    should behave the same the next time you start it.
  • A test suite is included (see README.md for how to run it).


What is new in v1.3
-------------------
  • If a download hits a brief network hiccup (the kind of Hugging Face
    connection glitch that used to stop a large file mid-way), the app
    tries again automatically, up to four times, and picks up what was
    already saved when it can. You should see a line like
    "Connection hiccup, retrying (2/4)…"
  • If Hugging Face's faster downloader keeps erroring, the app switches
    to a simpler method for the rest of that session and keeps going.
  • If a download still fails after those tries, a "Retry failed" button
    appears. You do not need to close the app and start completely over.
  • If you approved several models at once, one failure does not stop
    the others.


What is new in v1.2
-------------------
  • On the Hugging Face tab, you can paste any model link (or just
    org/name). Links that go to /tree/main or to a single file work too.
  • Click Check to preview the files, the total size, the format
    (GGUF, safetensors, PyTorch, ONNX, MLX, and others), the license,
    and a short note about what app can run it. If the model is bigger
    than this computer's cap, you get a warning, but you can still
    download it.
  • If the repo has GGUF files, you can pick one quality setting, or
    download the whole folder. The suggested file uses the same mid-size
    default as before.
  • If a repo has both safetensors and extra .bin copies of the same
    weights, the app skips the .bin files by default. You can un-tick
    that box to include everything.
  • Gated models need a Hugging Face token, and you must accept the
    license on the model's page in your browser first. The token is
    saved only on this computer, like the optional GitHub token.
  • Pasted downloads go to incoming/<computer>/manual/<org>__<name>/
    and can be Retired the same way as any other model.


What is new in v1.1
-------------------
  • You can tick which categories to search, and those ticks are saved
    per computer.
  • Built-in Hugging Face categories now include coding, emotional /
    relational chat, spiritual / esoteric knowledge, vision (image and
    video), music creation with voice cloning, speech / TTS, background /
    reasoning, and embeddings.
  • You can add your own category (a name, some search words, and how
    many results to keep).
  • Music, speech, and similar categories can include models that are
    not GGUF. The app still respects the computer's size cap, shows the
    format, and adds a one-line note about what can run it, when known.
  • A second tab, GitHub repos, finds recently updated projects
    (about the last 12 months), ranked by stars. Approve & download
    will git clone if git is installed, or download a zip if it is not.
    You can Update or Retire a downloaded repo later.


How to start on Windows
-----------------------
1. Unzip the folder if you have not already, and open it.
2. Double-click Start-Windows.bat.
3. The first time, it will set up a private toolkit folder and may take a
   minute. Leave the black command window open.
4. Your browser should open to the app. If it does not, look in the command
   window for a line like http://127.0.0.1:8765/ and paste that into the
   browser.

If you see a message that Python was not found, install Python 3.9 or newer
from https://www.python.org/downloads/  — during setup, check the box that
says "Add python.exe to PATH". Then run Start-Windows.bat again.

If you are updating from an earlier copy: keep your incoming folder and, if
you have already set a save path or approve list, keep your machines.yaml
too. Or unzip into a new folder and paste your save path again.


How to start on a Mac
---------------------
1. Unzip the folder if you have not already, and open it.
2. The first time, right-click Start-Mac.command and choose Open. If macOS
   warns that the file is from the internet, click Open in the dialog.
   After the first time, you can double-click it.
3. If macOS still will not run it, open System Settings, then Privacy &
   Security, scroll down, and click Open Anyway next to the message about
   Start-Mac.command.
4. If it is still blocked, open Terminal and run this, using the real path
   to the unzipped folder:

     xattr -dr com.apple.quarantine /path/to/ModelRepoFinder

   Then try right-click, Open again.
5. A Terminal window opens and sets things up. Leave it open.
6. Your browser should open to the app. If it does not, look in the Terminal
   for a line like http://127.0.0.1:8765/ and paste that into the browser.

If you see a message that Python was not found, install Python 3.9 or newer
from https://www.python.org/downloads/  and then run Start-Mac.command again
(right-click, Open).


What the buttons do
-------------------
Which computer is this for?
    Pick the machine you are finding things for. The app tries to guess
    (Windows usually starts on a PC preset; a Mac usually starts on a
    Mac preset). Change it if the guess is wrong. Category ticks then
    change to the ones saved for that computer.

    The names in the list are only examples. You can edit machines.yaml
    to rename them, change the size cap (max_model_gb), add a computer,
    or set enabled: false to hide one. Caps should usually sit a little
    under the memory you can spare. You do not have to be a programmer
    to change those numbers; make a copy of the file first if you want
    a backup.

Save files in this folder
    Where downloads should go. By default this is the folder next to the
    app. You can paste the path to a shared folder or NAS and click
    Save folder.

Category ticks
    Choose which kinds of models or repos to search. Use Select all or
    Select none if you like. The small number beside each category is
    how many suggestions to keep. Add your own category if you want a
    topic that is not in the list.

Paste any Hugging Face link / Check
    Paste a model URL or org/name, then click Check. You will see the
    files, size, format, license, and a note about what can run it.
    Approve & download saves it under incoming/<computer>/manual/.
    If the model has several GGUF quality settings, pick one or choose
    "Whole repo". A warning appears if it is larger than this computer
    can comfortably run, but the download is not blocked.

Optional Hugging Face token (recommended)
    Raises how many requests Hugging Face allows before it says "too
    many requests", and is needed for gated models. To get one: make a
    free account at huggingface.co, open Settings > Access Tokens,
    create a token with the Read role, and paste it here. For gated
    models, also accept the license on the model's page. It is saved
    only on this computer (system keychain if available, otherwise a
    hidden file only your user can read) and is sent only to Hugging
    Face.

Refresh suggestions
    Looks up items that fit the ticked categories. This can take a few
    minutes if many categories are ticked. It does not download anything.

Approve & download
    Adds that item to your approve list and then downloads it. You can
    tick several checkboxes in a category and use the category button,
    or use the button on a single item. Progress appears on the page.
    If a Hugging Face download hiccups, the app retries on its own. If
    it still fails, click Retry failed. Pieces that already arrived are
    kept when possible.

GitHub tab
    Same idea, for source-code projects. If git is installed, Approve &
    download will clone the repo. If git is not installed, the app
    downloads a zip of the default branch instead. Update later runs
    git pull, or re-downloads the zip.

Optional GitHub token
    Only needed if GitHub starts refusing searches (it limits how many
    you can do per minute without a token). A personal access token is
    saved only on this computer, in a hidden local file, and is sent
    only to GitHub. Leave the box blank unless you need it.

Retire
    Moves a downloaded item into the retired folder. It is never deleted,
    so you can get it back if you change your mind.

Name links
    Open the Hugging Face model card, or the GitHub page, in a new tab
    so you can read about it before you approve it.


Where the files go
------------------
  <your folder>/incoming/<computer>/<tag>/            Hugging Face models
  <your folder>/incoming/<computer>/manual/<name>/    models you pasted a link for
  <your folder>/incoming/github/<tag>/                GitHub repos
  <your folder>/retired/...                           items you retired

"tag" is the category, such as coding, relational, or runners.

On a Mac, GGUF models (which work in Ollama or LM Studio) and MLX models
(made for Apple Silicon) are both offered. Windows machines are offered
GGUF, plus other formats in categories like music and speech when those
models still fit the size cap.

The app also writes a text report called report.md next to itself after
each Hugging Face refresh, if you like a written copy.


A few gentle rules already built in
-----------------------------------
  • Preview (dry-run) is the default. Nothing downloads without a click.
  • If a model ships a companion file such as mmproj, projector, or
    clip, that file is offered and downloaded with the model, because
    vision and other multimodal models usually need it. It is not
    treated as a model on its own.
  • The app prefers mid-size quality settings (Q4_K_M, Q5_K_M, and similar)
    around 70% of each computer's size cap, so there is room to breathe.
  • Background / reasoning models have an extra smaller cap, so they stay
    light enough to leave running.
  • Only as many items as each category's "keep" count are suggested.
  • Retired items are moved, never deleted.
  • Spiritual / esoteric search is a bit fuzzy: it also looks at model-card
    words and tags, because names alone are often not enough.



Keeping the app running at login (optional)
-------------------------------------------
  The scheduled auto-scan only runs while ModelRepoFinder is open. If
  you want it to start when you log in, you can add the launcher to
  your computer's startup list yourself. The app will not do this
  for you, and it will not change those settings.

  On a Mac:
    1. Open System Settings.
    2. Go to General, then Login Items (the wording can vary a little
       by macOS version).
    3. Click the + button, and choose Start-Mac.command from the
       ModelRepoFinder folder.
    The first time, macOS may ask you to confirm. You can also
    right-click the file and choose Open. Leave the Terminal window
    open after it starts.

  On Windows:
    1. Press Windows+R, type shell:startup, and press Enter.
    2. In the folder that opens, make a shortcut to Start-Windows.bat
       from the ModelRepoFinder folder.
    Leave the command window open after it starts.

  Closing that window stops the schedule until you start the app again.


If something goes wrong
-----------------------
  • Leave the command or Terminal window open and read the message.
  • Make sure you are online; the app needs to reach huggingface.co
    and, for the second tab, api.github.com.
  • If GitHub searches fail with a rate-limit message, wait a minute
    or paste a token.
  • If a Hugging Face download fails, wait for the automatic retries,
    then click Retry failed if that button appears. You usually do not
    need to close the app and start completely over. Pieces that already
    arrived are kept when possible.
  • Close the window and start the app again only if it will not open,
    or if Retry failed does not help.
  • You can screenshot the window if you want to ask someone for help.

You do not need to "code" anything to use this. If you want to change
size caps later, that lives in the machines.yaml file — make a copy of
it first if you would like a backup.

This app helps you find and download files. You are responsible for
reading and following each model's or project's license before you
use it.

Created by shaeluinsovara with Lumi, an AI assistant.
Released under the MIT License. See LICENSE.
