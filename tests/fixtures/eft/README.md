# Redacted EFT log excerpts

Source: Jay's `application_000.log` files from `C:\Battlestate Games\Escape from Tarkov\Logs`
(game version 1.2.0.0.47965, 2026-10-12). Directory and file names are the real ones, so code that picks
"the latest `log_*` directory" (names sort chronologically) can run against this folder. Line timestamps
(`yyyy-MM-dd HH:mm:ss.fff|version|Level|channel|message`) are untouched.
`extract_eft_excerpts.py` rebuilds these from the originals (read-only on the source).

| File | Lines | Contents |
|---|---|---|
| `log_2026.10.12_21-32-05_1.2.0.0.47965/... application_000.log` | 90 | One clean session: startup, `PrepareSelectedProfileLocally`/`CompleteSelectedProfile`, `Matching with group id:` (21:34:04), `TRACE-NetworkGameMatching`, `LocationLoaded`, `MatchingCompleted`, `GamePrepared`, `TRACE-NetworkGameCreate ... Location: TarkovStreets`, `GameCreated`, `GameSpawned`, `GameStarting:` (21:36:56), `GameStarted:` (21:37:08), `[Transit]`, then the return to menu at 21:42:30 (`PrepareSelectedProfileLocally`, 3 lines) and the next BattlEye cycle. Raid duration about 5 min. |
| `log_2026.10.12_20-22-21_1.2.0.0.47965/... application_000.log` | 149 | An earlier session containing: a first matching attempt that ends in `TRACE-NetworkGameCreate` only (20:23:49), a full raid (match 20:30:26, `Location: TarkovStreets`, `GameStarting:` 20:32:33, `GameStarted:` 20:32:46, menu return 20:42:49) and a second matching that goes `MatchingCompleted` (20:49:48) then `Network game matching cancelled.` (20:49:53), followed by the menu-return `PrepareSelectedProfileLocally` (20:50:05). |

## Dropped lines

Multi-line "Game settings:" / "Control settings:" blocks (keybinds), continuation lines, and repeats beyond the
first two of `GC::CollectAsync iteration` and `Reason:Stuck` lines.

## Redaction

| Original | Replacement |
|---|---|
| 24-hex profile IDs (`ProfileId:`, `Profileid:`, `[Transit]`) | `REDACTED_PROFILE_ID` |
| 24-hex raid ids inside `Sid:` | `REDACTED_RAID_ID` (rest of the Sid kept) |
| `AccountId:<n>` and `Matching with group id: <n>` (same numeric id) | `1234567` |
| `Ip: a.b.c.d` | `203.0.113.10` (RFC 5737 documentation range) |
| `shortId: XXXXXX` | `ABC123` |

No nicknames or session tokens appear in these channels. Checked with greps for any IPv4 outside
203.0.113.0/24 (the five-part game version `1.2.0.0.47965` is not an IP), any 24-hex run, and
`nick|token|password|secret|cookie|email`. `tests/test_fixtures.py::test_eft_excerpts_layout` enforces the first two.
