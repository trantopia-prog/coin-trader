"""설치판 실행기 테스트. 실제 프로세스를 띄워 확인한다 (데이터 폴더는 임시 폴더로 격리)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(home: Path, port: int) -> dict:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(f"ADMIN_PASSWORD='launcher-test'\nPORT={port}\nWATCH_MARKETS=\n", encoding="utf-8")
    return {**os.environ, "COIN_TRADER_HOME": str(home), "COIN_TRADER_NO_BROWSER": "1", "PYTHONPATH": str(ROOT)}


def _wait_up(port: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/me", timeout=1) as r:
                return "auth" in json.loads(r.read())
        except OSError:
            time.sleep(0.3)
    return False


WINDOWS = os.name == "nt"
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def _interrupt(p: subprocess.Popen) -> None:
    """Ctrl+C(리눅스) / Ctrl+Break(윈도우) 를 보내 정상 종료 경로를 탄다."""
    p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)


def test_console_mode_single_instance_and_shutdown(tmp_path):
    port = _free_port()
    env = {**_env(tmp_path / "home", port), "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen([sys.executable, "-m", "app.launcher", "--console"], cwd=tmp_path, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **TEXT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
    try:
        assert _wait_up(port), "서버가 뜨지 않음"
        inst = tmp_path / "home" / "instance.json"
        for _ in range(50):  # 서버 응답 직후 실행기가 instance.json 을 쓰기까지의 짧은 간격
            if inst.exists():
                break
            time.sleep(0.1)
        info = json.loads(inst.read_text())
        assert info["url"] == f"http://127.0.0.1:{port}"
        # 두 번째 실행은 새 서버를 띄우지 않고 기존 인스턴스를 알려주고 끝나야 한다
        second = subprocess.run([sys.executable, "-m", "app.launcher", "--console"], cwd=tmp_path, env=env,
                                capture_output=True, timeout=30, **TEXT)
        assert second.returncode == 0 and "이미 실행 중" in second.stdout
    finally:
        _interrupt(p)
        out, _ = p.communicate(timeout=30)
    assert p.returncode == 0, out
    assert not (tmp_path / "home" / "instance.json").exists()


def test_self_test_without_stdout_writes_log(tmp_path):
    """콘솔 없는 실행파일처럼 stdout/stderr 가 없을 때 로그 파일로 출력되는지."""
    env = _env(tmp_path / "home", _free_port())
    # pythonw/창 없는 실행파일과 같은 상태: sys.stdout, sys.stderr 가 None
    r = subprocess.run([sys.executable, "-c", "import sys; sys.stdout = sys.stderr = None; "
                        "from app.launcher import main; main(['--self-test'])"],
                       cwd=tmp_path, env=env, timeout=60, capture_output=True, **TEXT)
    log_path = tmp_path / "home" / "logs" / "coin-trader.log"
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "SELF-TEST" in log, f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r} log={log!r}"
    assert "SELF-TEST OK" in log or "SELF-TEST FAIL" in log
    assert r.returncode == (0 if "SELF-TEST OK" in log else 1)


TRAY_SCRIPT = textwrap.dedent('''
    import json, sys, threading, time
    import pystray
    events = []

    class FakeIcon:
        def __init__(self, name, image, title, menu=None):
            self.menu, self.title, self.visible = menu, title, False
            self._stopped = threading.Event()
        def notify(self, message, title=None):
            events.append(["notify", title, message])
        def update_menu(self):
            pass
        def stop(self):
            self._stopped.set()
        def run(self, setup=None):
            threading.Thread(target=setup, args=(self,), daemon=True).start()
            from app import launcher
            deadline = time.time() + 30
            items = {}
            while time.time() < deadline:
                items = {str(i.text): i for i in self.menu.items if i is not pystray.Menu.SEPARATOR}
                if any("모의투자" in k for k in items):
                    break
                time.sleep(0.2)
            events.append(["menu", sorted(items)])
            items["긴급정지 (신규 매수 차단)"](self)
            items["종료"](self)
            self._stopped.wait(30)

    pystray.Icon = FakeIcon
    from app.launcher import main
    main([])
    print("EVENTS=" + json.dumps(events, ensure_ascii=False))
''')


def test_tray_menu_emergency_stop_and_quit(tmp_path):
    pytest.importorskip("pystray")
    port = _free_port()
    env = _env(tmp_path / "home", port)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-c", TRAY_SCRIPT], cwd=tmp_path, env=env, capture_output=True, timeout=90, **TEXT)
    assert r.returncode == 0, r.stdout + r.stderr
    events = json.loads(r.stdout.split("EVENTS=", 1)[1])
    menu = next(e[1] for e in events if e[0] == "menu")
    assert "대시보드 열기" in menu and "종료" in menu and any("모의투자" in m for m in menu)
    titles = [e[1] for e in events if e[0] == "notify"]
    assert "CoinTrader" in titles  # 시작 알림
    assert "긴급정지" in titles  # 긴급정지 결과 알림
    assert not (tmp_path / "home" / "instance.json").exists()  # 정상 종료 정리
