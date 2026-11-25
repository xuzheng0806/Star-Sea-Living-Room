# -*- coding: utf-8 -*-
"""启动器：若 127.0.0.1:8866 未运行则无窗拉起 server.py，随后打开浏览器。"""
import os
import subprocess
import time
import urllib.request
import webbrowser

URL = "http://127.0.0.1:8866"
APP_DIR = r"F:\星海小客厅"
PYTHONW = r"F:\ComfyUI\venv\Scripts\pythonw.exe"
SERVER_PY = os.path.join(APP_DIR, "server.py")


def is_up():
    try:
        with urllib.request.urlopen(URL + "/api/history", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    if not is_up():
        subprocess.Popen([PYTHONW, "-X", "utf8", SERVER_PY], cwd=APP_DIR)
        for _ in range(25):
            if is_up():
                break
            time.sleep(0.3)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
