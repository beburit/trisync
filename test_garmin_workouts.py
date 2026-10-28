import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from garmin_workouts import ROOT, compile_workout, load_plans, publish


class WorkoutTests(unittest.TestCase):
    def setUp(self):
        (Path(__file__).resolve().parent / 'garmin-data').mkdir(exist_ok=True)
        self.plan = json.loads((ROOT / 'workouts/swimming-example.json').read_text())

    def test_swim_distance_and_rest(self):
        payload = compile_workout(self.plan)
        def distance(steps):
            return sum(s['numberOfIterations'] * distance(s['workoutSteps'])
                       if s['type'] == 'RepeatGroupDTO' else
                       s.get('endConditionValue', 0) if s['endCondition']['conditionTypeKey'] == 'distance' else 0
                       for s in steps)
        steps = payload['workoutSegments'][0]['workoutSteps']
        self.assertEqual(distance(steps), 1800)
        self.assertEqual(payload['poolLength'], 25)
        self.assertEqual(steps[2]['numberOfIterations'], 6)
        rest = steps[2]['workoutSteps'][1]
        self.assertEqual(rest['endCondition']['conditionTypeId'], 8)
        self.assertEqual(rest['endConditionValue'], 25)
        self.assertEqual(steps[2]['endCondition']['conditionTypeId'], 7)

    def test_pace_conversion(self):
        item = load_plans(ROOT / 'workouts/running-example.json')[0][1]
        step = item['workoutSegments'][0]['workoutSteps'][1]['workoutSteps'][0]
        self.assertAlmostEqual(step['targetValueOne'], 1000 / 330)
        self.assertAlmostEqual(step['targetValueTwo'], 1000 / 315)

    def test_invalid_duration_pool_repeat_and_unknown_field(self):
        mutations = [lambda p: p['steps'][0].update(seconds=10),
                     lambda p: p['steps'][0].update(meters=33),
                     lambda p: p['steps'][1].update(repeat=True),
                     lambda p: p.update(poo_length_m=50),
                     lambda p: p.update(date='2026-02-30'),
                     lambda p: p['steps'][0].update(meters=float('nan'))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                plan = copy.deepcopy(self.plan)
                mutate(plan)
                with self.assertRaises(ValueError):
                    compile_workout(plan)

    def test_zone_uses_zone_number(self):
        payload = compile_workout({'name': 'Test', 'sport': 'running', 'steps': [
            {'seconds': 300, 'target': {'hr_zone': 2}}]})
        step = payload['workoutSegments'][0]['workoutSteps'][0]
        self.assertEqual(step['zoneNumber'], 2)
        self.assertNotIn('targetValueOne', step)

    def test_batch_validation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'garmin-data') as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps({'workouts': [self.plan, {'name': 'bad'}]}))
            with self.assertRaises(ValueError):
                load_plans(path)

    def test_repeated_upload_does_not_duplicate(self):
        api = Mock()
        api.upload_workout.return_value = {'workoutId': 123}
        with tempfile.TemporaryDirectory(dir=ROOT / 'garmin-data') as directory:
            state = Path(directory) / 'state.json'
            items = [(self.plan, compile_workout(self.plan))]
            publish(items, api, state)
            publish(items, api, state)
            api.upload_workout.assert_called_once()
            api.schedule_workout.assert_called_once_with(123, '2030-01-10')

    def test_ambiguous_upload_is_not_retried(self):
        api = Mock()
        api.upload_workout.side_effect = TimeoutError()
        with tempfile.TemporaryDirectory(dir=ROOT / 'garmin-data') as directory:
            state = Path(directory) / 'state.json'
            items = [(self.plan, compile_workout(self.plan))]
            with self.assertRaises(TimeoutError):
                publish(items, api, state)
            with self.assertRaisesRegex(ValueError, 'Uncertain previous upload'):
                publish(items, api, state)
            api.upload_workout.assert_called_once()

    def test_ambiguous_schedule_is_not_retried(self):
        api = Mock()
        api.upload_workout.return_value = {'workoutId': 123}
        api.schedule_workout.side_effect = TimeoutError()
        with tempfile.TemporaryDirectory(dir=ROOT / 'garmin-data') as directory:
            state = Path(directory) / 'state.json'
            items = [(self.plan, compile_workout(self.plan))]
            with self.assertRaises(TimeoutError):
                publish(items, api, state)
            with self.assertRaisesRegex(ValueError, 'Uncertain previous scheduling'):
                publish(items, api, state)
            api.upload_workout.assert_called_once()
            api.schedule_workout.assert_called_once()


if __name__ == '__main__':
    unittest.main()
