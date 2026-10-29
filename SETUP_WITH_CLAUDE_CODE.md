# Setting up the shared block library with Claude Code

Steps for Claude Code, run from the Show Tools folder on the user's Mac. Ask the user before each step that creates
something public or changes settings, and say what was done afterwards.

## Before starting
- Check `gh --version` works and `gh auth status` shows the user is signed in (if not: install the GitHub CLI with
  `brew install gh`, then the user runs `gh auth login` themselves).
- Ask the user for the GitHub owner (their user or organisation) and the repository name (suggest
  `show-tools-blocks`). It must be **public** for free GitHub Pages; confirm that's OK.

## 1. Create the repository and push the kit
- Copy `repository/` (including `.github/` and `submissions/.gitkeep`) to a new folder outside the app, e.g.
  `../show-tools-blocks`, `git init -b main`, commit, then
  `gh repo create <owner>/<name> --public --source . --push`.

## 2. The signing key - generated and stored without ever being shown
- Generate the pair on this Mac and send the private key straight into the repository secret, never printing it,
  never writing it inside either repository:
  ```
  python3 - <<'PY' > /tmp/i8pub.txt
  import os, subprocess, sys
  sys.path.insert(0, ".")
  import licensing
  secret = os.urandom(32)
  subprocess.run(["gh", "secret", "set", "LIBRARY_SIGNING_KEY", "--repo", "<owner>/<name>"],
                 input=secret.hex().encode(), check=True)
  print(licensing.public_key(secret).hex())
  PY
  ```
- Ask the user whether they want a backup of the private key (needed only to re-create the secret). If yes, have them
  generate a new pair later in a password manager rather than writing it to disk here; if the secret is ever lost,
  make a new pair and update the public key in the app (users then need the new build).
- Put the public key from `/tmp/i8pub.txt` into `block_share.py` as `LIBRARY_PUBLIC_KEY`, then delete the temp file.

## 3. GitHub Pages
- Enable Pages with GitHub Actions as the source:
  `gh api -X POST repos/<owner>/<name>/pages -f build_type=workflow` (if it already exists, use `-X PUT`).
- Run the publish workflow once: `gh workflow run "Publish the library" --repo <owner>/<name>`, wait for it
  (`gh run watch`), and check `https://<owner>.github.io/<name>/manifest.json` loads.
- Put `https://<owner>.github.io/<name>/` into `block_share.py` as `LIBRARY_URL`.

## 4. The submission service (the user does this part - it needs their Cloudflare login and a token)
Walk the user through it:
1. GitHub: Settings › Developer settings › Fine-grained tokens › Generate: only the new repository; permissions
   **Contents**, **Pull requests**, **Issues**: Read and write. They keep it on screen - not pasted into chat.
2. Cloudflare dashboard › Workers & Pages › Create › Worker › paste `worker.js` › Deploy.
3. Worker › Settings › Variables: `REPO` = `<owner>/<name>`; secret `GITHUB_TOKEN` = the token.
   Optional: create a KV namespace and bind it as `LIMITS`.
4. They give you the worker's address; `SUBMIT_URL` in `block_share.py` is that address + `/submit`.

## 5. Test end to end
- `curl` a test submission to `SUBMIT_URL` (a small valid block with a source link) and check a pull request opens and
  the "Check submitted blocks" report is posted. Close that test pull request without merging and delete its branch.
- In the app folder: `python3 block_share.py --export-repo-tools ../show-tools-blocks/tools` if the app's checks changed,
  commit and push in the repository.

## 6. Finish in the app
- Run `python3 show_tools.py --selftest`, bump the version (patch), add the change log line ("Shared block library
  live"), rebuild the manual, and commit the app changes (`block_share.py` now has the three settings; no secrets).
- Tell the user to rebuild the app with `./build_mac_app.sh`.
