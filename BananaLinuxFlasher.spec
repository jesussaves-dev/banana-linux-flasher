# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build specification for the BananaLinux Flasher.

Running this spec produces a single self-contained ``BananaLinuxFlasher.exe``
(``--onefile``) that opens the Tk GUI with no background console window
(``--noconsole`` / ``console=False``).

Build it with:

    pyinstaller --clean --noconfirm BananaLinuxFlasher.spec

The resulting binary lives at ``dist/BananaLinuxFlasher.exe``.

Antivirus false positives
-------------------------
Unsigned PyInstaller one-file executables are frequently flagged by Windows
Defender and other engines because malware uses the same packer. This spec
avoids the most common triggers:

* UPX compression is disabled (UPX-packed binaries are the #1 heuristic hit).
* A proper Windows version resource (company/product/description/version) is
  embedded; executables with no metadata score as more suspicious.
* CI builds PyInstaller's bootloader from source (see build-exe.yml) so the
  binary does not share the exact bootloader bytes that malware samples use.

The only complete fix is Authenticode code signing.
"""

import os
import re

project_root = os.path.abspath(SPECPATH)
docs_dir = os.path.join(project_root, "docs")

# --- Icon assets -----------------------------------------------------------
# Bundle the application icons when present in the checkout so the window and
# taskbar show the banana branding in the frozen build too.
icon_ico = os.path.join(docs_dir, "banana.ico")
icon_png = os.path.join(docs_dir, "banana.png")

datas = [(path, "docs") for path in (icon_ico, icon_png) if os.path.exists(path)]
app_icon = icon_ico if os.path.exists(icon_ico) else None

# --- Windows version resource ---------------------------------------------
with open(os.path.join(project_root, "flash_banana.py"), encoding="utf-8") as fh:
    _match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
app_version = _match.group(1) if _match else "0.0.0"
_parts = [int(p) for p in re.findall(r"\d+", app_version)[:4]]
_ver_tuple = tuple((_parts + [0, 0, 0, 0])[:4])

version_info = None
if os.name == "nt":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_ver_tuple, prodvers=_ver_tuple,
            mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "BananaLinux Community"),
                StringStruct("FileDescription",
                             "BananaLinux Flasher - microSD image writer"),
                StringStruct("FileVersion", app_version),
                StringStruct("InternalName", "BananaLinuxFlasher"),
                StringStruct("LegalCopyright", "Open source, see README"),
                StringStruct("OriginalFilename", "BananaLinuxFlasher.exe"),
                StringStruct("ProductName", "BananaLinux Flasher"),
                StringStruct("ProductVersion", app_version),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

# --- Hidden imports --------------------------------------------------------
# Tkinter loads several of its sub-modules lazily, and urllib only pulls in
# IDNA support when a non-ASCII host is used. Listing them explicitly means
# PyInstaller's static analysis can never miss them.
hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "encodings.idna",
]

# Keep the build lean: the app depends on the standard library only, so any
# heavyweight third-party stacks that leak in transitively are excluded.
excludes = [
    "numpy",
    "scipy",
    "matplotlib",
    "pandas",
    "PIL",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "setuptools",
    "pip",
]


a = Analysis(
    ["flash_banana.py"],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BananaLinuxFlasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX packing is a major antivirus trigger
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # --noconsole: no background terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon,
    version=version_info,
    uac_admin=False,          # GUI runs as the user; the write elevates on demand
)