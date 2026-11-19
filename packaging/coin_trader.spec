# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙: python -m PyInstaller packaging/coin_trader.spec --noconfirm
# 결과물: dist/CoinTrader/CoinTrader(.exe)  (onedir: 시작이 빠르고 백신 오탐이 적음)
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "icon.ico")

hiddenimports = (
    collect_submodules("app")
    + collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + collect_submodules("pystray")
    + [
        "aiosqlite",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "sqlalchemy.ext.asyncio",
        "greenlet",
        "itsdangerous",
        "multipart",
        "python_multipart",
        "feedparser",
        "bs4",
        "jwt",
        "cryptography.fernet",
        "PIL.Image",
        "PIL.ImageDraw",
        "tkinter",
        "tkinter.simpledialog",
        "tkinter.messagebox",
    ]
)

a = Analysis(
    [os.path.join(ROOT, "app", "launcher.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "web"), "web")] + ([(ICON, "assets")] if os.path.exists(ICON) else []),
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "IPython", "pytest", "respx", "uvloop"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CoinTrader",
    console=False,  # 트레이 아이콘으로 백그라운드 실행 (로그는 데이터 폴더의 logs/ 에 기록)
    icon=ICON if os.path.exists(ICON) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CoinTrader", upx=False)
