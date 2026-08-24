#!/bin/bash
# Claude Code hook — keep outbound SSH pointed at the debug agent account.
#
# THIS IS NOT A SECURITY BOUNDARY. It is a client-side guard that stops the
# obvious mistake (an agent reaching for root@ out of habit). Anything that
# runs a shell can bypass it. The real boundary is server-side: sshd
# AllowGroups/AllowUsers, the debugbot account's own privileges, and the
# sudoers rules the debug_agent role installs.
#
# It denies by default: if a command mentions ssh/scp/sftp/rsync and this
# script cannot prove the destination is <allowed-user>@host, it says no.
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
#
# Set BAY_DEBUG_AGENT_USER if you renamed debug_agent_user.

ALLOWED_USER="${BAY_DEBUG_AGENT_USER:-debugbot}"

allow() { exit 0; }

deny() {
  local reason="$1"
  jq -nc --arg r "$reason" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

INPUT=$(cat)
CMD=$(jq -r '.tool_input.command // empty' <<<"$INPUT" 2>/dev/null)

[ -n "$CMD" ] || allow

# Not an SSH-family command at all — none of our business.
grep -qP '\b(ssh|scp|sftp|rsync)\b' <<<"$CMD" || allow

BASE="SSH is only permitted as ${ALLOWED_USER}@. "

# ── No command chaining ──────────────────────────────────────────────────
# A second command after a legal ssh is the whole bypass, so nothing that
# can start one is allowed through.
if grep -qP '[;&|`]|\$\(|<\(|\n' <<<"$CMD"; then
  deny "${BASE}Command chaining (; && || | \` \$( ) is not allowed in an SSH command."
fi

# ── Tokenise ─────────────────────────────────────────────────────────────
read -ra TOK <<<"$CMD"
NTOK=${#TOK[@]}

# Drop leading VAR=value assignments.
START=0
while [ "$START" -lt "$NTOK" ]; do
  case "${TOK[$START]}" in
    [A-Za-z_]*=*) START=$((START + 1)) ;;
    *) break ;;
  esac
done

[ "$START" -lt "$NTOK" ] || deny "${BASE}Could not parse the command."

PROG=$(basename -- "${TOK[$START]}")
case "$PROG" in
  ssh|scp|sftp|rsync) ;;
  *) deny "${BASE}An SSH-family command must be the whole command, not embedded in another one (found: ${PROG})." ;;
esac

# ── Every user@host in the command must be the allowed user ──────────────
# Catches `ssh root@h ... # debugbot@`, `-J root@bastion`, a second host in
# an scp argument, and anything else carrying a login name.
while read -r pair; do
  [ -n "$pair" ] || continue
  if [ "${pair%%@*}" != "$ALLOWED_USER" ]; then
    deny "${BASE}Refused: the command logs in as '${pair%%@*}' (${pair})."
  fi
done < <(grep -oP '[A-Za-z0-9._%+-]+@[A-Za-z0-9._-]+' <<<"$CMD" || true)

# ── Option checks ────────────────────────────────────────────────────────
check_option_value() {
  local val
  val=$(tr '[:upper:]' '[:lower:]' <<<"$1")
  case "${val// /}" in
    proxycommand*|proxyjump*|user=*|remotecommand*|localcommand*|permitlocalcommand*)
      deny "${BASE}Option '$1' can redirect the connection or run another command."
      ;;
  esac
}

REMOTE_SEEN=no
LFLAG_USER=""
DEST=""

i=$START
i=$((i + 1))
while [ "$i" -lt "$NTOK" ]; do
  t=${TOK[$i]}
  case "$t" in
    -J|--jump|-J*)
      deny "${BASE}ProxyJump (-J) is not allowed."
      ;;
    -F|-F*)
      deny "${BASE}An alternate ssh config (-F) can override the login user."
      ;;
    -e|--rsh|-e*|--rsh=*)
      deny "${BASE}A custom remote shell (-e/--rsh) is not allowed."
      ;;
    -o)
      check_option_value "${TOK[$((i + 1))]:-}"
      i=$((i + 2))
      continue
      ;;
    -o*)
      check_option_value "${t#-o}"
      i=$((i + 1))
      continue
      ;;
    -l)
      LFLAG_USER=${TOK[$((i + 1))]:-}
      i=$((i + 2))
      continue
      ;;
    -l*)
      LFLAG_USER=${t#-l}
      i=$((i + 1))
      continue
      ;;
    -i|-p|-b|-c|-D|-E|-I|-L|-m|-O|-Q|-R|-S|-W|-w|-P)
      i=$((i + 2))
      continue
      ;;
    --*=*|--*)
      i=$((i + 1))
      continue
      ;;
    -*)
      i=$((i + 1))
      continue
      ;;
    *)
      DEST=$t
      break
      ;;
  esac
done

if [ -n "$LFLAG_USER" ] && [ "$LFLAG_USER" != "$ALLOWED_USER" ]; then
  deny "${BASE}Refused: -l ${LFLAG_USER} logs in as another user."
fi

case "$PROG" in
  ssh|sftp)
    [ -n "$DEST" ] || deny "${BASE}No destination found in the command."
    case "$DEST" in
      *@*)
        [ "${DEST%%@*}" = "$ALLOWED_USER" ] ||
          deny "${BASE}Refused: destination '${DEST}' is not ${ALLOWED_USER}@."
        ;;
      *)
        [ "$LFLAG_USER" = "$ALLOWED_USER" ] ||
          deny "${BASE}Destination '${DEST}' has no ${ALLOWED_USER}@ prefix."
        ;;
    esac
    ;;
  scp|rsync)
    # At least one operand must be an explicit <allowed>@host:path, and the
    # user@host scan above already proved every login name is the right one.
    for t in "${TOK[@]:$START}"; do
      if grep -qP "^${ALLOWED_USER}@[A-Za-z0-9._-]+:" <<<"$t"; then
        REMOTE_SEEN=yes
      fi
    done
    [ "$REMOTE_SEEN" = yes ] ||
      deny "${BASE}${PROG} must address ${ALLOWED_USER}@host:path explicitly."
    ;;
esac

allow
