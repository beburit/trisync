"""Local stdio MCP server. Credentials are read locally and never returned."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.mcp-packages'))
sys.path.insert(0, str(ROOT / '.workout-packages'))
if os.name == 'nt' and (ROOT / '.mcp-packages' / 'pywin32_system32').is_dir():
    # pip --target does not process pywin32.pth; supply its paths explicitly.
    for relative in ('win32', 'win32/lib', 'pywin32_system32'):
        sys.path.insert(0, str(ROOT / '.mcp-packages' / relative))
    _dll_directory = os.add_dll_directory(str(ROOT / '.mcp-packages' / 'pywin32_system32'))

from dotenv import dotenv_values
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from garmin_workouts import compile_workout, preview, publish, save_state

logging.disable(logging.CRITICAL)
TOKENS = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GarminWorkoutTool' / 'tokens'
STATE = ROOT / 'garmin-data' / 'workout-upload-state.json'
_thread_lock = threading.Lock()
DEVICE_MODELS = {'swimming': 'Forerunner 255', 'running': 'Forerunner 255', 'cycling': 'Edge 530'}

mcp = FastMCP('garmin-workouts', instructions=(
    'Create Garmin workouts only when the user asks to add/upload a workout or plan. '
    'Translate their workout into the structured plan format and preview it before adding. '
    'Use a 25 m pool for this user unless they specify another length. '
    'Never request or expose Garmin credentials; the server reads its local .env. '
    'A date schedules the workout in Garmin Connect, not Google Calendar. '
    'Report the returned Garmin workout links and whether scheduling succeeded. '
    'Scheduled swimming/running workouts are queued to the Forerunner 255; cycling to the Edge 530. '
    'After adding, ask when the user will perform the workout unless date/time are already specified; '
    'then use the connected Google Calendar tools to add a timed event with the workout summary and Garmin link. '
    'Use Europe/Amsterdam and ask for duration if it cannot be derived. Do not invent a start time. '
    'Do not retry uncertain writes; inspect existing workouts first.'
))


@contextlib.contextmanager
def operation_lock():
    """Coordinate MCP processes; always release the OS lock on process exit."""
    import msvcrt
    with _thread_lock:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with (STATE.parent / 'workout-upload.lock').open('a+b') as handle:
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ToolError('Another Garmin operation is running. Wait for it to finish.') from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def credentials():
    # interpolation=False preserves passwords containing ${...}; do not log this dict.
    values = dotenv_values(ROOT / '.env', interpolate=False)
    email = os.environ.get('GARMIN_EMAIL') or values.get('GARMIN_EMAIL')
    password = os.environ.get('GARMIN_PWD') or values.get('GARMIN_PWD')
    if not email or not password:
        raise ToolError('Set GARMIN_EMAIL and GARMIN_PWD in the local project .env file.')
    return email, password


def connect():
    from garminconnect import Garmin
    email, password = credentials()
    needs_mfa = False

    def mfa():
        nonlocal needs_mfa
        needs_mfa = True
        raise RuntimeError('interactive_mfa_required')

    api = Garmin(email=email, password=password, prompt_mfa=mfa, retry_attempts=0)
    try:
        # Both library output and legacy print statements must stay off MCP stdout.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            api.login(str(TOKENS))
    except Exception:
        if needs_mfa:
            raise ToolError('Garmin requires MFA. Run Garmin-Workouts.cmd login locally once, then retry.') from None
        raise ToolError('Garmin sign-in failed. Check the local credentials, saved session and network. No credentials were returned.') from None
    finally:
        password = None
        api.password = None
    return api


def validated(workouts):
    if not isinstance(workouts, list) or not 1 <= len(workouts) <= 100:
        raise ToolError('Provide 1–100 workouts per call.')
    try:
        return [(plan, compile_workout(plan)) for plan in workouts]
    except Exception:
        raise ToolError('Invalid workout. Check name, sport, YYYY-MM-DD date, pool length, steps, durations and targets. Use garmin_preview_workouts to validate the corrected plan.') from None


def resolve_devices(api, sports):
    """Resolve exact models, never use Garmin's last-used-device fallback."""
    if not sports:
        return {}
    try:
        devices = api.get_devices()
    except Exception:
        raise ToolError('Could not read Garmin devices. No workouts were changed.') from None
    routes = {}
    for sport in sports:
        model = DEVICE_MODELS[sport]
        pattern = r'(?<!\w)' + r'\s*'.join(re.escape(part) for part in model.split()) + r'(?!\d)'
        matches = {}
        for device in devices:
            # Prefer product model over a user's custom nickname.
            label = device.get('productDisplayName') or device.get('displayName') or ''
            if re.search(pattern, label, re.IGNORECASE) and device.get('deviceId'):
                matches[str(device['deviceId'])] = {'id': device['deviceId'], 'name': model}
        if len(matches) != 1:
            raise ToolError(f'Expected one {model} in Garmin, found {len(matches)}. No workouts were changed; check the registered devices.')
        routes[sport] = next(iter(matches.values()))
    return routes


