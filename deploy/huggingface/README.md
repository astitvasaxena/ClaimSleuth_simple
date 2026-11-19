---
title: ClaimSleuth
emoji: 🔎
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Multi-agent warranty claim investigation (offline demo)
---

# ClaimSleuth on Hugging Face Spaces (no credit card)

The file above is the Space "front matter". To deploy, the Space repository must contain, at its root: this file
renamed to `README.md` (the front matter must be the first thing in it), a file named `Dockerfile` (a copy of
`Dockerfile.single`), and the folders `src/`, `data/`, `requirements/` plus `pyproject.toml`. The helper below builds
exactly that in a scratch folder so your main repository is not changed.

## What runs
One container (`Dockerfile.single`): the dashboard on port 7860, which itself starts the MCP server and the CrewAI A2A
service on 127.0.0.1 inside the container. Both Python environments (`/app/.venv`, `/app/.venv-crew`) are in the image.
Runs as user uid 1000. The disk is ephemeral: stored runs vanish when the Space restarts or sleeps. SQLite works
because everything is in one container on one disk.

## Create the Space (you do this; free Hugging Face account, no card)
1. Sign up at https://huggingface.co and create an access token with "write" role (Settings, Access Tokens).
2. New Space: Owner = you, name `claimsleuth`, SDK = Docker (Blank), hardware = CPU basic (free), visibility = Public or Private (see below).
3. Build the Space folder and push (replace `YOUR_NAME`):
```
SPACE=$(mktemp -d)/claimsleuth-space
git clone https://huggingface.co/spaces/YOUR_NAME/claimsleuth "$SPACE"      # username + token as password when asked
cp -R src data requirements pyproject.toml README.md "$SPACE"/               # README.md here is replaced in the next line
cp deploy/huggingface/README.md "$SPACE"/README.md
cp Dockerfile.single "$SPACE"/Dockerfile
cd "$SPACE" && git add -A && git commit -m "ClaimSleuth demo" && git push
```
4. The Space shows "Building" for roughly 5 to 10 minutes (two Python environments), then "Running". The app is at
   `https://YOUR_NAME-claimsleuth.hf.space` and embedded on the Space page.

## Mode and secrets
- Default is OFFLINE (labelled rule-based test doubles, no key needed). Nothing to configure.
- For live Gemini: Space, Settings, Variables and secrets: add a SECRET `GOOGLE_API_KEY` and a VARIABLE `EXECUTION_MODE` = `live`.
  Secrets are injected as environment variables at runtime and are not shown in the build logs or copied into the image.
  Never put the key in a file you push. The free Gemini tier allows only about 20 requests/day/model; a live run needs 6 to 9.
- Rebuild is automatic after each push or after changing a variable.

## Public vs private
- Public: anyone with the link can open the dashboard and run claims (there is no login in the app). Fine for offline mode, because nothing sensitive is stored and the disk is wiped on restart. Do NOT combine a public Space with a live API key: strangers would spend your Gemini quota.
- Private: only you (and org members) can open it; visitors must be logged in to Hugging Face. Use this when a key is configured. To let a marker view it, share it as a public Space in offline mode instead, or add them as a collaborator.
- The reviewer name in the dashboard is still typed by the user and is not tied to any login.

## Limits (free CPU basic, as documented by Hugging Face at time of writing; check the current page)
- About 2 vCPU, 16 GB RAM, 50 GB ephemeral disk. The app uses roughly 400 MB.
- Free Spaces go to sleep after about 48 hours without visitors; the first visit wakes them (about a minute).
- No persistent storage on the free tier: decisions and human reviews are lost on restart. Persistent storage is a paid add-on.
- Outbound internet is allowed, so live Gemini calls work.
- The Space URL is HTTPS, provided by Hugging Face. No Caddy, no firewall to manage.

## Test the same image locally
```
docker build -f Dockerfile.single -t claimsleuth-single .
docker run --rm -p 127.0.0.1:7860:7860 claimsleuth-single      # open http://127.0.0.1:7860
```

## Teardown
Space, Settings, "Delete this Space". Revoke the access token under Settings, Access Tokens.
