# Build on the target OS: uv run --no-sync pyinstaller --noconfirm desktop/desktop.spec
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
resources = root / "build/desktop-resources"
if not (resources / "fonts/NotoSansCJKsc-Regular.otf").is_file():
    raise SystemExit("Run python desktop/prepare_resources.py before packaging")

datas = [
    (str(root / "config.example.toml"), "."),
    (str(root / "desktop/ui"), "desktop/ui"),
    (str(root / "webui"), "webui"),
    (str(resources), "resource"),
    (str(root / "LICENSE"), "."),
    (str(root / "docs/DESKTOP.md"), "docs"),
    (str(root / "app/services/data"), "app/services/data"),
]
datas += collect_data_files("app")
hidden = collect_submodules("app") + ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]
binaries = []
for name in ("streamlit", "streamlit_tour", "litellm", "tiktoken", "imageio_ffmpeg", "azure.cognitiveservices.speech", "ctranslate2", "faster_whisper"):
    data, libs, imports = collect_all(name)
    datas += data
    binaries += libs
    hidden += imports
for name in ("streamlit", "streamlit-tour", "litellm", "imageio", "imageio-ffmpeg"):
    datas += copy_metadata(name)
datas += collect_data_files("webview")
hidden += ["webview.platforms." + ("winforms" if sys.platform == "win32" else "cocoa" if sys.platform == "darwin" else "qt")]
excluded = ["PyQt5", "PyQt6", "PySide2", "cefpython3", "matplotlib", "IPython", "pytest"]
if sys.platform != "linux":
    excluded += ["PySide6", "qtpy", "gi"]

a = Analysis([str(root / "desktop.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hidden, excludes=excluded)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MoneyPrinterTurbo",
          debug=False, strip=False, upx=False, console=sys.platform == "linux")
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MoneyPrinterTurbo")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="MoneyPrinterTurbo.app", bundle_identifier="io.moneyprinterturbo.desktop",
                 info_plist={"NSHighResolutionCapable": True})