def queue_device(api, record, device, state):
    pushes = record.setdefault('device_pushes', {})
    key = str(device['id'])
    previous = pushes.get(key, {})
    if previous.get('status') == 'queued':
        return {'device': device['name'], 'status': 'already_queued'}
    if previous.get('status') in ('pending', 'uncertain'):
        return {'device': device['name'], 'status': 'uncertain',
                'message': 'A previous device-send result is uncertain. Check the Garmin device queue; automatic resend is blocked.'}
    pushes[key] = {'device': device['name'], 'status': 'pending'}
    save_state(STATE, state)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            api.push_workout_to_device(workout_id=record['id'], device_id=device['id'])
    except Exception:
        pushes[key]['status'] = 'uncertain'
        save_state(STATE, state)
        return {'device': device['name'], 'status': 'uncertain',
                'message': 'Workout is saved in Connect, but device-send result is uncertain. Check Connect before resending.'}
    pushes[key]['status'] = 'queued'
    save_state(STATE, state)
    return {'device': device['name'], 'status': 'queued',
            'message': 'Garmin accepted the transfer request; device must sync to receive it.'}


def workout_signature(workout):
    """Compare workout behavior, ignoring Garmin-generated IDs and display metadata."""
    def step(s):
        out = {key: s.get(key) for key in ('type', 'description', 'endConditionValue',
               'numberOfIterations', 'targetValueOne', 'targetValueTwo', 'zoneNumber',
               'secondaryTargetValueOne', 'secondaryTargetValueTwo', 'secondaryZoneNumber')}
        for key, id_key in (('stepType', 'stepTypeId'), ('endCondition', 'conditionTypeId'),
                            ('targetType', 'workoutTargetTypeId'), ('secondaryTargetType', 'workoutTargetTypeId'),
                            ('strokeType', 'strokeTypeId'), ('equipmentType', 'equipmentTypeId')):
            value = (s.get(key) or {}).get(id_key)
            out[key] = value or None
        out['skipLastRestStep'] = bool(s.get('skipLastRestStep'))
        if 'workoutSteps' in s:
            out['workoutSteps'] = [step(child) for child in s['workoutSteps']]
        return out
    return {'name': workout.get('workoutName', '').strip().casefold(),
            'sport': (workout.get('sportType') or {}).get('sportTypeId'),
            'pool': workout.get('poolLength'),
            'segments': [[step(s) for s in seg.get('workoutSteps', [])]
                         for seg in workout.get('workoutSegments', [])]}


