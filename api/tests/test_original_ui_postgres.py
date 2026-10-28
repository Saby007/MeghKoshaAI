import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.enable_socket


def test_original_dashboard_with_real_sql_projections(tmp_path):
    if os.environ.get("ASSESSMENT_RUN_BROWSER_TESTS") != "1":
        pytest.skip("Enable the local SQL-backed browser parity gate explicitly.")
    if not os.environ.get("ASSESSMENT_TEST_DSN"):
        pytest.fail("The SQL-backed browser gate requires the dedicated local test database.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).parents[2]
    with (tmp_path / "fixture-server.log").open("w+") as log:
        server = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("serving_ui_fixture.py")), "--port", str(port)],
            cwd=root / "api", stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 45
            while True:
                try:
                    with urllib.request.urlopen(f"{url}/fixture", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        pytest.fail(f"Synthetic fixture startup failed:\n{log.read()}")
                    time.sleep(0.2)
            result = subprocess.run(
                [shutil.which("npm.cmd") or shutil.which("npm"), "run", "test:browser", "--", "postgres-original-ui.pw.ts"],
                cwd=root / "web", env={**os.environ, "SQL_UI_FIXTURE_URL": url},
                capture_output=True, text=True, timeout=600,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout)
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
