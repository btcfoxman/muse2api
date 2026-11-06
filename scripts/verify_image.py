"""Run only on the hosted build runner, inside its unpublished image candidate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def isolated_env(home: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(MUSE2API_HOME=home,
               MUSE2API_PROFILE_ROOT=str(Path(home) / 'profiles'),
               MUSE2API_HOME_DIR=home,
               MUSE2API_KEY='offline-ci-only-key',
               MUSE2API_MANAGED_DEPLOYMENT='0',
               PYTHONDONTWRITEBYTECODE='1')
    return env


def regressions() -> None:
    tests = sorted((ROOT / 'tests').glob('test_*.py'))
    if not tests:
        raise RuntimeError('No image regression tests were mounted')
    for test in tests:
        with tempfile.TemporaryDirectory(prefix='muse-ci-') as home:
            command = [sys.executable, str(test)]
            if test.name == 'test_vm_wait.py':
                command.extend([str(ROOT / 'engine.py'), '--assert'])
            if test.name == 'test_account_proxy.py':
                command.append('--browser')
            print(f'Running {test.name}', flush=True)
            subprocess.run(command, cwd=ROOT, env=isolated_env(home),
                           check=True, timeout=240)


def smoke() -> None:
    expected = os.environ['EXPECTED_GIT_SHA']
    assert os.environ.get('APP_GIT_SHA') == expected, 'Image revision differs from requested commit'
    with tempfile.TemporaryDirectory(prefix='muse-smoke-') as home:
        env = isolated_env(home)
        env.pop('MUSE2API_KEY')
        env['MUSE2API_MANAGED_DEPLOYMENT'] = '1'
        Path(home, '.env').write_text('MUSE2API_KEY=offline-ci-only-key\n', encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'uvicorn', '--help'], cwd=ROOT,
                       env=env, stdout=subprocess.DEVNULL, check=True, timeout=15)
        chromium = subprocess.run(['/usr/bin/chromium', '--headless', '--no-sandbox',
                        '--disable-dev-shm-usage', '--disable-gpu',
                        f'--user-data-dir={home}/chromium-smoke', '--dump-dom',
                        'data:text/html,<p>muse-image-smoke</p>'], env=env,
                       check=True, timeout=30, capture_output=True)
        assert b'muse-image-smoke' in chromium.stdout, 'Chromium did not render the smoke page'
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1',
                 '--port', '18610'], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                origin = 'http://127.0.0.1:18610'
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError('Candidate process exited during startup')
                    try:
                        with urllib.request.urlopen(origin + '/healthz', timeout=2) as response:
                            assert json.load(response)['status'] == 'ok'
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.5)
                else:
                    raise RuntimeError('Candidate health check timed out')
                with urllib.request.urlopen(origin + '/readyz', timeout=3) as response:
                    assert json.load(response)['status'] == 'no_account'
                try:
                    urllib.request.urlopen(origin + '/v1/models', timeout=3)
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                else:
                    raise AssertionError('Unauthenticated models must return 401')
                request = urllib.request.Request(origin + '/v1/models', headers={
                    'Authorization': 'Bearer offline-ci-only-key'})
                with urllib.request.urlopen(request, timeout=3) as response:
                    assert json.load(response)['data']
                print(f'PASS: image revision, Chromium, process health and authentication ({expected})', flush=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    regressions()
    smoke()
