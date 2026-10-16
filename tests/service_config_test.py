"""Configuration precedence and validation without starting Redis or the app."""
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples' / 'agent_service'))
from service_config import redis_settings


class ServiceConfigTest(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'config.json'

    def test_defaults_need_no_secret(self):
        self.assertEqual(redis_settings(self.path, {}), {'host': 'localhost', 'port': 6379, 'db': 0, 'password': None})

    def test_environment_overrides_local_settings_including_empty_password(self):
        self.path.write_text(json.dumps({'redis': {'host': 'local', 'port': 6380, 'password': 'test-only'}}))
        self.assertEqual(redis_settings(self.path, {'AGENTSCOPE_REDIS_HOST': 'server', 'AGENTSCOPE_REDIS_DB': '2',
                                                    'AGENTSCOPE_REDIS_PASSWORD': ''}),
                         {'host': 'server', 'port': 6380, 'db': 2, 'password': None})

    def test_bad_configuration_fails_without_echoing_values(self):
        for config in ({'redsi': {}}, {'redis': []}, {'redis': {'port': True}}, {'redis': {'port': 70000}},
                       {'redis': {'db': -1}}, {'redis': {'host': ''}}, {'redis': {'password': 123}}):
            with self.subTest(config=config):
                self.path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    redis_settings(self.path, {})
