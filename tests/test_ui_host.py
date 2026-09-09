import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_DATA_TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("VPNGATE_DATA_DIR", _DATA_TEMP.name)

import vpngate_manager as manager

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAIN_BIND_SNIPPET = textwrap.dedent(
    """
    import json
    import sys
    from unittest import mock

    import vpngate_manager as manager

    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["address"] = list(address)
            captured["handler"] = handler.__name__

        def serve_forever(self):
            pass

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, *args):
            pass

        def connect(self, *args):
            pass

        def close(self):
            pass

    real_stdout, real_stderr = sys.stdout, sys.stderr
    try:
        with mock.patch.object(manager, "DualStackHTTPServer", FakeServer), \\
             mock.patch.object(manager.threading, "Thread", lambda *a, **k: mock.Mock()), \\
             mock.patch.object(manager, "kill_existing_openvpn_processes"), \\
             mock.patch.object(manager, "refresh_pool_state"), \\
             mock.patch.object(manager, "_recover_slots_on_boot"), \\
             mock.patch.object(manager.socket, "socket", FakeSocket):
            manager.main()
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    print("BIND=" + json.dumps(captured))
    """
)


def run_manager_snippet(snippet: str, extra_env: dict) -> str:
    with tempfile.TemporaryDirectory() as data_dir:
        env = dict(os.environ)
        env.pop("UI_HOST", None)
        env["VPNGATE_DATA_DIR"] = data_dir
        env.update(extra_env)
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if result.returncode != 0:
        raise AssertionError(f"snippet failed:\n{result.stderr}")
    return result.stdout


class UIHostTests(unittest.TestCase):
    def test_default_ui_host_is_loopback(self):
        out = run_manager_snippet(
            "import vpngate_manager as m; print(m.UI_HOST)", {}
        )
        self.assertEqual("127.0.0.1", out.strip().splitlines()[-1])

    def test_existing_wildcard_ui_host_migrates_to_loopback(self):
        out = run_manager_snippet(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                data_dir = Path(os.environ["VPNGATE_DATA_DIR"])
                (data_dir / "ui_auth.json").write_text(
                    json.dumps({"host": "::", "port": 8787}), encoding="utf-8"
                )
                import vpngate_manager as manager
                print(manager.load_ui_config()["host"])
                """
            ),
            {},
        )
        self.assertEqual("127.0.0.1", out.strip().splitlines()[-1])

    def test_env_ui_host_is_passed_to_server_bind(self):
        out = run_manager_snippet(_MAIN_BIND_SNIPPET, {"UI_HOST": "127.0.0.1"})
        bind_line = next(
            line for line in out.splitlines() if line.startswith("BIND=")
        )
        captured = json.loads(bind_line[len("BIND="):])
        self.assertEqual(["127.0.0.1", manager.UI_PORT], captured["address"])
        self.assertEqual("Handler", captured["handler"])


if __name__ == "__main__":
    unittest.main()
