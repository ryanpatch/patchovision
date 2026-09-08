#!/bin/sh
# Keeps the jvuey selector on screen.
#
# jvuey replaces itself with mpv when you start a title (that is deliberate -
# mpv gets the console directly, with nothing wrapped around it). So when mpv
# finishes, the whole thing has exited. This loop brings the menu back.
#
# Off switch:  touch ~/.jvuey-stop     (stops the loop, and keeps it stopped
#                                       across logins and reboots)
# Back on:     rm ~/.jvuey-stop

# Resolve our own directory so this works wherever the repo is checked out.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
JVUEY="$HERE/jvuey23.py"

if [ ! -x "$JVUEY" ]; then
    echo "jvuey-loop: cannot find an executable $JVUEY" >&2
    exit 1
fi

# Wait for the Jellyfin server before launching. On a Wi-Fi box the console
# autologin can beat the wireless association by several seconds at boot, and
# jvuey fetches its library exactly once at startup - so without this you get
# an "unreachable" error screen and have to quit and relaunch by hand.
wait_for_server() {
    python3 - <<'PY'
import configparser, os, socket, sys, time, urllib.parse
from pathlib import Path

candidates = [
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jvuey" / "config.ini",
    Path.home() / ".jvuey.ini",
]
config = configparser.ConfigParser()
for path in candidates:
    if path.exists():
        config.read(path)
        break
else:
    sys.exit(0)          # nothing configured yet - let jvuey run its own setup

try:
    server = config[f"server.{config['general']['active_server']}"]["url"]
except KeyError:
    sys.exit(0)

parsed = urllib.parse.urlparse(server)
host = parsed.hostname
port = parsed.port or (443 if parsed.scheme == "https" else 80)
if not host:
    sys.exit(0)

deadline = time.monotonic() + 90
attempt = 0
while time.monotonic() < deadline:
    try:
        socket.create_connection((host, port), timeout=3).close()
        if attempt:
            print(f"jvuey: {host}:{port} reachable after {attempt} tries", flush=True)
        sys.exit(0)
    except OSError:
        if attempt == 0:
            print(f"jvuey: waiting for {host}:{port} ...", flush=True)
        attempt += 1
        time.sleep(2)
print(f"jvuey: {host}:{port} still unreachable - starting anyway", flush=True)
PY
}

while [ ! -e "$HOME/.jvuey-stop" ]; do
    wait_for_server
    "$JVUEY" "$@"
    sleep 1
done

echo "jvuey loop stopped (~/.jvuey-stop exists). Remove it to re-enable."
