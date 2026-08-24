#!/bin/bash
# Claude Code hook — restrict SSH access to the debug agent user only.
#
# Setup:
# 1. Copy to your consumer project: cp .bay/files/hooks/validate-ssh.sh .claude/hooks/
# 2. Register in .claude/settings.json:
#    {
#      "hooks": {
#        "PreToolUse": [
#          {
#            "matcher": "Bash",
#            "hooks": [
#              {
#                "type": "command",
#                "command": "bash .claude/hooks/validate-ssh.sh"
#              }
#            ]
#          }
#        ]
#      }
#    }

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command')

if grep -qP '\bssh\b' <<<"$CMD"; then
  if ! grep -qP '\bdebugbot@' <<<"$CMD"; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"SSH is only permitted as debugbot@. Configure debug_agent_enabled: true in group_vars to set up the debug agent."}}'
    exit 0
  fi
fi

exit 0
