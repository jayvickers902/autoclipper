# Hub uploader (P3.4)

Recorders (the Segra fork in `C:\projects\autoclipper-recorder`) send recordings to the hub instead of segra.tv.
The hub side is `src/autoclipper/uploads.py`; the recorder side is `Backend/Media/HubUploadClient.cs` (protocol)
and `Backend/Media/UploadService.cs` (Segra glue).

## Choices

- **Transport now: plain HTTP to the hub over LAN or Tailscale.** No relay, no cloud, $0. Tailscale is
  preferred: it encrypts, and it works for friends outside the house. On the bare LAN the token travels in
  clear text, which is acceptable only inside Jay's home network.
- **Resumable chunks keyed by content hash** (a small tus-like protocol) rather than one big POST: an hour-long
  AV1 session is several GB, and a dropped Wi-Fi link should not restart it from zero. The SHA-256 names the
  upload, so every request is idempotent and a finished upload is never stored twice.
- **Later: presigned Cloudflare R2 PUTs** (design only, below). Nothing is created now.

## Protocol (v1)

Every request carries `Authorization: Bearer <token>`. Requests with an `Origin` header are refused (403), so a
web page in a browser cannot drive the receiver. Replies are JSON: `{"state": "partial"|"complete", "offset": n}`
or `{"error": "...", "offset"?: n}`.

| Request | Meaning |
| --- | --- |
| `POST /v1/uploads` + metadata JSON (≤ 1 MiB) | Create, or resume: answers the byte offset the hub already has. Also the re-sync step after any failure. |
| `PUT /v1/uploads/<sha256>?offset=n` + chunk (≤ 64 MiB, `Content-Length` required) | Append at `n`. A wrong `n` gets 409 with the hub's offset. The chunk that reaches `size` triggers the SHA-256 check and the move into the inbox. |

Status codes: 400 bad metadata or a chunk past `size`; 401 bad token; 404 PUT before POST; 409 offset or size
conflict; 413 file over the size limit or chunk too big; 422 hash mismatch (the staged bytes are discarded;
the recorder reports "the file changed during the upload"); 507 not enough disk space.

Recorder behaviour (`HubUploadClient.UploadAsync`):

1. Hash the file (SHA-256, streamed), then POST the metadata.
2. Send 32 MiB chunks from the offset the hub reports.
3. Network errors, timeouts, 408, 429 and 5xx are retried with backoff (2, 4, 8 … 60 s). After a failure the
   recorder always re-POSTs to learn the hub's offset before sending more, so a chunk that landed but whose reply
   was lost is never appended twice. The upload gives up after 6 consecutive failures; any chunk that gets
   through resets the count. A 409 jumps to the hub's offset and is not counted as a failure.
4. 400, 401, 404, 413, 422 and 507 fail at once, with a modal in the recorder.

Uploading the same file again finishes after the POST (state `complete`) without sending any bytes.

## Metadata

The POST body, stored as `meta.json` next to the video:

| Field | Source in Segra |
| --- | --- |
| `sha256`, `size`, `filename` | the file (filename reduced to a safe basename; `.mp4 .mkv .mov .webm` only) |
| `contentType`, `contentId` | `Content.Type` (`Session`, `Buffer`, `Clip`, `Highlight`) and its id |
| `game`, `igdbId`, `gameExe` | `Content.Game`, `IgdbId`, last two parts of the exe path (never the full path) |
| `title`, `description` | the upload dialog |
| `createdAt` | `Content.CreatedAt` as local time with UTC offset (for a session: when Segra wrote the metadata, i.e. after recording stopped) |
| `startedAt` | wall clock of the first frame: for a session the file's creation time when it is within 5 min of `createdAt - duration`, else `createdAt - duration`; for a buffer `createdAt - duration`; null for clips and highlights |
| `durationSeconds` | `Content.Duration` |
| `bookmarks` | `[{type, subtype, timeSeconds}]` sorted by time: Segra's Kill/Death/Manual bookmarks, including the Tarkov raid start/end bookmarks (P3.2) |
| `audioTrackNames`, `audioTrackTypes` | track layout (P3.3: Full Mix, mic(s), Game Audio, Voice Chat); pick tracks by name/type, not index |
| `recorder` | `autoclipper-recorder/<version>` |

Times carry the recorder's UTC offset, so the hub can line up EFT logs, which are in local time.
The hub adds `user` (from the token), `file` and `received_at` (Unix time).

