# Edge Agent unified terminal entrypoint. Existing provider binaries are not
# replaced; only the PATH precedence is changed for one-shot CLI forms.
typeset -gx EDGE_AGENT_ROOT="${EDGE_AGENT_ROOT:-$HOME/mac-agent}"
typeset -gx PATH="$EDGE_AGENT_ROOT/bin/terminal-shims:$PATH"
