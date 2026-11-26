# PyInstaller spec for the Windows app. Build from the repo root:
#   pyinstaller packaging/redtooth.spec --noconfirm
# Produces dist/Redtooth/ with RedtoothApp.exe (no console) and redtooth.exe (console), sharing one set of files.
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = collect_data_files("redtooth")
hiddenimports = collect_submodules("redtooth")
binaries = []
for pkg in ("onnxruntime", "tokenizers", "pptx", "docx", "pypdf", "reportlab", "sqlite_vec", "aiohttp", "PIL",
            "pystray", "pycaw", "comtypes", "win11toast", "winrt"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
)
pyz = PYZ(a.pure)
common = dict(exclude_binaries=True, debug=False, strip=False, upx=False, icon="redtooth.ico")
app = EXE(pyz, a.scripts, [], name="RedtoothApp", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="redtooth", console=True, **common)
coll = COLLECT(app, cli, a.binaries, a.datas, strip=False, upx=False, name="Redtooth")