def signatures_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(signatures_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(signatures_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
    return a == b


def reconcile_existing(api, items):
    """Read Garmin before writes, so a missing ledger cannot create duplicate entries."""
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    entries = []
    for start in range(0, 10000, 100):
        page = api.get_workouts(start=start, limit=100)
        if not isinstance(page, list):
            raise ValueError('Unrecognized Garmin workout list')
        entries.extend(page)
        if len(page) < 100:
            break
    else:
        raise ValueError('Duplicate scan limit reached; refusing to create unchecked workouts')
    schedules = {}
    for when in {plan['date'] for plan, _ in items if plan.get('date')}:
        schedule = api.get_scheduled_workouts_by_period(when, when)
        if not isinstance(schedule, list):
            raise ValueError('Unrecognized Garmin schedule list')
        schedules[when] = {str((item.get('workout') or {}).get('workoutId') or item.get('workoutId')) for item in schedule}
    details = {}
    for plan, payload in items:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        record = state.get(digest, {})
        signature = workout_signature(payload)
        matches = []
        for item in entries:
            if item.get('workoutName', '').strip().casefold() != signature['name']:
                continue
            wid = item.get('workoutId')
            if wid not in details:
                details[wid] = api.get_workout_by_id(wid)
            if signatures_equal(signature, workout_signature(details[wid])):
                matches.append(wid)
        if 'id' in record and record['id'] not in matches:
            raise ValueError('Previously tracked workout is missing or changed in Garmin; inspect it before creating a replacement')
        if matches:
            when = plan.get('date')
            dated_matches = [wid for wid in matches if when and str(wid) in schedules[when]]
            selected = dated_matches[0] if dated_matches else record.get('id', matches[0])
            if record.get('id') and record['id'] != selected:
                record = {}  # Adopt the already-scheduled copy, not a second identical template.
            record.update(id=selected, name=payload['workoutName'], pending=False)
            record.setdefault('dates', [])
            if when:
                if str(record['id']) in schedules[when]:
                    if when not in record['dates']:
                        record['dates'].append(when)
                    if record.get('pending_date') == when:
                        record.pop('pending_date')
                elif when in record['dates']:
                    record['dates'].remove(when)
            state[digest] = record
    save_state(STATE, state)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def garmin_workout_devices() -> dict:
    """Check the exact sport-to-device routes without sending workouts."""
    with operation_lock():
        routes = resolve_devices(connect(), set(DEVICE_MODELS))
    return {'routes': routes}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def garmin_preview_workouts(workouts: list[dict[str, Any]]) -> dict:
    """Validate and preview workouts without contacting Garmin.

    Each workout: {name: str, sport: running|cycling|swimming, date?: YYYY-MM-DD,
    description?: str, pool_length_m?: number (required for swimming), steps: [...]}.
    Each step: {kind?: warmup|cooldown|interval|recovery|rest|main,
    exactly one of meters:number / seconds:number / lap:true,
    notes?:str, stroke?:any_stroke|free (swimming only),
    target?: {hr_zone:1..5} OR {bpm:[low,high]} OR {pace:["5:15","5:30"]}
    OR {watts:[low,high]}}. Targets are running/cycling only; pace is min:sec/km.
    Repeats: {repeat:2..99, steps:[...]}. Swim distances must be whole pool lengths.
    A date schedules on Garmin's calendar; omit date to add only to the library.
    """
    items = validated(workouts)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        preview(items)
    return {'valid': True, 'preview': stream.getvalue(),
            'garmin_payloads': [payload for _, payload in items]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def garmin_connection_status() -> dict:
    """Verify local credentials and a live Garmin sign-in without creating workouts."""
    with operation_lock():
        connect()
    return {'connected': True, 'credentials_source': 'local environment or project .env',
            'default_pool_length_m': 25}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def garmin_list_workouts(start: int = 0, limit: int = 25) -> dict:
    """List existing Garmin workouts to find IDs and inspect possible duplicates. Pages of up to 100."""
    if isinstance(start, bool) or isinstance(limit, bool) or start < 0 or not 1 <= limit <= 100:
        raise ToolError('start must be nonnegative and limit must be 1–100.')
    with operation_lock():
        api = connect()
        try:
            data = api.get_workouts(start=start, limit=limit)
            return {'workouts': [{k: item.get(k) for k in ('workoutId', 'workoutName', 'sportType')}
                                 for item in data], 'start': start, 'limit': limit}
        except Exception:
            raise ToolError('Could not list Garmin workouts. Check the connection and try again later.') from None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def garmin_get_workout(workout_id: int) -> dict:
    """Read a Garmin workout's steps by ID to verify an upload. Does not modify it."""
    if workout_id <= 0:
        raise ToolError('workout_id must be positive.')
    with operation_lock():
        api = connect()
        try:
            data = api.get_workout_by_id(workout_id)
            # Do not return author/account information contained in Garmin's full response.
            return {k: data.get(k) for k in ('workoutId', 'workoutName', 'description', 'sportType',
                                           'poolLength', 'poolLengthUnit', 'workoutSegments')}
        except Exception:
            raise ToolError('Could not read this Garmin workout. Check its ID and the connection.') from None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=True))
def garmin_add_workouts(workouts: list[dict[str, Any]]) -> dict:
    """Create and optionally schedule user-requested workouts in Garmin Connect.

    Use the workout format documented by garmin_preview_workouts. Validate/preview first.
    User instructions to add a workout authorize this write. Existing identical uploads
    in this server's ledger are reused; new dates are scheduled once. Changed content
    creates a new workout. Dated swims/runs are sent to the Forerunner 255; dated
    bike workouts to the Edge 530. Library-only workouts are not sent. Device receipt
    requires sync. No existing workout is edited or deleted. On an uncertain
    write this tool stops; inspect Connect instead of blindly retrying.
    """
    items = validated(workouts)  # validate the entire batch before any network or write
    with operation_lock():
        api = connect()
        routes = resolve_devices(api, {plan['sport'] for plan, _ in items if plan.get('date')})
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                reconcile_existing(api, items)
                publish(items, api, STATE)
            state = json.loads(STATE.read_text(encoding='utf-8'))
            results = []
            for plan, payload in items:
                key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                record = state[key]
                delivery = queue_device(api, record, routes[plan['sport']], state) if plan.get('date') else {'status': 'not_requested'}
                summary = io.StringIO()
                with contextlib.redirect_stdout(summary):
                    preview([(plan, payload)])
                results.append({'workout_id': record['id'], 'name': payload['workoutName'],
                                'scheduled_dates': record['dates'],
                                'requested_date': plan.get('date'),
                                'url': f"https://connect.garmin.com/modern/workout/{record['id']}",
                                'device_delivery': delivery, 'calendar_summary': summary.getvalue()})
            return {'success': True, 'workouts': results,
                    'device_delivery_complete': all(r['device_delivery']['status'] != 'uncertain' for r in results),
                    'google_calendar_next_step': 'Ask for intended date, start time and duration if not already known, then create a Google Calendar event with the summary and Garmin link. Timezone: Europe/Amsterdam.'}
        except Exception:
            raise ToolError('Upload or scheduling did not finish. Earlier items may have succeeded. Check Connect and the local workout-upload-state.json ledger before retrying; uncertain writes are blocked automatically.') from None


if __name__ == '__main__':
    mcp.run(transport='stdio')