## Where files land on the hub

- Inbox root: `AUTOCLIPPER_UPLOAD_INBOX`, default `data/inbox` (ext4, so renames are atomic and fast).
- In progress: `<inbox>/.staging/<user>/<sha256>/{meta.json,data.part}`.
- Finished: `<inbox>/<user>/<sha256>/{<filename>,meta.json}`. The whole directory appears in one `rename`, so the
  ingest step can treat any `<inbox>/<user>/<sha256>/` directory as complete and must skip `.staging`.
- The ingest step reads uploads like replays (read-only originals). Cleaning the inbox is a separate, manual
  decision, like deleting recordings.

## Limits

- File: `AUTOCLIPPER_UPLOAD_MAX_GB` (default 64). The hub also refuses a new upload when free space would drop
  under 2 GiB (507).
- Chunk: 64 MiB on the hub; the recorder sends 32 MiB. Metadata: 1 MiB.
- Requests time out after 10 minutes on the recorder (one chunk per request).

## Configuration

Hub (`.env`, never in git; see `.env.example`):

```
AUTOCLIPPER_UPLOAD_PORT=          # empty = receiver off (the default)
AUTOCLIPPER_UPLOAD_HOST=127.0.0.1 # set to the Tailscale or LAN address to accept recorders
AUTOCLIPPER_UPLOAD_TOKENS=jay:<token>,friend:<token>
AUTOCLIPPER_UPLOAD_INBOX=         # default data/inbox
AUTOCLIPPER_UPLOAD_MAX_GB=64
```

Enabling it:

1. `ports claim autoclipper-upload` and put that port in `AUTOCLIPPER_UPLOAD_PORT`. Never pick a port by hand.
2. Bind `AUTOCLIPPER_UPLOAD_HOST` to the Tailscale address (with mirrored networking WSL sees the Windows
   interfaces). `0.0.0.0` also exposes it on the LAN.
3. Generate one token per person (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), at least
   16 chars. Usernames are `[a-z0-9_-]{1,32}` and name the inbox subdirectory.
4. Allow the port inbound in the Windows (Hyper-V) firewall, on the Tailscale interface only.
5. Run it: `uv run python -m autoclipper.uploads` for now; a CLI subcommand and a systemd unit
   (`autoclipper-upload`) come with the wiring.

Recorder (`%APPDATA%\Segra\settings.json`, edited while Segra is closed; no UI yet):

```json
"hubUploadUrl": "http://<tailscale-ip>:<port>/",
"hubUploadToken": "<that person's token>"
```

With either one empty, uploading is off: the Upload buttons are hidden and the upload path makes no network
calls. The Upload menu item works for every content type, sessions included.

## segra.tv features disabled in the upload path

- `processing.segra.tv/upload` (the upload itself) is gone; uploads go only to `hubUploadUrl`.
- The stale-link check (`segra.tv/api/user/videos` after login) is a no-op.
- No segra.tv login is needed to upload (the dialog no longer asks for one).
- "Open in browser after upload" and the Visibility choice do nothing (the hub has no public page); the upload
  stores no `UploadUrl`.

Still calling segra.tv outside the upload path (not changed by P3.4): login (`AuthService`,
`DiscordLoginService`), the `games.json` catalog and blacklist CDN, OBS version list, HEVC extension download,
and game covers/icons in the UI. Airplane mode turns most of them off; it also turns uploading off.

## Later: presigned Cloudflare R2 PUT (design only)

For friends who can't reach the hub over Tailscale. Cloudflare tunnels reject request bodies over 100 MB on the
free plan, so video must go straight to R2:

1. The recorder POSTs the same metadata to a small control endpoint (a Worker or the hub through a tunnel; the
   bodies are tiny) with its token.
2. The endpoint starts an S3 multipart upload in a dedicated bucket under `inbox/<user>/<sha256>/` and answers
   presigned `UploadPart` URLs (parts of 32–64 MiB, URLs valid for about an hour; it re-issues them on request,
   which keeps the upload resumable).
3. The recorder PUTs the parts directly to R2, then asks the endpoint to complete the upload.
4. The hub polls the bucket, verifies the SHA-256, moves the files into the same inbox layout as above and
   deletes the R2 object.

Cost: the account's free 10 GB of R2 storage is already used up by `fragvid` (about 40 GB), so every byte held in
R2 is billed. The hub must delete objects as soon as it has them, and this needs Jay's approval before anything
is created.
