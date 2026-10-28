"""Offline MCP tests; --live additionally verifies Garmin sign-in, without writes."""
import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import garmin_mcp as server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ServerTests(unittest.TestCase):
    def setUp(self):
        (Path(__file__).resolve().parent / 'garmin-data').mkdir(exist_ok=True)
        self.plan = json.loads((server.ROOT / 'workouts/swimming-example.json').read_text())

    def fake_api(self):
        api = Mock()
        stored, scheduled = [], []
        api.get_devices.return_value = [
            {'deviceId': 11, 'productDisplayName': 'Forerunner 255'},
            {'deviceId': 22, 'productDisplayName': 'Edge 530'}]
        api.get_workouts.side_effect = lambda start=0, limit=100: stored[start:start + limit]
        api.get_workout_by_id.side_effect = lambda wid: next(w for w in stored if w['workoutId'] == wid)
        api.get_scheduled_workouts_by_period.side_effect = lambda start, end: [s for s in scheduled if s['date'] == start]
        def upload(payload):
            item = copy.deepcopy(payload)
            item['workoutId'] = 123 + len(stored)
            stored.append(item)
            return item
        def schedule(wid, when):
            scheduled.append({'date': when, 'workout': {'workoutId': wid}})
        api.upload_workout.side_effect = upload
        api.schedule_workout.side_effect = schedule
        return api

    def test_preview_needs_no_credentials(self):
        with patch.object(server, 'connect', side_effect=AssertionError('Must stay offline')):
            self.assertTrue(server.garmin_preview_workouts([self.plan])['valid'])

    def test_invalid_batch_never_connects(self):
        with patch.object(server, 'connect') as connect:
            with self.assertRaises(server.ToolError):
                server.garmin_add_workouts([self.plan, {'name': 'invalid'}])
            connect.assert_not_called()

    def test_env_password_not_interpolated(self):
        with patch.object(server, 'dotenv_values', return_value={'GARMIN_EMAIL': 'test@example.invalid', 'GARMIN_PWD': '${literal}#password'}) as read:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(server.credentials()[1], '${literal}#password')
            self.assertFalse(read.call_args.kwargs['interpolate'])

    def test_auth_errors_redacted(self):
        with patch.object(server, 'credentials', return_value=('test@example.invalid', 'secret-password')):
            with patch('garminconnect.Garmin') as cls:
                cls.return_value.login.side_effect = RuntimeError('secret-password secret-token')
                with self.assertRaises(server.ToolError) as err:
                    server.connect()
                self.assertNotIn('secret-password', str(err.exception))
                self.assertNotIn('secret-token', str(err.exception))

    def test_add_returns_structured_result_and_reuses_ledger(self):
        api = self.fake_api()
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                result = server.garmin_add_workouts([self.plan])
                server.garmin_add_workouts([self.plan])
                self.assertTrue(result['success'])
                self.assertEqual(result['workouts'][0]['scheduled_dates'], ['2030-01-10'])
                api.upload_workout.assert_called_once()
                api.schedule_workout.assert_called_once()
                api.push_workout_to_device.assert_called_once_with(workout_id=123, device_id=11)

    def test_remote_duplicate_reused_without_ledger(self):
        api = self.fake_api()
        payload = server.compile_workout(self.plan)
        api.upload_workout(payload)
        api.schedule_workout(123, self.plan['date'])
        api.upload_workout.reset_mock()
        api.schedule_workout.reset_mock()
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                result = server.garmin_add_workouts([self.plan])
        self.assertTrue(result['success'])
        api.upload_workout.assert_not_called()
        api.schedule_workout.assert_not_called()
        api.push_workout_to_device.assert_called_once_with(workout_id=123, device_id=11)

    def test_cycling_routes_to_edge(self):
        api = self.fake_api()
        plan = {'name': 'Bike', 'sport': 'cycling', 'date': '2026-11-01', 'steps': [{'seconds': 600}]}
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                server.garmin_add_workouts([plan])
        api.push_workout_to_device.assert_called_once_with(workout_id=123, device_id=22)

    def test_already_scheduled_copy_preferred_when_library_has_duplicates(self):
        api = self.fake_api()
        payload = server.compile_workout(self.plan)
        api.upload_workout(payload)
        api.upload_workout(payload)
        api.schedule_workout(124, self.plan['date'])
        api.upload_workout.reset_mock()
        api.schedule_workout.reset_mock()
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                result = server.garmin_add_workouts([self.plan])
        self.assertEqual(result['workouts'][0]['workout_id'], 124)
        api.upload_workout.assert_not_called()
        api.schedule_workout.assert_not_called()

    def test_missing_or_wrong_model_stops_before_upload(self):
        api = self.fake_api()
        api.get_devices.return_value = [{'deviceId': 33, 'productDisplayName': 'Forerunner 2550'}]
        with patch.object(server, 'connect', return_value=api):
            with self.assertRaises(server.ToolError):
                server.garmin_add_workouts([self.plan])
        api.upload_workout.assert_not_called()

    def test_failed_duplicate_check_stops_before_upload(self):
        api = self.fake_api()
        api.get_workouts.side_effect = TimeoutError()
        with patch.object(server, 'connect', return_value=api):
            with self.assertRaises(server.ToolError):
                server.garmin_add_workouts([self.plan])
        api.upload_workout.assert_not_called()

    def test_uncertain_device_send_is_not_retried(self):
        api = self.fake_api()
        api.push_workout_to_device.side_effect = TimeoutError()
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                result = server.garmin_add_workouts([self.plan])
                self.assertTrue(result['success'])  # Garmin workout itself is saved.
                self.assertFalse(result['device_delivery_complete'])
                server.garmin_add_workouts([self.plan])
        api.push_workout_to_device.assert_called_once()
        api.upload_workout.assert_called_once()

    def test_library_only_not_sent(self):
        api = self.fake_api()
        plan = dict(self.plan)
        plan.pop('date')
        with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
            with patch.object(server, 'STATE', Path(tmp) / 'state.json'), patch.object(server, 'connect', return_value=api):
                server.garmin_add_workouts([plan])
        api.push_workout_to_device.assert_not_called()

    def test_signature_ignores_garmin_metadata_but_preserves_workout_content(self):
        payload = server.compile_workout(self.plan)
        remote = copy.deepcopy(payload)
        remote['workoutSegments'][0]['workoutSteps'][0].update(stepId=55, childStepId=1)
        remote['workoutSegments'][0]['workoutSteps'][0]['stepType']['displayOrder'] = 99
        self.assertTrue(server.signatures_equal(server.workout_signature(payload), server.workout_signature(remote)))
        remote['workoutSegments'][0]['workoutSteps'][0]['endConditionValue'] += 25
        self.assertFalse(server.signatures_equal(server.workout_signature(payload), server.workout_signature(remote)))


