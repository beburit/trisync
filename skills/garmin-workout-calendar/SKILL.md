---
name: garmin-workout-calendar
description: Add the user's workouts to Garmin Connect, route scheduled workouts to their Forerunner 255 or Edge 530, and arrange a timed Google Calendar event with a workout summary. Use when adding or scheduling Garmin workouts, not for recorded activity imports or general training advice.
---

# Garmin workouts and Google Calendar

The user's standing preference is to send dated runs/swims to **Forerunner 255**, dated cycling workouts to **Edge 530**, and add the planned session to **Google Calendar** after establishing when they want to do it. Use **Europe/Amsterdam** and a **25 m pool** unless they specify otherwise.

## Add or reuse the Garmin workout

- Discover `garmin-workouts` MCP tools by name when deferred. Translate the source workout into the format accepted by `garmin_preview_workouts`, then add with `garmin_add_workouts`. The user's request to add the workout authorizes this flow; don't add another terminal confirmation.
- The MCP add tool checks the live Garmin library for matching name, sport and step content, and checks the target date for that workout ID before writing. It reuses matching workouts even without a local ledger. Preserve this behavior; never bypass it by calling a raw upload endpoint. An identical name with different step content is not an exact duplicate. Inspect same-name variants or ask if replacement intent is unclear.
- Scheduled workouts automatically queue to the correct device. Check `device_delivery` in the result. `queued` means Garmin accepted a transfer request; it does not prove the device received it. Report an uncertain delivery separately from a successfully saved workout. Do not blindly retry an uncertain send or erase ledger records.
- The server keeps credentials local. Do not read `.env`, pass passwords in tool calls, or expose raw authentication errors.

## Establish the execution time

After a workout is added or reused, ask when the user wants to do it if their intended date and start time are not already explicit. Ask for duration/end time in the same question when it is unknown. A Garmin calendar date alone does not specify a Google Calendar start time. Use an explicit planned duration from the source; time-based steps can establish duration, but distances, lap-ended steps and unspecified rests cannot establish an exact duration without a pace/estimate. If proposing a duration estimate, have the user accept it rather than silently treating it as exact.

If the request already includes date, start time and duration/end time, use those details without asking again. Never convert a known timed session to all-day, invent a start time, or treat an availability window as the workout's duration. For batches, establish timing for each session together when practical. A workout added only to the library can remain unscheduled until the user supplies a date; then call the MCP add tool again with that date to reuse and schedule it.

## Prevent Google Calendar duplicates and create the event

Use the connected **Google Calendar** plugin tools; no Google password is needed by the Garmin MCP server. The user's standing request authorizes creating their workout event once its timing is known. Use their primary calendar unless they name another calendar.

1. Before creating, search that calendar with a bounded window covering the intended local day (RFC3339 bounds with the correct Amsterdam UTC offset). Read all result pages. Search without a keyword first so differently titled events are visible; inspect candidate descriptions with a read-event tool if the search response omits them.
2. Identify an existing workout event using the Garmin workout URL/ID and local session date. Also check for the same sport/name, timing and workout summary when a manually created event has no Garmin link. An identical description or URL is stronger evidence than a generic title such as "Swimming". Treat repeats on different dates as separate sessions. If the user explicitly wants a second session of the same workout on the same day, distinguish it by its intended time.
3. Reuse an event already representing this planned session and return its link; do not create a second event. If it exists at another time, explain the existing time and ask whether to move it unless the user already authorized rescheduling. Resolve ambiguous candidates before writing. If search fails, stop the Google write; do not assume no duplicate exists.
4. Check the intended slot against returned events or the availability tool. Report conflicts and ask which time they prefer; do not silently move their workout. Do not count a reused workout event as a new conflict.
5. Create a timed event with `start_time`, `end_time` and `timezone_str: Europe/Amsterdam` (not all-day dates), `attendees: []`, no Meet link. Title: sport plus the workout name. Description: a readable summary of warm-up, main sets/repeats, rests, targets, cool-down, total distance when known, pool length for swims, target Garmin device, and the Garmin workout URL. Include a stable marker such as `Garmin workout: <ID>; local session date: YYYY-MM-DD` to support later duplicate checks. Never infer that an event was created from an attempted call; use the returned result/link.
6. After an uncertain create response, search the same bounded window/marker before any retry. Do not reupload the Garmin workout because the Google action failed.

Report Garmin scheduling, device queue status, and Google Calendar time/link separately. If timing is still pending, say the Garmin workout is saved and ask for the missing scheduling details. Do not claim Google Calendar was updated before the event exists.

Implementation: `garmin_mcp.py` in the TriSync repository. Setup and maintenance notes: the repository README.md.
