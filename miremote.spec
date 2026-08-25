# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包 spec：单个 exe + 隐藏控制台窗口（PySide6/Qt GUI）
# 打包命令: pyinstaller miremote.spec

block_cipher = None

from PyInstaller.utils.hooks import collect_data_files

faster_whisper_datas = collect_data_files(
    'faster_whisper', includes=['assets/*.onnx']
)

import os
_datas = [('assets/remote.jpg', 'assets')]
# Frida Gadget 压缩包：exe 里哑键拦截（返回/音量）的必需资产。
# 仓库不含该二进制（约 7MB），下载后放 assets/ 即自动打包：
# https://github.com/frida/frida/releases/download/17.15.3/frida-gadget-17.15.3-windows-x86_64.dll.xz
_gadget = 'assets/frida-gadget-17.15.3-windows-x86_64.dll.xz'
if os.path.exists(_gadget):
    _datas.append((_gadget, 'assets'))
_datas = [*_datas, *faster_whisper_datas]

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        'winrt.windows.devices.bluetooth',
        'winrt.windows.devices.bluetooth.genericattributeprofile',
        'winrt.windows.storage.streams',
        'winrt.windows.foundation',
        'winrt.windows.foundation.collections',
        'winrt.windows.devices.radios',
        'winrt.windows.devices.enumeration',
        'faster_whisper',
        'ctranslate2',
        'sounddevice',
        # PySide6 / Qt
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # miremote 包内全部模块
        'miremote',
        'miremote.__main__',
        'miremote.gui',
        'miremote.service',
        'miremote.voice',
        'miremote.tray',
        'miremote.tray_qt',
        'miremote.backkey',
        'miremote.tapinject',
        'miremote.actions',
        'miremote.keys',
        'miremote.rawinput',
        'miremote.runner',
        'miremote.diagnose',
        'miremote.remote_widget',
        'miremote.learn_qt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pystray', 'PIL._tkinter_finder'],
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
    name='小米遥控器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 隐藏控制台窗口（GUI 应用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
