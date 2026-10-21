# Needs Jay

These are things the autopilot can't do alone. Each item says exactly what to do and why.



## Discord test webhook (posting is off until this is done)
`.env` doesn't exist, so `DISCORD_TEST_WEBHOOK_URL` is unset and every Discord post (P2.7 test clips, phase status lines) is skipped.
1. In a private Discord channel: Edit Channel > Integrations > Webhooks > New Webhook > Copy Webhook URL.
2. `cp ~/projects/autoclipper/.env.example ~/projects/autoclipper/.env && chmod 600 ~/projects/autoclipper/.env`
3. Paste the URL after `DISCORD_TEST_WEBHOOK_URL=`.
4. Test: `cd ~/projects/autoclipper && uv run autoclipper notify "hello from autoclipper"`.
