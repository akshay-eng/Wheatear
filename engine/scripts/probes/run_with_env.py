"""Run a command with IBMUrl/IBMKey loaded from fish universal variables and
WXO_CONSOLE_COOKIE loaded from the HAR capture.

Only exists because fish's universal variables aren't exported into this shell
and its quoting rules make a one-liner painful. Nothing ships with this.
"""
import json
import os
import re
import subprocess
import sys

FISH_VARS = os.path.expanduser("~/.config/fish/fish_variables")
HAR = os.environ["WXO_HAR"]  # path to the console HAR capture


def _unescape(value: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)


env = dict(os.environ)
for line in open(FISH_VARS):
    m = re.match(r"SETUVAR (?:--export )?(\w+):(.*)", line.strip())
    if m and m.group(1) in ("IBMUrl", "IBMKey"):
        env[m.group(1)] = _unescape(m.group(2))

if "--cookie" in sys.argv:
    sys.argv.remove("--cookie")
    har = json.load(open(HAR))
    env["WXO_CONSOLE_COOKIE"] = next(
        h["value"]
        for e in har["log"]["entries"] if "catalogv3" in e["request"]["url"]
        for h in e["request"]["headers"] if h["name"].lower() == "cookie"
    )

sys.exit(subprocess.call(sys.argv[1:], env=env))