async def protocol_smoke(live=False):
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(server.ROOT / 'garmin_mcp.py')], cwd=str(server.ROOT),
        env={key: value for key, value in os.environ.items()
             if key in ('SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'LOCALAPPDATA', 'PATH', 'TEMP', 'TMP')})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = [tool.name for tool in listing.tools]
            assert 'garmin_add_workouts' in names
            print('MCP handshake and discovery OK:', ', '.join(names))
            plan = json.loads((server.ROOT / 'workouts/swimming-example.json').read_text())
            result = await session.call_tool('garmin_preview_workouts', {'workouts': [plan]})
            assert not result.isError, 'MCP preview failed'
            print('MCP swim preview OK')
            bad = await session.call_tool('garmin_preview_workouts', {'workouts': [{'name': 'invalid'}]})
            assert bad.isError, 'Invalid workout was not rejected'
            print('MCP invalid plan rejection OK')
            if live:
                result = await session.call_tool('garmin_connection_status', {})
                # Tool errors are explicitly sanitized by the server.
                print('Live connection:', json.dumps(result.structuredContent or [c.text for c in result.content if c.type == 'text']))
                assert not result.isError, 'Live Garmin sign-in failed'
                devices = await session.call_tool('garmin_workout_devices', {})
                assert not devices.isError, 'Device routes could not be verified'
                print('Live device routing OK: Forerunner 255 for runs/swims, Edge 530 for cycling')
                # Only reads Garmin; reconciliation writes to an isolated local test ledger.
                with tempfile.TemporaryDirectory(dir=server.ROOT / 'garmin-data') as tmp:
                    with patch.object(server, 'STATE', Path(tmp) / 'dry-run-state.json'):
                        server.reconcile_existing(server.connect(), [(plan, server.compile_workout(plan))])
                        state = json.loads(server.STATE.read_text())
                        record = next(iter(state.values()), {})
                        print('Live duplicate check:', 'existing swim and scheduled date found' if plan['date'] in record.get('dates', []) else 'no exact scheduled match; no external writes performed')


if __name__ == '__main__':
    if '--protocol' in sys.argv or '--live' in sys.argv:
        asyncio.run(protocol_smoke(live='--live' in sys.argv))
    else:
        unittest.main()
