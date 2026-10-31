#!/bin/sh
# Runs the plugin's bin/house-rules with the arguments it is given and the hook's stdin: `run.sh auto stop` is the Stop hook.
# The tool needs Python 3.10 or newer and the python3 macOS ships is 3.9, so the interpreter is looked for here. A machine without
# one, or a plugin without bin/house-rules, gets exit 0 and no output: this runs at the end of every turn, and a hook that failed on
# every turn would be switched off.

# Most turns end here: a session that committed nothing has no pending file, and starting Python to find that out costs about 0.1 s
# a turn. These are the first checks of `house-rules auto stop` (which keeps all its own and reads the input), made with shell builtins
# alone. The state directory is looked for in the order of state_dir() in house_rules/auto.py, an empty variable counting as unset.
[ "${HOUSE_RULES_REVIEW_CHILD:-}" != 1 ] || exit 0
[ -n "${CLAUDE_CODE_SESSION_ID:-}" ] || exit 0
state=${HOUSE_RULES_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/house-rules}
[ -e "$state/pending/$CLAUDE_CODE_SESSION_ID.jsonl" ] || exit 0

root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
[ -f "$root/bin/house-rules" ] || exit 0

# The same test as the git hooks' (house_rules/hooks.py), for the same $HOUSE_RULES_PY.
python_ok() {
  "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

py=
if [ -n "${HOUSE_RULES_PY:-}" ] && python_ok "$HOUSE_RULES_PY"; then
  py=$HOUSE_RULES_PY
else
  # A name that carries its version needs no test, which would start Python once more.
  for name in python3.14 python3.13 python3.12 python3.11 python3.10; do
    py=$(command -v "$name") && break
  done
  if [ -z "$py" ] && python_ok python3; then
    py=python3
  fi
fi
[ -n "$py" ] || exit 0

# Not `exec`, so that the status can be changed: exit status 2 of a Stop hook makes Claude go on with the turn and take stderr as its
# instruction, and the tool exits 2 on a usage error (a bin/house-rules from before `auto stop`), as Python does on a script it cannot
# open. The turn would go on at every Stop. 1 is a failure that is shown and stops nothing.
"$py" "$root/bin/house-rules" "$@"
status=$?
[ "$status" -ne 2 ] || status=1
exit "$status"
