from pathlib import Path


project_root = Path(SPEC).resolve().parent

analysis = Analysis(
    [str(project_root / "desktop_window.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "templates"), "templates"),
        (str(project_root / "static"), "static"),
        (str(project_root / "billiard_app" / "schema.sql"), "billiard_app"),
        (str(project_root / "billiard_app" / "migrations"), "billiard_app/migrations"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["cefpython3", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="BilliardManagerWindow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BilliardManagerWindow",
)
