# Illumin8 Show Tools - shared block library

Blocks shared from Illumin8 Show Tools (**Share block** in System Designer's library) arrive here as pull requests,
are checked automatically, reviewed, and on merge are published as a signed library that every copy of Show Tools
downloads when it starts.

```
Show Tools  --Share block-->  Cloudflare Worker  --pull request-->  this repository
                                                                     |  check.yml: automated checks, report posted on the PR
                                                                     |  reviewer: checks against the maker's sheet, merges
                                                                     v
Show Tools  <--manifest.json + library.json (signed)--  GitHub Pages  <--  publish.yml on merge
```

## Setting it up (once)

1. **Repository.** Create a GitHub repository (e.g. `illumin8studios/show-tools-blocks`) and push this folder to `main`.
   A public repository is simplest (GitHub Pages is free for public repositories). Submitted blocks are public there;
   that's the point of a shared library.
2. **Signing key.** In the Show Tools source folder run `python3 block_share.py --new-keys`.
   - Add the **private** key as a repository secret: Settings › Secrets and variables › Actions › New secret,
     name `LIBRARY_SIGNING_KEY`. Never commit it or put it in the app.
   - Put the **public** key in `block_share.py` (`LIBRARY_PUBLIC_KEY`). The app refuses any library that doesn't
     verify against it, so nobody can slip a doctored library onto users.
3. **Pages.** Settings › Pages › Source: **GitHub Actions**. Run the **Publish the library** workflow once (Actions ›
   Publish the library › Run workflow). Its address (e.g. `https://illumin8studios.github.io/show-tools-blocks/`) is
   `LIBRARY_URL` in `block_share.py`.
4. **Submission service.** In Cloudflare (free plan) › Workers › Create › paste `worker.js`. Settings › Variables:
   `REPO` = `illumin8studios/show-tools-blocks`; secret `GITHUB_TOKEN` = a fine-grained GitHub token for this
   repository only, with **Contents**, **Pull requests** and **Issues** read & write. Optionally bind a KV namespace as
   `LIMITS` (20 submissions per address per day). The worker's address plus `/submit` is `SUBMIT_URL` in `block_share.py`.
5. Rebuild Show Tools. From then on users can share blocks, and everyone gets the library at start-up.

## Reviewing a submission

1. A pull request opens with the block in `submissions/<maker>-<model>.json`.
2. **Check submitted blocks** runs and posts a report on the pull request:
   - errors (unknown signal or connector, impossible direction, duplicate socket names, no source link) fail the check;
   - warnings (unusual connector for the signal, no power inlet, network port not in/out, not the maker's own site,
     different from a block already in the library - with the differences);
   - if the user attached the spec sheet, the spec-sheet reader's port count by signal and direction against the
     block's, with a score out of 100;
   - the socket table and the reviewer checklist.
3. Open the source link and go through the checklist against the manufacturer's sheet. Fix anything small directly in
   the file on the pull request.
4. Publish as **shared** (reviewed) - the default - or set `"status": "verified"` in the file when you've checked it
   line by line against the manufacturer's published specification.
5. Merge. **Publish the library** builds and signs `library.json`; every Show Tools picks it up at its next start.

To withdraw a block, set `"withdrawn": true` in its file (or delete the file) and merge.

**Problem reports** from the app ("Report problem" on a shared block) arrive as issues labelled `problem report`.

## Keeping the checks in step with the app

`tools/` holds copies of the app's `block_share.py`, `specs.py`, `licensing.py` and its signal / connector
catalogue. When signals or connectors change in the app, refresh them from the Show Tools folder:

```
python3 block_share.py --export-repo-tools ../show-tools-blocks/tools
```

The app's self test fails if its copy in `repository/tools` is out of date.

## Costs and privacy

GitHub (public repository, Actions, Pages) and Cloudflare Workers' free plan cover this comfortably. A submission
carries the block, the optional name the user types, their notes and spec sheet text, and the app version. The
worker sees the sender's IP address only to apply the daily limit (kept 25 hours when the KV limit is on).
