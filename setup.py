"""
Build a macOS .app bundle:  python setup.py py2app
"""
from setuptools import setup

APP = ["app.py"]
DATA_FILES = [
    ("ui", ["ui/index.html", "ui/Device_Cyan.png"]),
    ("Icons", ["Icons/Icon_Mac.icns"]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "Icons/Icon_Mac.icns",
    "plist": {
        "CFBundleName": "GeekMagic Controller",
        "CFBundleDisplayName": "GeekMagic SmallTV-Ultra",
        "CFBundleIdentifier": "com.geekmagic.controller",
        "CFBundleShortVersionString": "2.0.0",
        "CFBundleVersion": "2.0.0",
        "CFBundleIconFile": "Icon_Mac.icns",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "App Developer - Zahidul Haque Tipu",
    },
    "packages": ["webview", "requests", "PIL"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
