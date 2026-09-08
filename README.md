# patchovision

A Jellyfin front-end for a TV-connected Linux box with no desktop: a terminal
selector on the console, mpv rendering straight to DRM/KMS, and a phone remote
you reach in a browser.

There is no compositor, no X, no Wayland. The selector draws in the framebuffer
and mpv takes the display directly, which is what keeps playback clean on
modest hardware.

## The pieces

| File | What it does |
|---|---|
| `jvuey23.py` | The selector. Browses Jellyfin, then `exec`s mpv with your DRM connector, mode and audio device. |
| `jvuey-remote.py` | The phone remote. A small web server; standard library only. |
| `jvuey-loop.sh` | Restarts the selector after mpv exits, and waits for the server at boot. |
| `autostart.zsh` | Optional. Starts the selector on the TV console at login. |
| `install.sh` | Sets up the remote as a user service. |

`configuration.nix`, `hardware-configuration.nix`, `flake.nix` and `.zshrc` are
the NixOS configuration for the machine this was built on. They are here as a
working reference, not as something to deploy as-is.

## How the remote works

Your phone does not get its own copy of the menu. It sends *keystrokes*, and
the menu stays on the television where it already is. A small web server sits
in the middle and routes each press by looking at which Unix socket is
currently accepting connections:

```
                          ┌──────────────────────────┐
  phone  ──── Wi-Fi ────▶ │ jvuey-remote.py  :8080   │
  /remote                 └────────────┬─────────────┘
                                       │
                      jvuey-tui.sock ──┼── the selector   (arrows, OK, search)
                      jvuey-mpv.sock ──┘── mpv            (pause, seek, chapters)
```

Only one of those is ever up, so which one answers tells the server what is on
screen, and the page switches between a d-pad and transport controls by itself.

Two consequences worth knowing. Because the phone only sends keystrokes, no
Jellyfin credentials or stream URLs ever reach it. And because the selector
still `exec`s mpv exactly as before, playback is untouched: the only addition
to the mpv command line is `--input-ipc-server`, which opens a socket and
changes nothing about decoding, output or timing.

## Requirements

- Linux with systemd, on a machine whose console you can log into
- [`uv`](https://docs.astral.sh/uv/) — the Python scripts declare their own deps
- `mpv` built with DRM support (`mpv --vo=help` should list `drm`)
- `python3`
- `tmux`, only for the console autostart
- A Jellyfin server, and an API key

Tested on NixOS 25.11 with mpv 0.41 and Textual 8.

## Install

```sh
git clone https://github.com/vladlab/patchovision.git
cd patchovision
./install.sh
```

That checks your tools, writes a systemd user unit pointing at wherever you
cloned it, starts it, and enables lingering so it survives logout. It then
tells you the URL.

Two things it deliberately leaves to you:

**Open the firewall.** Nothing reaches the port until you do.

```sh
# NixOS - in configuration.nix, then rebuild
networking.firewall.allowedTCPPorts = [ 8080 ];

# ufw
sudo ufw allow 8080/tcp
```

**Start the selector on the console.** Either run `./jvuey-loop.sh` by hand, or
add this to `~/.zshrc` — **above** any block that launches tmux, because such a
block does not return until tmux exits and anything after it is unreachable:

```sh
source /path/to/patchovision/autostart.zsh
```

It only fires on `/dev/tty1` (override with `JVUEY_CONSOLE`), so SSH sessions
always get an ordinary shell and you cannot lock yourself out. Set
`touch ~/.jvuey-stop` to disable it, `rm ~/.jvuey-stop` to re-enable.

First run of `jvuey23.py` walks you through connecting to a server, including
Jellyfin Quick Connect.

## On NixOS

The declarative equivalents, for `configuration.nix`:

```nix
networking.firewall.allowedTCPPorts = [ 8080 ];
users.users.<you>.linger = true;          # instead of loginctl enable-linger
services.getty.autologinUser = "<you>";   # if you want the TV to boot to the menu
```

If you want the remote on port 80 rather than 8080, that is a kernel setting —
a file in `/etc/sysctl.d/` will not survive a rebuild:

```nix
boot.kernel.sysctl."net.ipv4.ip_unprivileged_port_start" = 80;
```

## Configuration

`~/.config/jvuey/config.ini`, written by the selector and kept at mode 600
because it holds API keys in clear text.

```ini
[general]
active_server = myserver

[server.myserver]
url = http://192.168.1.10:8096
api_key = ...
user_id = ...

[mpv]
drm_connector = HDMI-A-1
drm_mode = 1
audio_device = auto
audio_spdif =
```

Set the mpv values from the selector's settings screen (`s`), which reads the
real connectors and modes off your hardware. `mpv.conf` in this repo is the
matching player configuration — copy it to `~/.config/mpv/` if you want the
same output setup.

## Security

The remote has no authentication: anyone who can reach the port can drive your
television. That is fine on a home network and is the intended scope. Do not
forward a port to it from the internet — put it behind Tailscale or a VPN if
you want it from outside the house.

## Troubleshooting

**The page says "Idle".** Nothing is running for it to talk to. Start
`./jvuey-loop.sh` on the console.

**The phone cannot load the page.** The firewall, almost always. Check the
server is listening with `ss -ltn | grep 8080`, then check your firewall rules.

**The service dies with "uv: not found".** systemd user services get a minimal
PATH. Re-run `./install.sh`, which pins the directory `uv` actually lives in.

**Buttons do nothing during playback.** Check the socket exists while a title
is playing: `ls -l "${XDG_RUNTIME_DIR:-/tmp}"/jvuey-mpv.sock`. If it is missing,
confirm `--input-ipc-server` reached mpv:
`tr '\0' '\n' < /proc/$(pgrep -x mpv)/cmdline`.

**The menu never comes back after a title ends.** You started `jvuey23.py`
directly instead of `jvuey-loop.sh`. The selector hands the whole process over
to mpv, so something has to restart it.

**Logs.** `journalctl --user -u jvuey-remote -n 50`

## Uninstall

```sh
./install.sh --uninstall
```

Then remove the `source .../autostart.zsh` line from `~/.zshrc` and close the
firewall port.
