"""Google Drive upload server for flutter_photobooth.

The Flutter app must not ship Google credentials, so it POSTs files here and
this server uploads them into one Drive folder, makes them viewable by anyone
with the link, and returns the link (the app shows it as a QR code).

Contract (mirrors the print server's style):

    GET  /health                       -> 200 {"ok": true}
    POST /upload?name=<file name>[&folder=<subfolder name>]
         Content-Type: image/gif | image/png | image/jpeg | ...
         [X-Api-Key: <API_KEY>]        (only if API_KEY is set)
         <raw file bytes>
      -> 201 {"id", "name", "url", "folderId", "folderUrl"}
      -> 4xx/5xx {"error": "..."}
    POST /folder?name=<subfolder name>  [X-Api-Key]
      -> 201 {"folderId", "folderUrl"}  (find-or-create, shared by link)

`folder` (e.g. the session folder name) puts the file in a subfolder of
DRIVE_FOLDER_ID, created on first use and shared by link, so one QR code can
point at every file of a guest's session (`folderUrl`).

Commands:
    python main.py auth     one-time OAuth consent in a browser -> token.json
    python main.py check    verify credentials + folder access
    python main.py serve    run the server (default)

Config (environment variables):
    DRIVE_FOLDER_ID   required: the parent Drive folder's id (from its URL)
    HOST / PORT       default 0.0.0.0 / 8766 (print server uses 8765)
    API_KEY           optional shared secret the app sends as X-Api-Key
    MAX_UPLOAD_MB     default 25
    TOKEN_FILE        path to token.json; default: next to main.py (on Render:
                      /etc/secrets/token.json, a Secret File)
    KEEP_ALIVE_URL    optional: this server's public /health URL, requested
                      every KEEP_ALIVE_MINUTES (default 10) so free hosting
                      (e.g. Render) doesn't put it to sleep
"""

import hmac
import io
import json
import os
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# drive.file: only files/folders this app created (or that the user picked).
# Enough to upload, share and list our own session folders. If you change
# scopes, delete token.json and re-run `auth`.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

HERE = os.path.dirname(os.path.abspath(__file__))
OAUTH_CLIENT_FILE = os.path.join(HERE, "oauth-client.json")
TOKEN_FILE = os.environ.get("TOKEN_FILE") or os.path.join(HERE, "token.json")

FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------- credentials


def load_credentials() -> Credentials:
  if not os.path.exists(TOKEN_FILE):
    sys.exit(f"Missing {TOKEN_FILE}: run `python main.py auth` first")
  creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
  if not creds.valid:
    creds.refresh(Request())
    save_token(creds)
  return creds


def save_token(creds, required: bool = False) -> None:
  """Write the token back to TOKEN_FILE.

  Only `auth` needs this to succeed. While serving, the refresh token never
  changes and the fresh access token lives in memory, so a read-only or
  temporary disk (e.g. a Render Secret File) is fine: log and carry on.
  """
  try:
    with open(TOKEN_FILE, "w") as token:
      token.write(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
  except OSError as error:
    if required:
      raise
    print(f"Note: couldn't write {TOKEN_FILE} ({error}); using it in memory.",
          file=sys.stderr)


def run_oauth_consent() -> None:
  from google_auth_oauthlib.flow import InstalledAppFlow

  if not os.path.exists(OAUTH_CLIENT_FILE):
    sys.exit(
        f"Missing {OAUTH_CLIENT_FILE}: create an OAuth client ID of type"
        " 'Desktop app' in Google Cloud Console and download its JSON here."
    )
  flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_FILE, SCOPES)
  creds = flow.run_local_server(port=0)
  save_token(creds, required=True)
  print(f"Saved {TOKEN_FILE}")


# ---------------------------------------------------------------------- drive


