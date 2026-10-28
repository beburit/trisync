# TriSync

**Plan it. Sync it. Train.**

A local MCP server that turns training plans into Garmin Connect workouts, schedules them, and sends them to the right device. An optional Codex skill coordinates a timed Google Calendar event with a workout summary.

## What it does

- Running, cycling and pool-swimming workouts with warm-ups, repeats, recovery, rests and cool-downs.
- Distance, time and lap-button steps; running/cycling pace, heart-rate and power targets.
- Checks Garmin's live library and calendar before creating matching entries, including when the local ledger is missing.
- Scheduled runs/swims queue to a **Forerunner 255**; scheduled cycling workouts queue to an **Edge 530**. These are the current built-in defaults; edit `DEVICE_MODELS` in `garmin_mcp.py` to adapt them.
- A reusable `garmin-workout-calendar` skill asks when you intend to train, checks for an existing Google Calendar event, and creates a timed event containing the workout summary and Garmin link.

Device transfers are queued through Garmin; the device must sync to receive them. Google Calendar coordination is performed by the assistant through its connected Google Calendar plugin, not by a Google API client embedded in this server.

## Requirements

- Windows and Python 3.12+.
- A Garmin Connect account with the target devices registered.
- An MCP-compatible desktop client for chat-driven operation.
- A connected Google Calendar plugin if using the calendar skill.

This project uses the **unofficial** [python-garminconnect client](https://github.com/cyberjunky/python-garminconnect). Garmin's private endpoints can change. It is not affiliated with Garmin. Workout compatibility depends on the sport and device.

## Install

From the repository folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally and set `GARMIN_EMAIL` and `GARMIN_PWD`. Never paste those values into chat. The server reads this file with password interpolation disabled; process environment values take precedence. Credentials and authentication tokens are never included in tool results. Cached tokens live under `%LOCALAPPDATA%\GarminWorkoutTool\tokens`. Keep this project outside cloud-synced folders if you do not want `.env` synchronized.

No PowerShell activation script is needed: invoke the virtual environment's Python executable directly. If Garmin requires MFA, run the manual login once:

```powershell
.\.venv\Scripts\python.exe garmin_workouts.py login
```

## Connect the MCP server

For Codex, replace both paths below with your absolute paths:

```powershell
codex mcp add garmin-workouts -- "C:\path\to\trisync\.venv\Scripts\python.exe" "C:\path\to\trisync\garmin_mcp.py"
```

Reload the client to discover the tools. Other MCP clients can start the same Python script over stdio. The process waits for MCP requests; there is no HTTP port or separately managed background service.

Tools:

| Tool | Purpose |
| --- | --- |
| `garmin_preview_workouts` | Validate and preview plans offline |
| `garmin_connection_status` | Verify Garmin sign-in |
| `garmin_workout_devices` | Verify exact sport/device routes |
| `garmin_list_workouts` | Read paged workout names and IDs |
| `garmin_get_workout` | Inspect workout steps |
| `garmin_add_workouts` | Reuse/create, schedule, and queue dated workouts |

Example request:

> Add a swimming workout for tomorrow: 200 m warm-up, 4 Ã— 50 m technique, 6 Ã— 200 m easy with 25 seconds rest, 200 m cool-down. Pool length: 25 m.

The assistant converts your instructions to structured JSON; this tool does not parse arbitrary prose or PDFs by itself.

## Optional calendar skill

Copy `skills/garmin-workout-calendar` to your personal Codex skills folder, normally `~/.codex/skills/`. Reload Codex and connect Google Calendar. The skill can be selected automatically for Garmin workout requests or invoked as `$garmin-workout-calendar`.

The included skill defaults to Europe/Amsterdam, a 25 m pool and the device mapping above. Customize those preferences for your setup. It asks for missing date/time/duration, searches all event pages for that local day, reuses matching events, and asks before resolving ambiguous matches or time conflicts. Events include the workout steps and a Garmin ID marker to make later duplicate checks more reliable. This is an assistant workflow, not an unattended Google Calendar integration.

## Manual JSON workflow

```powershell
.\.venv\Scripts\python.exe garmin_workouts.py preview workouts\swimming-example.json
.\.venv\Scripts\python.exe garmin_workouts.py export workouts\swimming-example.json
.\.venv\Scripts\python.exe garmin_workouts.py upload workouts\swimming-example.json
```

Examples are illustrative, not personalized training prescriptions. Change the date before scheduling them. Manual CLI uploads retain ledger-based duplicate protection; the live duplicate checks and device delivery are provided by the MCP add tool.

```json
{
  "name": "Easy running intervals",
  "sport": "running",
  "date": "2030-01-10",
  "steps": [
    {"kind": "warmup", "seconds": 600},
    {"repeat": 4, "steps": [
      {"meters": 1000, "target": {"pace": ["5:15", "5:30"]}},
      {"kind": "recovery", "seconds": 120}
    ]},
    {"kind": "cooldown", "lap": true}
  ]
}
```

`date` is optional; without it the workout stays in the library and is not sent to a device. Swimming requires `pool_length_m` and whole-pool-length distances. Use `notes` for drills and effort instructions. Targets for running/cycling support one of `hr_zone`, `bpm`, `pace` (min:sec/km), or `watts`. Batch input is `{"workouts": [...]}`; the MCP tools take the workout array directly.

## Duplicate handling and recovery

The MCP server reads the live Garmin library, compares matching names/sports/steps, and checks the requested date before writing. It reuses an exact existing workout and prefers a copy already scheduled on that date. Same-name workouts with different content are distinct. A failed lookup stops the write. The library scan is capped at 10,000 entries and refuses unchecked creation at that limit.

The local ledger is `garmin-data/workout-upload-state.json`. It tracks workout IDs, scheduled dates and per-device sends. Keep it between runs. One Garmin account per project/ledger; don't run the legacy CLI concurrently with MCP writes.

An upload, schedule or device-send request may succeed even if its response is lost. Such writes are marked pending/uncertain and are not blindly retried. Check Connect first. Definite matching remote workouts/schedules can be reconciled on the next call. For unresolved device delivery, inspect Garmin's device queue before changing the corresponding `device_pushes` record. Never delete the ledger merely to force a retry. Batch operations can partially succeed; report Garmin creation, scheduling, device queueing and Google Calendar creation separately.

## Tests

Offline unit tests require no credentials and make no Garmin/Google writes:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_garmin_workouts.py test_garmin_mcp.py
.\.venv\Scripts\python.exe test_garmin_mcp.py --protocol
```

`--live` is an explicit, read-only integration check that signs in to Garmin, checks registered devices and searches for the example workout. It uses an isolated local ledger. Actual device-send writes are covered with mocks, not a live transfer test.

## License

MIT. See [LICENSE](LICENSE).
