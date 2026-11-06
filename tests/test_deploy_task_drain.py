"""Exercise the real deployment task reader without sudo or private server data."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'scripts/deploy_pre.sh').read_text(encoding='utf-8')
READER = SCRIPT.split('active_tasks() {', 1)[1].split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]


class TaskDrainTest(unittest.TestCase):
    def run_reader(self, path):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['-', str(path)]), contextlib.redirect_stdout(output):
            exec(compile(READER, 'deploy_pre.sh task reader', 'exec'), {})
        return output.getvalue()

    def test_readable_and_missing_records_need_no_privilege(self):
        with tempfile.TemporaryDirectory() as folder, patch('subprocess.run') as privileged:
            path = Path(folder) / 'tasks.json'
            self.assertEqual(self.run_reader(path), '0\n')
            path.write_text(json.dumps({
                'one': {'status': 'processing'}, 'two': {'status': 'queued'},
                'three': {'status': 'completed'}, 'four': {'status': 'failed'},
            }), encoding='utf-8')
            self.assertEqual(self.run_reader(path), '2\n')
            privileged.assert_not_called()

    def test_private_read_fallback_counts_without_disclosing_payload(self):
        path = Path('/fixed/deployment/data/tasks.json')
        payload = json.dumps({'fixture': {'status': 'running', 'prompt': 'private-marker'}})
        with patch.object(Path, 'read_text', side_effect=PermissionError), \
             patch('subprocess.run', return_value=SimpleNamespace(stdout=payload)) as privileged:
            self.assertEqual(self.run_reader(path), '1\n')
            privileged.assert_called_once_with(
                ['sudo', '-n', 'cat', str(path)], capture_output=True, text=True,
                encoding='utf-8', timeout=5, check=True)

    def test_invalid_or_unreadable_private_data_fails_closed(self):
        path = Path('/fixed/deployment/data/tasks.json')
        for payload in ('private-marker invalid json', '[]', '{"task":null}'):
            with self.subTest(payload=payload), \
                 patch.object(Path, 'read_text', side_effect=PermissionError), \
                 patch('subprocess.run', return_value=SimpleNamespace(stdout=payload)):
                with self.assertRaises(SystemExit) as error:
                    self.run_reader(path)
                self.assertNotIn('private-marker', str(error.exception))
        with patch.object(Path, 'read_text', side_effect=PermissionError), \
             patch('subprocess.run', side_effect=subprocess.CalledProcessError(
                 1, ['sudo'], output='private-marker', stderr='private-marker')):
            with self.assertRaises(SystemExit) as error:
                self.run_reader(path)
            self.assertEqual(str(error.exception), 'Cannot safely read persisted task records')


if __name__ == '__main__':
    unittest.main()
