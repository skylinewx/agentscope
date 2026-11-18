from copy import deepcopy
from unittest import TestCase
from jsonschema import Draft202012Validator, ValidationError

from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisTask
from analysis_extension.turn_completion import TurnCompletion


class TaskContextSchemaTests(TestCase):
    def test_discovery_and_adoption_keep_distinct_locations(self):
        validator = Draft202012Validator(AnalysisTask.input_schema)
        validator.validate({'action': 'sources', 'contextRefs': ['context-a', 'context-b']})
        validator.validate({'action': 'declare', 'mode': 'ANALYSIS', 'goal': '分析两组结果',
                            'context': {'mode': 'SYNTHESIZE', 'contextRefs': ['context-a', 'context-b']}})
        for action in ('declare', 'contexts', 'status', 'blocked', 'select_results'):
            with self.subTest(action=action), self.assertRaises(ValidationError):
                validator.validate({'action': action, 'mode': 'DIRECT', 'goal': '查询', 'contextRefs': []})


class TaskContextServiceTests(PresentationCase):
    async def test_wrong_context_location_is_a_structured_error_without_state_change(self):
        turn = TurnCompletion(self.service, {'turnKey': 'context-test', 'request': '继续查询'})
        self.service.turn = turn
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.task(action='declare', mode='DIRECT', goal='继续查询', contextRefs=[])
        self.assertEqual(caught.exception.code, 'INVALID_TASK_ARGUMENT')
        self.assertEqual(caught.exception.details['expectedPath'], '/context/contextRefs')
        self.assertEqual(turn.state, before)
