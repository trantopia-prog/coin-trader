"""설치판(Windows .exe) 실행기: 트레이 아이콘으로 백그라운드 실행.

- 서버(uvicorn + 매매 엔진)는 백그라운드 스레드의 이벤트 루프에서 돌고, 메인 스레드는 트레이 아이콘을 담당한다.
- 첫 실행 시 관리자 비밀번호를 입력받아 사용자 데이터 폴더에 .env 를 만든다 (GUI 창, 없으면 콘솔).
- 이미 실행 중이면 새로 띄우지 않고 기존 대시보드를 연다.
- 콘솔이 없는 실행파일에서는 표준출력/오류를 로그 파일로 보낸다.

옵션: --console  트레이 없이 콘솔에서 실행 (개발/서버용)
      --no-browser  시작 시 브라우저를 열지 않음 (Windows 시작 시 자동 실행에 사용)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "CoinTrader"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------------------------------------------------------------------- 공통 유틸
def _paths():
    from app.config import BASE_DIR, DATA_DIR, ENV_FILE

    return BASE_DIR, DATA_DIR, ENV_FILE


def _log_file() -> Path:
    _, data_dir, _ = _paths()
    return data_dir / "logs" / "coin-trader.log"


def _setup_stdio() -> None:
    """콘솔 없는 실행파일은 sys.stdout 이 None 이다. 로그 파일로 연결하고 5MB 넘으면 하나 보관 후 새로 쓴다."""
    if sys.stdout is not None and sys.stderr is not None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        return
    path = _log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        path.replace(path.with_suffix(".log.1"))
    f = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - 프로세스 수명 동안 유지
    sys.stdout = sys.stderr = f


def _message_box(text: str, title: str = APP_NAME, yes_no: bool = False) -> bool:
    """Windows 메시지 상자. 다른 OS 에서는 출력만 하고 True."""
    if os.name == "nt":
        import ctypes

        flags = 0x04 | 0x30 if yes_no else 0x40  # MB_YESNO|MB_ICONWARNING : MB_ICONINFORMATION
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000) in (1, 6)  # IDOK, IDYES
    print(f"[{title}] {text}")
    return True


def _open_path(path: Path, is_file: bool = False) -> None:
    try:
        if is_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607
    except OSError as e:
        _message_box(f"열 수 없습니다: {path}\n{e}")


# ---------------------------------------------------------------------------- 첫 실행 설정
def _ask_password_gui() -> str | None:
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        while True:
            pw = simpledialog.askstring(
                f"{APP_NAME} 첫 실행 설정",
                "대시보드 로그인에 사용할 관리자 비밀번호를 정하세요 (8자 이상).",
                show="*", parent=root,
            )
            if pw is None:
                raise SystemExit(0)
            if len(pw) < 8:
                messagebox.showwarning(APP_NAME, "8자 이상 입력하세요.", parent=root)
                continue
            if simpledialog.askstring(APP_NAME, "비밀번호를 한 번 더 입력하세요.", show="*", parent=root) != pw:
                messagebox.showwarning(APP_NAME, "두 비밀번호가 다릅니다.", parent=root)
                continue
            return pw
    finally:
        root.destroy()


def _ask_password_console() -> str:
    import getpass

    print("=" * 60)
    print(f" {APP_NAME} 첫 실행 설정")
    print("=" * 60)
    print(" 대시보드 로그인에 사용할 관리자 비밀번호를 정하세요 (8자 이상).")
    while True:
        pw = getpass.getpass(" 비밀번호: ")
        if len(pw) < 8:
            print(" 8자 이상 입력하세요.")
            continue
        if pw != getpass.getpass(" 비밀번호 확인: "):
            print(" 두 비밀번호가 다릅니다.")
            continue
        return pw


def _quote(value: str) -> str:
    """dotenv 값으로 안전하게 쓰기 위해 따옴표로 감싼다 (#, 공백, = 포함 비밀번호 대응)."""
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ensure_env(prefer_gui: bool) -> None:
    _, data_dir, env_file = _paths()
    data_dir.mkdir(parents=True, exist_ok=True)
    if env_file.exists():
        return
    pw = _ask_password_gui() if prefer_gui else None
    if pw is None:
        if sys.stdin is None or not sys.stdin.isatty():
            _message_box("비밀번호 입력 창을 열 수 없습니다. 다시 설치해 주세요.")
            raise SystemExit(1)
        pw = _ask_password_console()
    if any(c in pw for c in "\r\n"):
        raise SystemExit("비밀번호에 줄바꿈을 넣을 수 없습니다.")
    env_file.write_text(
        "# CoinTrader 설정 파일. 수정 후 프로그램을 다시 시작하세요.\n"
        f"ADMIN_PASSWORD={_quote(pw)}\n"
        f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
        "# 업비트 API 키는 대시보드 설정 화면에서 등록하는 것을 권장합니다 (암호화 저장).\n"
        "UPBIT_ACCESS_KEY=\nUPBIT_SECRET_KEY=\n"
        "# paper: 모의투자만 / live: 대시보드에서 비밀번호 재확인 후 실거래 전환 가능\n"
        "TRADING_MODE=paper\n"
        "PAPER_INITIAL_KRW=10000000\n"
        "RISK_MAX_ORDER_KRW=1000000\nRISK_MAX_POSITION_RATIO=0.3\nRISK_DAILY_LOSS_LIMIT_PCT=5\n"
        "WATCH_MARKETS=KRW-BTC,KRW-ETH,KRW-XRP,KRW-SOL\n"
        "CRYPTOPANIC_TOKEN=\nNEWS_INTERVAL_SEC=300\nDISCLOSURE_SCRAPE_URLS=\n"
        "HOST=127.0.0.1\nPORT=8000\n",
        encoding="utf-8",
    )
    try:
        env_file.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------- 단일 실행
def _instance_file() -> Path:
    return _paths()[1] / "instance.json"


def _existing_instance() -> str | None:
    """이미 실행 중인 인스턴스의 URL. instance.json 의 포트가 실제로 이 앱으로 응답할 때만 인정."""
    try:
        info = json.loads(_instance_file().read_text(encoding="utf-8"))
        url = info["url"]
        with urllib.request.urlopen(url + "/api/me", timeout=1.5) as r:  # noqa: S310 - 로컬 주소
            if "auth" in json.loads(r.read()):
                return url
    except (OSError, ValueError, KeyError):
        pass
    return None


def _free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{start}~{start + tries - 1} 포트가 모두 사용 중입니다. .env 의 PORT 를 바꾸세요.")


# ---------------------------------------------------------------------------- Windows 자동 실행
def autostart_supported() -> bool:
    return os.name == "nt" and getattr(sys, "frozen", False)


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --no-browser')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------- 서버 스레드
class ServerThread:
    """uvicorn 서버를 별도 스레드의 이벤트 루프에서 실행. 트레이(메인 스레드)에서 시작/종료를 제어한다."""

    def __init__(self, host: str, port: int):
        import uvicorn

        from app.main import app

        self.app = app
        config = uvicorn.Config(
            app, host=host, port=port, log_level="info", loop="asyncio", http="h11", ws="websockets-sansio",
        )
        self.server = uvicorn.Server(config)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._run, name="server", daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.serve())
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            self.loop.close()

    def start(self, timeout: float = 60) -> bool:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return False

    def stop(self, timeout: float = 15) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)

    @property
    def svc(self):
        return getattr(self.app.state, "svc", None)

    def call(self, coro, timeout: float = 30):
        """서버 루프에서 코루틴 실행 후 결과 반환 (트레이 메뉴 동작용)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)


# ---------------------------------------------------------------------------- 트레이
class TrayApp:
    NOTIFY_LEVELS = ("trade", "error", "warning")

    def __init__(self, server: ServerThread, url: str, open_browser: bool):
        self.server = server
        self.url = url
        self.open_browser = open_browser
        self.icon = None
        self._last_notify: dict[str, float] = {}

    def _image(self):
        from PIL import Image, ImageDraw

        base, _, _ = _paths()
        for p in (base / "assets" / "icon.ico", base / "packaging" / "icon.ico"):
            if p.exists():
                return Image.open(p)
        img = Image.new("RGBA", (64, 64), (79, 70, 229, 255))
        ImageDraw.Draw(img).rectangle((24, 14, 40, 50), fill=(246, 70, 93, 255))
        return img

    # --- 메뉴 텍스트/동작
    def _status_text(self, _item=None) -> str:
        svc = self.server.svc
        if not svc:
            return "시작 중…"
        eng = svc.engine
        mode = "실거래" if eng.mode == "live" else "모의투자"
        halted = " · 정지됨" if svc.risk.halted else ""
        equity = f" · 자산 {eng.last_equity:,.0f}원" if eng.last_equity else ""
        return f"{mode}{equity}{halted}"

    def _open_dashboard(self, *_):
        webbrowser.open(self.url)

    def _emergency_stop(self, *_):
        svc = self.server.svc
        if not svc:
            return
        try:
            r = self.server.call(svc.engine.emergency_stop(False))
            self._notify("긴급정지", f"신규 매수를 차단했습니다. 미체결 {r['cancelled']}건 취소. 대시보드 설정에서 재개할 수 있습니다.", force=True)
        except Exception as e:  # noqa: BLE001
            self._notify("긴급정지 실패", str(e), force=True)

    def _toggle_autostart(self, *_):
        try:
            set_autostart(not autostart_enabled())
        except OSError as e:
            self._notify("자동 실행 설정 실패", str(e), force=True)

    def _quit(self, *_):
        svc = self.server.svc
        running = 0
        if svc:
            try:
                running = self.server.call(_count_enabled(svc))
            except Exception:  # noqa: BLE001
                running = 0
        if running and not _message_box(
            f"실행 중인 자동매매 전략 {running}개가 멈춥니다.\n종료할까요?", yes_no=True
        ):
            return
        self.icon.title = f"{APP_NAME} 종료 중…"
        self.server.stop()
        self.icon.stop()

    def _menu(self):
        from pystray import Menu, MenuItem as Item

        return Menu(
            Item("대시보드 열기", self._open_dashboard, default=True),
            Item(self._status_text, None, enabled=False),
            Menu.SEPARATOR,
            Item("긴급정지 (신규 매수 차단)", self._emergency_stop),
            Menu.SEPARATOR,
            Item("Windows 시작 시 자동 실행", self._toggle_autostart, checked=lambda _i: autostart_enabled(),
                 visible=autostart_supported()),
            Item("데이터 폴더 열기", lambda *_: _open_path(_paths()[1])),
            Item("로그 파일 열기", lambda *_: _open_path(_log_file(), is_file=True)),
            Menu.SEPARATOR,
            Item("종료", self._quit),
        )

    # --- 알림
    def _notify(self, title: str, message: str, force: bool = False) -> None:
        if not self.icon:
            return
        key = title + message[:40]
        now = time.monotonic()
        if not force and now - self._last_notify.get(key, 0) < 300:
            return
        self._last_notify[key] = now
        try:
            self.icon.notify(message[:250], title)
        except Exception:  # noqa: BLE001 - 알림 미지원 환경
            pass

    async def _event_forwarder(self):
        svc = self.server.svc
        q = svc.bus.subscribe("log", "news")
        try:
            while True:
                topic, d = await q.get()
                if topic == "log" and d.get("level") in self.NOTIFY_LEVELS:
                    title = {"trade": "체결", "error": "오류", "warning": "주의"}[d["level"]]
                    self._notify(title, d["message"], force=d["level"] == "trade")
                elif topic == "news" and d.get("importance") in ("warning", "delisting"):
                    self._notify("거래소 공지 (매수 차단)", d["title"])
        finally:
            svc.bus.unsubscribe(q)

    def _setup(self, icon):
        icon.visible = True
        self.server.spawn(self._event_forwarder())
        self._notify(APP_NAME, "백그라운드에서 실행 중입니다. 트레이 아이콘을 더블클릭하면 대시보드가 열립니다.", force=True)
        if self.open_browser:
            webbrowser.open(self.url)
        # 메뉴의 상태 문구 갱신 + 서버가 비정상 종료되면 트레이도 종료
        while self.icon is not None and self.server.thread.is_alive():
            time.sleep(5)
            try:
                icon.title = f"{APP_NAME} - {self._status_text()}"
                icon.update_menu()
            except Exception:  # noqa: BLE001
                pass
        if not self.server.server.should_exit:
            _message_box("서버가 예기치 않게 종료되었습니다. 로그 파일을 확인하세요.\n" + str(_log_file()))
            icon.stop()

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon(APP_NAME, self._image(), APP_NAME, menu=self._menu())
        self.icon.run(setup=self._setup)
        self.icon = None


async def _count_enabled(svc) -> int:
    from sqlalchemy import func, select

    from app.models import StrategyConfig

    async with svc.db.session() as s:
        return int(await s.scalar(select(func.count()).select_from(StrategyConfig).where(StrategyConfig.enabled.is_(True))) or 0)


# ---------------------------------------------------------------------------- main
def _raise_interrupt(*_):
    raise KeyboardInterrupt


def _self_test() -> None:
    """실행파일에 트레이/첫 실행 창/서버에 필요한 모듈과 리소스가 모두 들어있는지 확인한다."""
    import importlib

    base, _, _ = _paths()
    required = ["pystray", "PIL.Image", "uvicorn", "app.main", "aiosqlite", "greenlet"]
    if os.name == "nt":
        required.append("tkinter")
    missing = []
    for mod in required:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod}: {e}")
    for res in ("web/index.html", "web/js/common.js"):
        if not (base / res).exists():
            missing.append(f"리소스 없음: {res}")
    print("SELF-TEST FAIL" if missing else "SELF-TEST OK", *missing, sep="\n", flush=True)  # 빌드 스크립트가 이 한 줄을 검사
    if missing:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    _setup_stdio()
    if hasattr(signal, "SIGBREAK"):  # Windows 콘솔의 Ctrl+Break 도 Ctrl+C 처럼 정상 종료 처리
        signal.signal(signal.SIGBREAK, _raise_interrupt)
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--console", action="store_true", help="트레이 없이 콘솔에서 실행")
    ap.add_argument("--no-browser", action="store_true", help="시작 시 브라우저를 열지 않음")
    ap.add_argument("--self-test", action="store_true", help="번들 구성 점검 후 종료 (빌드 검증용)")
    args = ap.parse_args(argv)
    open_browser = not args.no_browser and os.environ.get("COIN_TRADER_NO_BROWSER") != "1"
    if args.self_test:
        _self_test()
        return
    use_tray = not args.console
    if use_tray:
        try:
            import pystray  # noqa: F401
        except Exception:  # noqa: BLE001 - 트레이 미지원 환경은 콘솔 모드로
            use_tray = False
    _ensure_env(prefer_gui=use_tray or sys.stdin is None)

    existing = _existing_instance()
    if existing:
        print(f"이미 실행 중입니다: {existing}")
        if open_browser:
            webbrowser.open(existing)
        return

    from app.config import DATA_DIR, get_settings

    s = get_settings()
    port = _free_port(s.host, s.port)
    browse_host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    url = f"http://{browse_host}:{port}"
    print(f"{APP_NAME} 시작: {url}  (데이터 폴더: {DATA_DIR})", flush=True)

    server = ServerThread(s.host, port)
    if not server.start():
        _message_box(f"서버를 시작하지 못했습니다: {server.error or '시간 초과'}\n로그: {_log_file()}")
        raise SystemExit(1)
    _instance_file().write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    try:
        if use_tray:
            TrayApp(server, url, open_browser).run()
        else:
            print(" 콘솔 모드: Ctrl+C 로 종료합니다.", flush=True)
            if open_browser:
                webbrowser.open(url)
            while server.thread.is_alive():
                server.thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        try:
            _instance_file().unlink()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - 창 없는 실행파일에서 오류를 사용자에게 보여준다
        import traceback

        traceback.print_exc()
        _message_box("예기치 않은 오류로 종료되었습니다.\n로그: " + str(_log_file()))
        sys.exit(1)
