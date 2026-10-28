"""Compile simple training-plan JSON and upload to Garmin Connect. Python 3.12+."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.workout-packages'))
SPORTS = {'running': 1, 'cycling': 2, 'swimming': 4}
KINDS = {'warmup': 1, 'cooldown': 2, 'interval': 3, 'recovery': 4, 'rest': 5, 'main': 8}


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be a positive finite number')
    return value


def keys(obj, allowed, label):
    if not isinstance(obj, dict):
        raise ValueError(f'{label} must be an object')
    unknown = set(obj) - set(allowed)
    if unknown:
        raise ValueError(f'{label}: unknown fields {sorted(unknown)}')


def enum(prefix, ident, key):
    return {prefix + 'Id': ident, prefix + 'Key': key, 'displayOrder': ident}


def pace_seconds(value):
    parts = str(value).split(':')
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[1]) >= 60:
        raise ValueError('Pace must be minutes:seconds per km, e.g. 5:30')
    return number(int(parts[0]) * 60 + int(parts[1]), 'pace')


def compile_workout(plan):
    keys(plan, ['name', 'sport', 'date', 'description', 'pool_length_m', 'steps'], 'workout')
    name = plan.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('name must contain 1–100 characters')
    sport = plan.get('sport')
    if sport not in SPORTS:
        raise ValueError('sport must be running, cycling or swimming')
    if plan.get('date'):
        if date.fromisoformat(plan['date']).isoformat() != plan['date']:
            raise ValueError('date must be YYYY-MM-DD')
    pool = None
    if sport == 'swimming':
        pool = number(plan.get('pool_length_m'), 'pool_length_m')
    elif 'pool_length_m' in plan:
        raise ValueError('pool_length_m is only valid for swimming')
    order = 0

    def build(items, depth=0, parent=None):
        nonlocal order
        if not isinstance(items, list) or not items:
            raise ValueError('steps must be a non-empty array')
        if depth > 3:
            raise ValueError('At most three nested repeat groups are supported')
        result = []
        for item in items:
            keys(item, ['repeat', 'steps', 'kind', 'meters', 'seconds', 'lap', 'notes', 'target', 'stroke'], 'step')
            order += 1
            if order > 200:
                raise ValueError('At most 200 step definitions are supported')
            current = order
            if 'repeat' in item:
                keys(item, ['repeat', 'steps'], 'repeat')
                count = item['repeat']
                if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 99:
                    raise ValueError('repeat must be an integer from 2 to 99')
                step = {'type': 'RepeatGroupDTO', 'stepOrder': current,
                        'stepType': enum('stepType', 6, 'repeat'),
                        'numberOfIterations': count, 'endCondition': enum('conditionType', 7, 'iterations'),
                        'endConditionValue': count, 'smartRepeat': False,
                        'workoutSteps': build(item.get('steps'), depth + 1, current)}
            else:
                if 'steps' in item:
                    raise ValueError('steps requires repeat')
                kind = item.get('kind', 'interval')
                if kind not in KINDS:
                    raise ValueError(f'Unknown step kind: {kind}')
                duration = [k for k in ('meters', 'seconds', 'lap') if k in item]
                if len(duration) != 1:
                    raise ValueError('Each step needs exactly one of meters, seconds or lap')
                duration = duration[0]
                value = item[duration]
                if duration == 'lap':
                    if value is not True:
                        raise ValueError('lap must be true')
                    condition, ident, value = 'lap.button', 1, None
                else:
                    value = number(value, duration)
                    condition, ident = ('distance', 3) if duration == 'meters' else ('time', 2)
                    if pool and duration == 'meters' and not math.isclose(value / pool, round(value / pool)):
                        raise ValueError('Swim distances must be whole pool lengths')
                    if pool and duration == 'seconds' and kind == 'rest':
                        condition, ident = 'fixed.rest', 8
                step = {'type': 'ExecutableStepDTO', 'stepOrder': current,
                        'stepType': enum('stepType', KINDS[kind], kind),
                        'endCondition': enum('conditionType', ident, condition),
                        'targetType': enum('workoutTargetType', 1, 'no.target')}
                if value is not None:
                    step['endConditionValue'] = value
                if duration == 'meters':
                    step['preferredEndConditionUnit'] = {'unitKey': 'meter'}
                if 'notes' in item:
                    if not isinstance(item['notes'], str) or len(item['notes']) > 500:
                        raise ValueError('notes must be text of at most 500 characters')
                    step['description'] = item['notes']
                if pool and kind != 'rest':
                    stroke = item.get('stroke', 'any_stroke')
                    strokes = {'any_stroke': 1, 'free': 6}
                    if stroke not in strokes:
                        raise ValueError('Supported strokes: any_stroke, free')
                    step['strokeType'] = enum('strokeType', strokes[stroke], stroke)
                elif 'stroke' in item:
                    raise ValueError('stroke is only valid on active swimming steps')
                if 'target' in item:
                    target = item['target']
                    if pool:
                        raise ValueError('For swimming, put effort/pace instructions in notes')
                    keys(target, ['hr_zone', 'bpm', 'pace', 'watts'], 'target')
                    if len(target) != 1:
                        raise ValueError('target requires exactly one of hr_zone, bpm, pace or watts')
                    key, val = next(iter(target.items()))
                    tid, tkey = {'hr_zone': (4, 'heart.rate.zone'), 'bpm': (4, 'heart.rate.zone'),
                                 'pace': (6, 'pace.zone'), 'watts': (2, 'power.zone')}[key]
                    step['targetType'] = enum('workoutTargetType', tid, tkey)
                    if key == 'hr_zone':
                        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 5:
                            raise ValueError('hr_zone must be 1–5')
                        step['zoneNumber'] = val
                    else:
                        if not isinstance(val, list) or len(val) != 2:
                            raise ValueError(f'{key} requires [lower, upper] bounds')
                        if key == 'pace':
                            bounds = sorted(1000 / pace_seconds(v) for v in val)
                        else:
                            bounds = [number(v, key) for v in val]
                            if bounds[0] > bounds[1]:
                                raise ValueError(f'{key} bounds must be ascending')
                        step['targetValueOne'], step['targetValueTwo'] = bounds
            if parent is not None:
                step['childStepId'] = parent
            result.append(step)
        return result

    stype = enum('sportType', SPORTS[sport], sport)
    payload = {'workoutName': name.strip(), 'sportType': stype,
               'description': plan.get('description', ''), 'estimatedDurationInSecs': 0,
               'workoutSegments': [{'segmentOrder': 1, 'sportType': stype, 'workoutSteps': build(plan.get('steps'))}]}
    if pool:
        payload.update(poolLength=pool, poolLengthUnit={'unitKey': 'meter'})
    return payload


def load_plans(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(raw, dict) and 'workouts' in raw:
        keys(raw, ['workouts'], 'plan')
        plans = raw['workouts']
    else:
        plans = [raw]
    if not isinstance(plans, list) or not plans:
        raise ValueError('workouts must be a non-empty array')
    return [(p, compile_workout(p)) for p in plans]


def preview(items):
    def show(steps, indent='  '):
        for s in steps:
            if s['type'] == 'RepeatGroupDTO':
                print(f"{indent}Repeat {s['numberOfIterations']} times:")
                show(s['workoutSteps'], indent + '  ')
            else:
                condition = s['endCondition']['conditionTypeKey']
                unit = 'm' if condition == 'distance' else 's'
                duration = 'lap button' if condition == 'lap.button' else f"{s['endConditionValue']:g} {unit}"
                target = s['targetType']['workoutTargetTypeKey']
                detail = f" zone {s['zoneNumber']}" if 'zoneNumber' in s else ''
                if 'targetValueOne' in s:
                    detail = f" {s['targetValueOne']:g}–{s['targetValueTwo']:g} (m/s for pace)"
                print(f"{indent}{s['stepType']['stepTypeKey']}: {duration}; {target}{detail}" +
                      (f"; {s['description']}" if s.get('description') else ''))
    for plan, payload in items:
        print(f"\n{payload['workoutName']} | {plan['sport']} | {plan.get('date', 'unscheduled')}")
        if 'poolLength' in payload:
            print(f"Pool: {payload['poolLength']:g} m")
        show(payload['workoutSegments'][0]['workoutSteps'])


def login(tokens, fresh=False):
    try:
        from garminconnect import Garmin, GarminConnectAuthenticationError
    except ImportError:
        raise ValueError('Install dependencies first: .\\Garmin-Workouts.cmd setup') from None
    logging.disable(logging.CRITICAL)
    if not fresh:
        api = Garmin(retry_attempts=0)
        try:
            api.login(str(tokens))
            return api
        except (GarminConnectAuthenticationError, FileNotFoundError):
            raise ValueError('No usable session. Run .\\Garmin-Workouts.cmd login in your terminal.') from None
    api = Garmin(email=input('Garmin email: ').strip(), password=getpass('Garmin password: '),
                 prompt_mfa=lambda: input('Garmin MFA code: ').strip(), retry_attempts=0)
    api.login(str(tokens))
    return api


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    tmp.replace(path)


def publish(items, api, state_path):
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    for plan, payload in items:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        record = state.get(digest, {})
        # Persist an intent before POST. An ambiguous failure must be checked in Connect,
        # rather than retried automatically and potentially creating a duplicate.
        if record.get('pending'):
            raise ValueError(f"Uncertain previous upload: {payload['workoutName']}. Check Garmin and the state file before retrying.")
        if 'id' not in record:
            record = {'pending': True, 'name': payload['workoutName'], 'dates': []}
            state[digest] = record
            save_state(state_path, state)
            response = api.upload_workout(payload)
            wid = response.get('workoutId') if isinstance(response, dict) else None
            if not wid:
                raise ValueError('Garmin did not return a workout ID. Check Connect before retrying.')
            record.update(id=wid, pending=False)
            save_state(state_path, state)
            print(f"Created: {payload['workoutName']} (ID {wid})")
        else:
            print(f"Already uploaded: {payload['workoutName']} (ID {record['id']})")
        when = plan.get('date')
        if record.get('pending_date'):
            raise ValueError('Uncertain previous scheduling request. Check the Garmin calendar and state file before retrying.')
        if when and when not in record['dates']:
            record['pending_date'] = when
            save_state(state_path, state)
            api.schedule_workout(record['id'], when)
            record['dates'].append(when)
            record.pop('pending_date')
            save_state(state_path, state)
            print(f'Scheduled: {when}')
        print(f"https://connect.garmin.com/modern/workout/{record['id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preview', 'export', 'login', 'upload'])
    parser.add_argument('plan', nargs='?')
    parser.add_argument('--output', default='garmin-data/workout-export.json')
    parser.add_argument('--yes', action='store_true', help='Publish without interactive confirmation')
    parser.add_argument('--tokens', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GarminWorkoutTool' / 'tokens')
    parser.add_argument('--state', type=Path, default=ROOT / 'garmin-data' / 'workout-upload-state.json')
    args = parser.parse_args()
    if args.command == 'login':
        login(args.tokens, fresh=True)
        print('Signed in. Tokens are stored locally outside the project and OneDrive.')
        return
    if not args.plan:
        parser.error('plan file is required')
    items = load_plans(args.plan)  # validate the complete batch before any network call
    preview(items)
    if args.command == 'export':
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        payloads = [payload for _, payload in items]
        out.write_text(json.dumps(payloads[0] if len(payloads) == 1 else payloads, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f'Garmin JSON written to {out.resolve()}')
    elif args.command == 'upload':
        if not args.yes and input('\nCreate/schedule these workouts in Garmin Connect? [y/N] ').lower() != 'y':
            print('Cancelled.')
            return
        publish(items, login(args.tokens), args.state)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nCancelled.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Exception bodies from authentication libraries may contain credentials/tokens.
        print(f'Garmin operation failed ({type(exc).__name__}). Check connection/login; inspect Connect before retrying a write.', file=sys.stderr)
        sys.exit(1)
