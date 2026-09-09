# jvuey console autostart - source this from ~/.zshrc
#
#   source /path/to/patchovision/autostart.zsh
#
# Put it BEFORE any block that launches tmux or another long-running program.
# Such a block does not return until that program exits, so anything after it
# is unreachable on the console.
#
# Runs the selector only on the machine's first virtual console, so SSH
# sessions (which get a pts device) always land at an ordinary shell and you
# can never lock yourself out.
#
# Off switch: touch ~/.jvuey-stop     Back on: rm ~/.jvuey-stop

# Honour an explicit location (NixOS sources this from /etc/zshrc, where the
# script's own path is not where the rest of the project lives); otherwise
# resolve our own directory.
JVUEY_DIR="${JVUEY_DIR:-${0:A:h}}"
JVUEY_CONSOLE="${JVUEY_CONSOLE:-/dev/tty1}"

if [ -z "$TMUX" ] && [ "$(tty)" = "$JVUEY_CONSOLE" ] \
   && [ -x "$JVUEY_DIR/jvuey-loop.sh" ] && [ ! -e "$HOME/.jvuey-stop" ]; then
  # Its own named session, so a bare "tmux attach" elsewhere never joins the
  # television and starts driving it.
  if tmux has-session -t jvuey 2>/dev/null; then
    exec tmux attach -t jvuey
  else
    exec tmux new -s jvuey "$JVUEY_DIR/jvuey-loop.sh"
  fi
fi
