# Dudgy bot integration

Design only. Nothing here is built. The bot is the Discord bot in cs-analytics; the hub is autoclipper's local service.

## What the bot is today

- Node.js with discord.js v14 (`/home/jay/projects/cs-analytics/server/package.json`).
- Lives in `server/lib/discordBot.js`. It logs in with the gateway client (`client.login`) and registers
  **guild-scoped** slash commands through REST `Routes.applicationGuildCommands` (around line 1678).
  Registration happens at startup; a failed REST registration is not retried on its own
  (`CLAUDE-HANDOFF-discord-bot-expansion-findings.md`).
- Posts replies as interaction responses and embeds. It does not use webhooks for normal output.
- Capabilities are being moved into one registry (`CLAUDE-PLAN-discord-bot-expansion.md`, `botRegistry.js`).
  New commands should be added there, not as ad hoc slash declarations, or the parity test will fail.
- Runs on jaybox in WSL as `cs-analytics.service`, published through a Cloudflare Tunnel
  (`/home/jay/projects/cs-analytics/CLAUDE.md`, line 59). The bot is therefore on the same machine as the hub.
- The gateway intents currently requested are `Guilds`, `GuildMessages`, `MessageContent`, `DirectMessages`
  (`discordBot.js` around line 1653). Reactions need `GuildMessageReactions` added. That is a bot-side change.

## 1. Posting clips: how the bot gets new kept clips

The hub's Discord poster (`src/autoclipper/discord.py`) only sends to the private test webhook. It is not the
bot's path. The bot needs a read of kept clips.

Option A: read-only JSON API on the hub.
- `GET /api/clips?since=<cursor>` on `http://127.0.0.1:4180`, returning id, title, summary, score, type,
  created_at, a local media path or URL, and a thumbnail URL.
- Cursor is the clip id or `updated_at`, so the bot keeps a last-seen value in its own store.
- The hub owns the schema. Rendered files stay under `C:\Users\jayvi\Videos\autoclipper\`; the API returns
  paths relative to that root, not absolute Windows paths.

Option B: the bot opens `~/projects/autoclipper/data/autoclipper.sqlite` read-only.
- Simpler, but it couples the bot to table names, WAL behaviour and the Windows-side file layout. Schema
  changes would break the bot without a warning.

Recommendation: **Option A.** It is a few lines in the hub's existing `BaseHTTPRequestHandler` routes
(`src/autoclipper/review.py`, `_route`), it keeps SQLite private to the hub, and it gives one place to add
auth later. The bot polls every few minutes; no push is needed.

## 2. `/clips search <query>`

- The hub adds FTS5 over titles, summaries and transcripts (`autoclipper search` plus `GET /search`).
- The bot's slash handler calls `GET /search?q=<query>&limit=5` on the hub (the same JSON shape as the API
  above) and replies with an embed per hit, with the thumbnail and a link to the review page.
- Use the registry so `/clips search` appears in help and in the LLM tool schema from one definition.
- Keep the query short (for example, 200 characters) and the result count capped.
- Defer the interaction first (`deferReply`) because the hub call can be slow on a cold start.

## 3. Reactions as taste labels

- The bot records 👍 and 👎 on posted clip messages. Other emoji are stored as raw reaction names, not
  interpreted, until we decide a mapping.
- The bot maps each message to its `candidate_id` (it stores this when it posts, from the clip feed) and
  sends `POST /label` with `candidate_id` and `label=up|down`. This matches the review page's existing route,
  which validates both fields and stores the label in `labels` (`review.py`, `do_POST`).
- Reaction removals are not labels. The bot ignores them and does not send a "neutral" value.
- Retries: the bot queues unsent labels in its own SQLite table and replays them. `/label` is idempotent for the
  same value, so a replay is safe.

How labels would tune the judge later:
- **Keep threshold.** `AUTOCLIPPER_KEEP_MIN_SCORE` (default 7 per `docs/JUDGE-COMPARISON.md`) decides which Claude
  verdicts are kept. The threshold sweep in that doc already runs against labelled data. With enough
  Discord labels, pick the threshold that maximises agreement with 👍 and 👎, and check it on held-out clips.
  Treat small samples (n = 17 today) as provisional.
- **Judge prompts.** Liked and disliked clips become few-shot examples or a rubric. Changes go through the
  judge comparison harness before any prompt is promoted. They are never applied directly.
- Labels never change the Claude call path without an eval. No automatic retraining.

## 4. Networking and security

Options for the bot-to-hub link:

1. **Same machine (recommended now).** The bot runs on jaybox; the hub listens on `127.0.0.1:4180`. No new
   exposure. The bot calls `http://127.0.0.1:4180`. Port comes from `ports get autoclipper`, not a
   hard-coded number.
2. **Tailscale.** Needed only if the bot moves off jaybox. Bind the hub to the tailnet address, never `0.0.0.0`.
3. **Cloudflare Tunnel (later, design only).** Would need a `cloudflared@autoclipper` unit, its own ingress
   rule and an explicit DNS record. Never add ingress to `cs-analytics-g4-staging`. Access policy in front
   of the tunnel is mandatory. Not needed while the bot is local.

Auth:
- Bearer token per caller. The hub already uses this shape for uploads
  (`AUTOCLIPPER_UPLOAD_TOKENS`, `src/autoclipper/config.py` around line 67: `user:token` pairs, each at least
  16 characters, kept out of `repr`).
- Give the bot its own token, read-only scope for `/api/clips` and `/search`, and a separate write scope for
  `/label`.
- Compare tokens with a constant-time check. Reject requests whose `Host` or `Origin` is not local, as
  `do_POST` already does.

Secrets:
- Tokens live in `.env` files that are git-ignored, one per repo, mode 600. Nothing goes in git or in
  `~/dotfiles`.
- The bot's Discord token and the hub token are separate values.
- Logs must not print webhook URLs or bearer tokens. `discord.py` already has a `redact` helper; reuse it.

## Open questions

- Does the bot poll the hub, or should the hub push to a bot endpoint? Polling is the proposal.
- Which guild and channel receive clip posts, and does the bot need a separate channel from the test webhook?
- Should non-👍/👎 emoji ever count as labels?
- Who owns the `botRegistry.js` entry for `/clips search`: the bot repo, which is the only one that registers commands?
- Is the bot's `/clips` command (a different feature for Tarkov or CS clips) to be renamed to avoid a clash
  with `/clips search`?
