# autoclipper

Turns gameplay replays (Escape from Tarkov, WARDOGS) into judged, trimmed, shareable clips for a small
friend group. Design: `docs/DESIGN.md`. Build plan: `PLAN.md`.

## Run

```sh
cp .env.example .env && chmod 600 .env   # fill DISCORD_TEST_WEBHOOK_URL
uv sync
uv run autoclipper --help
```

The review page serves on `http://autoclipper.localhost/` (127.0.0.1:4180, registered with `ports`).

## Develop

```sh
uv run pytest -q
uv run ruff check
```
