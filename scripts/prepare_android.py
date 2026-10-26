#!/usr/bin/env python3
"""`npx cap add android` で生成された android/ に、このアプリ用の変更を加える（何度実行しても同じ結果）。

1. TermuxLauncherPlugin（TermuxのRUN_COMMANDでブリッジを起動する）を追加し、MainActivityで登録する
2. AndroidManifest.xml に
   - com.termux.permission.RUN_COMMAND 権限
   - Termuxを見つけるための <queries>
   - http://127.0.0.1（Termuxのブリッジ）と通信するための usesCleartextTraffic
   を追加する
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"
OVERLAY = ROOT / "android-overlay"


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def find_main_activity():
    java_root = ANDROID / "app" / "src" / "main" / "java"
    found = list(java_root.rglob("MainActivity.java")) if java_root.exists() else []
    if not found:
        fail(f"MainActivity.java が見つかりません（{java_root}）。先に `npx cap add android` を実行してください")
    return found[0]


def install_java(main_activity):
    text = main_activity.read_text(encoding="utf-8")
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    if not m:
        fail("MainActivity.java のpackage宣言を読み取れません")
    package = m.group(1)
    for tmpl in ("MainActivity.java.tmpl", "TermuxLauncherPlugin.java.tmpl"):
        body = (OVERLAY / tmpl).read_text(encoding="utf-8").replace("__PACKAGE__", package)
        dest = main_activity.parent / tmpl.removesuffix(".tmpl")
        dest.write_text(body, encoding="utf-8")
        print(f"wrote {dest.relative_to(ROOT)}")
    return package


def patch_manifest():
    path = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
    if not path.exists():
        fail(f"{path} が見つかりません")
    s = path.read_text(encoding="utf-8")
    changed = []

    if "usesCleartextTraffic" not in s:
        s, n = re.subn(r"<application\b", '<application android:usesCleartextTraffic="true"', s, count=1)
        if n != 1:
            fail("AndroidManifest.xml に <application> が見つかりません")
        changed.append("usesCleartextTraffic")

    additions = []
    if "com.termux.permission.RUN_COMMAND" not in s:
        additions.append('    <uses-permission android:name="com.termux.permission.RUN_COMMAND" />')
        changed.append("RUN_COMMAND permission")
    if "<queries>" not in s:
        additions.append('    <queries>\n        <package android:name="com.termux" />\n    </queries>')
        changed.append("queries(com.termux)")
    if additions:
        if "</manifest>" not in s:
            fail("AndroidManifest.xml に </manifest> が見つかりません")
        s = s.replace("</manifest>", "\n".join(additions) + "\n</manifest>", 1)

    path.write_text(s, encoding="utf-8")
    print("manifest: " + (", ".join(changed) if changed else "already patched"))


def main():
    if not ANDROID.exists():
        fail("android/ がありません。先に `npx cap add android` を実行してください")
    package = install_java(find_main_activity())
    patch_manifest()
    print(f"done (package {package})")


if __name__ == "__main__":
    main()
