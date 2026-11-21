# py-drive-uploader

A small HTTP backend that uploads files to a Google Drive folder. Clients never hold the Google
credentials: they POST files here, and the server uploads them and returns a share link.

## Setup

### Python and Poetry
Needs **Python 3.15+** and **[Poetry](https://python-poetry.org/) 2.x**.

```bash
# Install Poetry (once per machine), e.g. with pipx:
pipx install poetry            # or: curl -sSL https://install.python-poetry.org | python3 -

# Optional: keep the virtualenv in ./.venv (writes poetry.toml in this folder)
poetry config virtualenvs.in-project true --local

# Pick the Python 3.15 interpreter, then install the dependencies
poetry env use python3.15
poetry install
```

Run commands inside the environment with `poetry run …` (used below). Or activate it once per
shell with `eval $(poetry env activate)`. Poetry 2 removed `poetry shell`.

To add a dependency, run `poetry add <package>`. This updates `pyproject.toml` and
`poetry.lock`; commit both.

### Google Cloud (one-time)
1. Enable the **Google Drive API** in your Cloud project.
2. Create an **OAuth client ID** of type **Desktop app**. Download its JSON and save it as
   `oauth-client.json` in this folder.
3. Choose a publishing status (Google Auth Platform → **Audience**):
   - **Testing (simplest):** add your Google account as a **test user**. The token in
     `token.json` **expires 7 days after you sign in**, so run `auth` again shortly before each use (see
     [Replacing the token](#replacing-the-token)).
   - **In production (no weekly sign-in):** Google requires an app name, a support email, a
     **homepage URL** and a **privacy policy URL** (Google Auth Platform → **Branding**) before
     you can click **Publish app**. `drive.file` is a non-sensitive scope, so you don't need full
     verification. When you sign in, Google may warn that the app is unverified; continue
     through **Advanced → Go to (app name)**.

### Sign in (one-time)
```bash
poetry run python main.py auth
```
This opens a browser. Sign in with the Google account that owns the Drive folder, and the
command writes `token.json`. You can run it on any machine with a browser, then copy
`token.json` to the server.

> `oauth-client.json` and `token.json` are secrets and are ignored by git (`*.json`).
> Don't commit them or share them.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DRIVE_FOLDER_ID` | yes | | The parent Drive folder's ID (the last part of its URL) |
| `API_KEY` | recommended | none (no check) | Shared secret clients send as the `X-Api-Key` header |
| `HOST` | no | `0.0.0.0` | Address to listen on |
| `PORT` | no | `8766` | Port |
| `MAX_UPLOAD_MB` | no | `25` | Upload size limit |
| `TOKEN_FILE` | no | `token.json` next to `main.py` | Path to the token, e.g. a mounted secret file. If it can't be written back, the server keeps going |
| `KEEP_ALIVE_URL` | no | none (off) | This server's public `/health` URL. The server requests it regularly so hosts that sleep idle services keep it awake |
| `KEEP_ALIVE_MINUTES` | no | `10` | How often to request `KEEP_ALIVE_URL` |

Generate an API key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Commands

```bash
# Check that the token works and that the folder can be reached
DRIVE_FOLDER_ID=<folder id> poetry run python main.py check

# Run the server
DRIVE_FOLDER_ID=<folder id> API_KEY=<key> poetry run python main.py serve
```

## API

| Method & path | Body | Response |
|---|---|---|
| `GET /health` | none | `200 {"ok": true}` |
| `POST /folder?name=<subfolder>` | none | `201 {"folderId", "folderUrl"}` |
| `POST /upload?name=<file>&folder=<subfolder>` | raw file bytes, with `Content-Type` such as `image/gif` or `image/png` | `201 {"id", "name", "url", "folderId", "folderUrl"}` |

- **Authentication:** if `API_KEY` is set, send `X-Api-Key: <key>` on every POST. Otherwise the server returns `401`.
- **Subfolders:** `/folder` and the `folder` parameter of `/upload` find or create a subfolder of `DRIVE_FOLDER_ID` with that name, shared as "anyone with the link can view". The same name always returns the same folder. Uploaded files are shared the same way. `folder` is optional; without it, files go directly into `DRIVE_FOLDER_ID`.
- **Errors:** all errors return `{"error": "..."}`, with `400` for a bad request, `401` for a wrong key, `413` for a file that's too large, and `502` for a Drive error.

Try it with curl:
```bash
curl -s localhost:8766/health
curl -s -X POST -H 'X-Api-Key: <key>' 'http://localhost:8766/folder?name=test'
curl -s -X POST -H 'X-Api-Key: <key>' -H 'Content-Type: image/png' \
  --data-binary @some.png 'http://localhost:8766/upload?name=test.png&folder=test'
```

## Replacing the token

The token stops working **7 days after sign-in if the app is in Testing**. When the app is in
production, it stops working if you revoke access or it goes unused for about 6 months. Uploads
then fail with `invalid_grant`. To fix it:
1. Run `poetry run python main.py auth` (on any machine with a browser).
2. **Stop** `serve`, copy the new `token.json` into this folder, then **start** `serve` again.
   `serve` reads the token only when it starts.

In Testing, do this shortly before each use (the token lasts 7 days), then upload a test file to
check it works.
