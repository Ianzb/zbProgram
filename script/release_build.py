#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from config import *

import argparse
import os
import re
import sys
import json
import shutil
import subprocess

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str):
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def write_text(path: str, content: str):
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)


def replace_version_in_program(version: str, version_code: int):
    text = read_text(PROGRAM_PY)
    pattern = re.compile(r"(\bVERSION\s*=\s*)([\"'])(.*?)(\2)", flags=re.M)
    text, n = pattern.subn(lambda m: m.group(1) + m.group(2) + version + m.group(2), text, count=1)

    pattern = re.compile(r"(\bVERSION_CODE\s*=\s*)(\d+)", flags=re.M)
    text, n = pattern.subn(lambda m: m.group(1) + str(version_code), text, count=1)
    write_text(PROGRAM_PY, text)
    print("已修改program.py版本号！")


def replace_version_in_setup(version: str):
    text = read_text(SETUP_ISS)
    pattern = re.compile(r'(#define\s+MyAppVersion\s+")([^"]*)(\")', flags=re.M)
    new_text, n = pattern.subn(lambda m: m.group(1) + version + m.group(3), text, count=1)
    write_text(SETUP_ISS, new_text)
    print("已修改setup.iss版本号！")


def replace_index_json(version: str, version_code: int):
    data = json.loads(read_text(INDEX_JSON))
    data["version"] = version
    data["version_code"] = version_code
    write_text(INDEX_JSON, json.dumps(data, ensure_ascii=False, indent=4))
    print("已修改index.json版本号！")


def extract_release_notes():
    html = read_text(INDEX_HTML)
    if LOG_INDEX:
        m = re.search(rf'<div\s+class="zb">\s*<h5>\s*{LOG_INDEX}\s*</h5>(.*?)</div>', html, flags=re.S)
    else:
        m = re.search(r'<div\s+class="zb">(.*?)</div>', html, flags=re.S)
    inner = m.group(1)
    parts = re.split(r"<br\s*/?>|\r|\n", inner)
    cleaned = [re.sub(r"<.*?>", "", p).strip() for p in parts]
    cleaned = [c for c in cleaned if c]
    last = cleaned[-1]
    last = re.sub(r"^\s*\d{4}-\d{2}-\d{2}：\s*[^：]+：\s*", "", last)
    last = re.sub(r"^\s*\d+(?:\.\d+){0,}：\s*", "", last).strip()
    print("已读取更新日志！")
    return last


def compile_exe():
    zb.deletePath(BUILD_PATH)
    zb.createDir(BUILD_PATH)
    if USE_NUITKA:
        cmd = [sys.executable, "-m", "nuitka", "--clang", "--assume-yes-for-downloads", "--show-progress", f"--force-stdout-spec={zb.joinPath(zb.TEMP_PATH, NAME + ".log")}", f"--force-stderr-spec={zb.joinPath(zb.TEMP_PATH, NAME + ".log")}",
               "--standalone", "--windows-console-mode=disable", "--enable-plugin=pyside6", *[f"--include-package={i}" for i in EXTRA_LIBS], *[f"--include-package-data={i}" for i in EXTRA_LIBS],
               "--remove-output", f"--output-dir={BUILD_PATH}", "--follow-imports", "--show-scons", f"--windows-icon-from-ico={ICON_PATH}",
               f"--output-folder-name={NAME}", f"--output-filename={NAME}" + (" --onefile" if IS_SINGLE_FILE else ""),
               f"--include-data-dir={RESOURCE_PATH}={zb.getFileName(RESOURCE_PATH)}", MAIN_PYW
               ]
    else:
        cmd = [sys.executable, "-m", "PyInstaller", SPEC_PATH,
               "--distpath", BUILD_PATH, "--workpath", zb.joinPath(BUILD_PATH, "build"),
               "--clean", "-y"
               ]

    print("CMD:", " ".join(cmd))
    subprocess.check_call(cmd)
    if USE_NUITKA:
        zb.movePath(zb.joinPath(BUILD_PATH, f"{NAME}.dist"), zb.joinPath(BUILD_PATH, NAME))
    zb.deletePath(zb.joinPath(BUILD_PATH, NAME, NAME + ".pdb"))
    print("打包完成")


def make_zip(version: str):
    print(f"正在压缩...")
    zip_path = shutil.make_archive(zb.joinPath(ROOT, f"{NAME}_{version}"), "zip", root_dir=zb.joinPath(BUILD_PATH, NAME))
    print(f"压缩完成！")
    return zip_path


def get_current_version_code():
    data = json.loads(read_text(INDEX_JSON))
    return data.get("version_code", 0)


def get_current_version():
    data = json.loads(read_text(INDEX_JSON))
    return data.get("version", "")


def copy_extra_files():
    for file in EXTRA_FILES:
        zb.copyPath(zb.joinPath(ROOT, file), zb.joinPath(BUILD_PATH, NAME, file))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--version", required=False, help="版本号")
    args = parser.parse_args()
    version = args.version

    release_notes = extract_release_notes()

    # 获取当前版本和版本代码
    current_version = get_current_version()
    current_version_code = get_current_version_code()
    if not version:
        version = current_version

    # 如果版本号发生变化，则增加版本代码
    if current_version != version:
        new_version_code = current_version_code + 1
        print(f"版本号从 {current_version} 变为 {version}, 增加版本代码到 {new_version_code}")
    else:
        new_version_code = current_version_code
        print(f"版本号未变化，保持版本代码为 {new_version_code}")

    replace_index_json(version, new_version_code)
    replace_version_in_program(version, new_version_code)
    if IS_SETUP:
        replace_version_in_setup(version)

    compile_exe()
    if IS_SINGLE_FILE:
        zb.copyPath(zb.joinPath(BUILD_PATH, f"{NAME}.exe"), zb.joinPath(ROOT, f"{NAME}_{version}.exe"))
        zip_path = zb.joinPath(ROOT, f"{NAME}_{version}.exe")
    else:
        copy_extra_files()
        zip_path = make_zip(version)

    out = {
        "version": version,
        "version_code": new_version_code,
        "release_notes": release_notes,
        "output": zip_path,
        "build_path": zb.joinPath(BUILD_PATH, NAME),
    }

    out_path = zb.joinPath(ROOT, "script", "release_output.json")
    print("打包结果：", out)
    write_text(out_path, json.dumps(out, ensure_ascii=False, indent=4))
