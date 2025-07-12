# -*- mode: python ; coding: utf-8 -*-
import sys
import platform

# Platform-specific hidden imports
hiddenimports_base = [
    'anthropic',
    'openai',
    'github',
    'pygithub',
    'urllib3',
    'certifi',
    'charset_normalizer',
    'idna',
    'requests',
    'typing_extensions',
    'pydantic',
    'pydantic_core',
    'annotated_types',
    'httpx',
    'httpcore',
    'h11',
    'anyio',
    'sniffio',
    'distro',
    'tqdm',
    'colorama',
    'pyjwt',
    'jwt',
    'wrapt',
    'deprecated',
    'dateutil',
    'python-dateutil',
    'nacl',
    'cffi',
    'pycparser',
    'cryptography',
]

# Add platform-specific imports
if sys.platform == 'win32':
    hiddenimports_base.extend(['win32com', 'win32api'])

a = Analysis(
    ['index.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports_base,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='release-notes-pls',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Disable UPX compression for better compatibility
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon support if needed
)

# Platform-specific build adjustments
if sys.platform == 'darwin':
    exe.target_arch = platform.machine()  # Properly detect ARM64 vs x86_64
