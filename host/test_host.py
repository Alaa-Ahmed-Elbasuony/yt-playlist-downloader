#!/usr/bin/env python3
"""Talk to the native host exactly like the browser does, from the command line.

  py host\\test_host.py check
  py host\\test_host.py info  "https://www.youtube.com/playlist?list=..."
  py host\\test_host.py sizes "https://www.youtube.com/playlist?list=..."
  py host\\test_host.py download "https://www.youtube.com/playlist?list=..." [--quality 720] [--audio]
                                 [--folder DIR] [--items 1,3,5]

By default it launches host\\yt_host.bat (what the browser runs, created by
install.ps1). Use --direct to run yt_host.py with this Python instead.
Press Ctrl+C during a download to send a cancel request.
"""

import argparse
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FINAL = {"check", "info", "done", "error", "sizes_done"}


def send(proc, message):
    data = json.dumps(message).encode("utf-8")
    proc.stdin.write(struct.pack("<I", len(data)) + data)
    proc.stdin.flush()


def receive(proc):
    header = proc.stdout.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    return json.loads(proc.stdout.read(length).decode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "info", "sizes", "download"])
    parser.add_argument("url", nargs="?")
    parser.add_argument("--quality", default="1080", choices=["360", "720", "1080", "best"])
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--folder", default="")
    parser.add_argument("--items", default="", help="playlist positions to download, e.g. 1,3,5 (default: all)")
    parser.add_argument("--direct", action="store_true", help="run yt_host.py directly instead of the .bat")
    opts = parser.parse_args()

    if opts.direct:
        cmd = [sys.executable, os.path.join(HERE, "yt_host.py")]
    else:
        launcher = os.path.join(HERE, "yt_host.bat")
        if not os.path.exists(launcher):
            sys.exit("host\\yt_host.bat not found: run install.cmd first, or pass --direct")
        cmd = [launcher]

    request = {"type": opts.command}
    if opts.command != "check":
        if not opts.url:
            sys.exit("a URL is required for " + opts.command)
        request["url"] = opts.url
    if opts.command == "download":
        request.update(quality=opts.quality, audioOnly=opts.audio, folder=opts.folder,
                       items=[int(i) for i in opts.items.split(",")] if opts.items else None)

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    send(proc, request)
    try:
        while True:
            try:
                message = receive(proc)
            except KeyboardInterrupt:
                print("-> sending cancel")
                send(proc, {"type": "cancel"})
                continue
            if message is None:
                print("host closed the connection (see host\\yt_host.log)")
                break
            print(json.dumps(message, ensure_ascii=False))
            if message.get("type") in FINAL:
                break
    finally:
        proc.stdin.close()
        proc.wait(timeout=20)


if __name__ == "__main__":
    main()
