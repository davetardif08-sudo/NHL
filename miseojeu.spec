# -*- mode: python ; coding: utf-8 -*-
"""
Spec PyInstaller pour Mise-O-Jeu Analyzer
Build : venv\Scripts\pyinstaller.exe miseojeu.spec
"""

import os

block_cipher = None

# Ressources à inclure dans l'exe (templates, static)
added_files = [
    ('templates', 'templates'),
    ('static',    'static'),
]

a = Analysis(
    ['app_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        # Flask et ses dépendances
        'flask',
        'flask.templating',
        'jinja2',
        'jinja2.ext',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.routing',
        'werkzeug.exceptions',
        'werkzeug.middleware.shared_data',
        'click',
        # Modules de l'app
        'predictions',
        'app',
        'requests',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',
        # Stdlib couramment manqués
        'email.mime.multipart',
        'email.mime.text',
        'pkg_resources',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclure ce qui n'est pas nécessaire (réduit la taille)
        'playwright',
        'lxml',
        'bs4',
        'beautifulsoup4',
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'scipy',
        'IPython',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MiseOJeu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # True = fenêtre console visible (pour voir les erreurs)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