class DriveUploader:
  """Thread-safe: each call builds its own Drive client (httplib2 isn't)."""

  def __init__(self, creds: Credentials, parent_folder_id: str):
    self._creds = creds
    self._creds_lock = threading.Lock()
    self.parent_id = parent_folder_id
    self._folders: dict[str, str] = {}  # subfolder name -> id
    self._folders_lock = threading.Lock()

  def _service(self):
    with self._creds_lock:
      if not self._creds.valid:
        self._creds.refresh(Request())
        save_token(self._creds)
    return build("drive", "v3", credentials=self._creds, cache_discovery=False)

  def check(self) -> dict:
    return (
        self._service()
        .files()
        .get(
            fileId=self.parent_id,
            fields="id, name, mimeType, capabilities/canAddChildren",
            supportsAllDrives=True,
        )
        .execute()
    )

  def _share_by_link(self, service, file_id: str) -> None:
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()

  def subfolder(self, name: str) -> str:
    """Find or create (and share) subfolder `name` under the parent."""
    with self._folders_lock:
      if name in self._folders:
        return self._folders[name]
      service = self._service()
      escaped = name.replace("\\", "\\\\").replace("'", "\\'")
      found = (
          service.files()
          .list(
              q=(
                  f"'{self.parent_id}' in parents and name = '{escaped}'"
                  f" and mimeType = '{FOLDER_MIME}' and trashed = false"
              ),
              fields="files(id)",
              supportsAllDrives=True,
              includeItemsFromAllDrives=True,
          )
          .execute()
          .get("files", [])
      )
      if found:
        folder_id = found[0]["id"]
      else:
        folder_id = (
            service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": FOLDER_MIME,
                    "parents": [self.parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()["id"]
        )
        self._share_by_link(service, folder_id)
      self._folders[name] = folder_id
      return folder_id

  def upload(
      self, data: bytes, name: str, mime_type: str, folder: str | None
  ) -> dict:
    folder_id = self.subfolder(folder) if folder else self.parent_id
    service = self._service()
    media = MediaIoBaseUpload(
        io.BytesIO(data), mimetype=mime_type, resumable=len(data) > 5 << 20
    )
    created = (
        service.files()
        .create(
            body={"name": name, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    self._share_by_link(service, created["id"])
    return {
        "id": created["id"],
        "name": created["name"],
        "url": created["webViewLink"],
        "folderId": folder_id,
        "folderUrl": f"https://drive.google.com/drive/folders/{folder_id}",
    }


# --------------------------------------------------------------------- server


def make_handler(uploader: DriveUploader, api_key: str | None, max_bytes: int):
  class Handler(BaseHTTPRequestHandler):
    server_version = "DriveUploader/0.1"

    def _json(self, status: int, payload: dict) -> None:
      body = json.dumps(payload).encode()
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def do_GET(self):
      if urlparse(self.path).path == "/health":
        return self._json(HTTPStatus.OK, {"ok": True})
      self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
      url = urlparse(self.path)
      if url.path not in ("/upload", "/folder"):
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
      if api_key and not hmac.compare_digest(
          self.headers.get("X-Api-Key", "").encode(), api_key.encode()
      ):
        return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad api key"})

      query = parse_qs(url.query)
      if url.path == "/folder":
        return self._create_folder(query)
      name = (query.get("name") or [""])[0].strip()
      folder = (query.get("folder") or [""])[0].strip() or None
      if not name or "/" in name:
        return self._json(
            HTTPStatus.BAD_REQUEST, {"error": "missing or invalid ?name="}
        )

      try:
        length = int(self.headers.get("Content-Length", ""))
      except ValueError:
        return self._json(
            HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"}
        )
      if length <= 0:
        return self._json(HTTPStatus.BAD_REQUEST, {"error": "empty body"})
      if length > max_bytes:
        return self._json(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            {"error": f"over {max_bytes} bytes"},
        )
      data = self.rfile.read(length)
      mime = self.headers.get("Content-Type", "application/octet-stream")

      try:
        result = uploader.upload(data, name, mime.split(";")[0], folder)
      except HttpError as error:
        self.log_error("Drive error: %s", error)
        return self._json(
            HTTPStatus.BAD_GATEWAY, {"error": f"Drive: {error.reason}"}
        )
      except Exception as error:  # noqa: BLE001 - report, keep serving
        self.log_error("Upload failed: %r", error)
        return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
      self.log_message("Uploaded %s -> %s", name, result["url"])
      self._json(HTTPStatus.CREATED, result)

    def _create_folder(self, query: dict) -> None:
      name = (query.get("name") or [""])[0].strip()
      if not name or "/" in name:
        return self._json(
            HTTPStatus.BAD_REQUEST, {"error": "missing or invalid ?name="}
        )
      try:
        folder_id = uploader.subfolder(name)
      except HttpError as error:
        self.log_error("Drive error: %s", error)
        return self._json(
            HTTPStatus.BAD_GATEWAY, {"error": f"Drive: {error.reason}"}
        )
      except Exception as error:  # noqa: BLE001 - report, keep serving
        self.log_error("Folder failed: %r", error)
        return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
      folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
      self.log_message("Folder %s -> %s", name, folder_url)
      self._json(
          HTTPStatus.CREATED, {"folderId": folder_id, "folderUrl": folder_url}
      )

  return Handler


def make_uploader() -> DriveUploader:
  folder_id = os.environ.get("DRIVE_FOLDER_ID")
  if not folder_id:
    sys.exit("Set DRIVE_FOLDER_ID to the Drive folder id (from its URL).")
  return DriveUploader(load_credentials(), folder_id)


def keep_alive(url: str, every_seconds: float) -> None:
  """Request [url] every [every_seconds], forever (daemon thread)."""

  def loop() -> None:
    while True:
      time.sleep(every_seconds)
      try:
        with urllib.request.urlopen(url, timeout=30) as response:
          response.read()
      except Exception as error:  # noqa: BLE001 - keep trying
        print(f"Keep-alive request to {url} failed: {error}", file=sys.stderr)

  threading.Thread(target=loop, name="keep-alive", daemon=True).start()


def main(argv: list[str]) -> None:
  command = argv[1] if len(argv) > 1 else "serve"
  if command == "auth":
    return run_oauth_consent()
  if command == "check":
    uploader = make_uploader()
    print(f"Folder: {uploader.check()}")
    return
  if command != "serve":
    sys.exit(__doc__)

  uploader = make_uploader()
  host = os.environ.get("HOST", "0.0.0.0")
  port = int(os.environ.get("PORT", "8766"))
  max_bytes = int(os.environ.get("MAX_UPLOAD_MB", "25")) << 20
  handler = make_handler(uploader, os.environ.get("API_KEY"), max_bytes)
  server = ThreadingHTTPServer((host, port), handler)
  print(f"Drive uploader on http://{host}:{port}")
  if url := os.environ.get("KEEP_ALIVE_URL"):
    minutes = float(os.environ.get("KEEP_ALIVE_MINUTES", "10"))
    keep_alive(url, minutes * 60)
    print(f"Keep-alive: {url} every {minutes:g} min")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass


if __name__ == "__main__":
  main(sys.argv)
