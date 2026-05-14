#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件收纳箱 - 一键打包脚本
功能: 自动安装依赖, 用 PyInstaller 打包成单文件 exe
"""

import os
import sys
import re
import shutil
import subprocess

# 强制 stdout/stderr 用 UTF-8，避免 Windows 终端乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # 旧版 Python 忽略

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)


def run(cmd, check=True):
    """执行命令并实时输出"""
    print(f">>> {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败，返回码: {result.returncode}")
    return result.returncode


def main():
    print("=" * 64)
    print("           文件收纳箱  -  PyInstaller 一键打包")
    print("=" * 64)
    print()

    # 0. 读取版本号
    print("[0/4] 读取版本号...")
    main_py_path = os.path.join(BASE_DIR, "main.py")
    with open(main_py_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', content)
    if not m:
        print("[错误] 无法从 main.py 中解析 APP_VERSION")
        sys.exit(1)
    version = m.group(1)
    parts = version.split(".")
    major = parts[0] if len(parts) > 0 else "0"
    minor = parts[1] if len(parts) > 1 else "0"
    patch = parts[2] if len(parts) > 2 else "0"
    exe_name = f"文件收纳箱v{major}.{minor}"
    print(f"当前版本: {version}  => 打包名称: {exe_name}.exe")
    if patch != "0":
        print()
        print(f"[警告] 版本号 patch 位为 {patch}（非 0），说明还在调试中。")
        print("       建议将版本号最后一位改为 0 后再打包。")
        print("       按任意键继续打包，或关闭窗口取消...")
        os.system("pause >nul")

    # 1. 检查 Python
    print()
    print("[1/4] 检查 Python 环境...")
    try:
        run([sys.executable, "--version"])
    except Exception:
        print("[错误] 未检测到 Python, 请先安装 Python 3.8+")
        print("       下载地址: https://www.python.org/downloads/")
        print("       安装时请务必勾选 'Add Python to PATH'")
        os.system("pause")
        sys.exit(1)

    # 2. 安装依赖
    print()
    print("[2/4] 安装/更新依赖 (pywebview, pyinstaller, opencv-python)...")
    try:
        run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=False)
        run([sys.executable, "-m", "pip", "install", "pywebview", "pyinstaller", "opencv-python", "pillow"])
    except Exception as e:
        print(f"[错误] 依赖安装失败: {e}")
        os.system("pause")
        sys.exit(1)

    # 3. 清理旧的构建产物
    print()
    print("[3/4] 清理旧的构建文件...")
    for name in ("build", "dist"):
        path = os.path.join(BASE_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f"  已删除 {name}/")
    for fname in os.listdir(BASE_DIR):
        if fname.endswith(".spec") and (fname.startswith("文件聚合管家") or fname.startswith("文件收纳箱")):
            os.remove(os.path.join(BASE_DIR, fname))
            print(f"  已删除 {fname}")

    # 4. 执行打包
    print()
    print("[4/4] 开始打包 (耗时约 30-90 秒, 请耐心等待)...")
    print()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", exe_name,
        "--add-data", "ui.html;.",
        "--add-data", "ui_icon.png;.",
        "--collect-all", "webview",
        "--collect-all", "cv2",
        "--collect-all", "PIL",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.mshtml",
        "--hidden-import", "webview.platforms.winforms",
        "main.py",
    ]
    try:
        run(cmd)
    except Exception as e:
        print(f"\n[错误] 打包失败: {e}")
        print("       查看控制台错误信息后重试")
        os.system("pause")
        sys.exit(1)

    # 完成
    print()
    print("=" * 64)
    print("                      [OK] 打包完成")
    print("=" * 64)
    print()
    dist_dir = os.path.join(BASE_DIR, "dist")
    exe_path = os.path.join(dist_dir, f"{exe_name}.exe")
    print(f" exe 位置: {exe_path}")
    print(" 双击即可使用, 可放到任何 Win10/Win11 电脑运行")
    print()
    print(" 注: 首次运行若提示需要 WebView2 Runtime, 系统会自动安装,")
    print("     Win10 1809 以上 / Win11 通常都已自带")
    print()

    if os.path.isdir(dist_dir):
        os.startfile(dist_dir)

    os.system("pause")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[致命错误] {e}")
        os.system("pause")
        sys.exit(1)
