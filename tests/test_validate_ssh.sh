#!/usr/bin/env bash
# Guard for files/hooks/validate-ssh.sh.
#
# The hook used to accept any command containing the string "debugbot@",
# so `ssh root@host cat /etc/shadow # debugbot@` sailed through. These
# cases pin the deny-by-default behaviour that replaced it.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/files/hooks/validate-ssh.sh"
PASS=0
FAIL=0

decide() {
  local out
  out=$(jq -nc --arg c "$1" '{tool_input:{command:$c}}' | bash "$HOOK")
  if [[ -z "$out" ]]; then
    echo "allow"
  elif grep -q '"permissionDecision":"deny"' <<<"$out"; then
    echo "deny"
  else
    echo "unexpected: $out"
  fi
}

check() {
  local want=$1 cmd=$2 got
  got=$(decide "$cmd")
  if [[ "$got" == "$want" ]]; then
    PASS=$((PASS + 1))
    printf "  \033[32m✓\033[0m %s → %s\n" "$cmd" "$got"
  else
    FAIL=$((FAIL + 1))
    printf "  \033[31m✗\033[0m %s → %s (want %s)\n" "$cmd" "$got" "$want"
  fi
}

printf "\n\033[1mvalidate-ssh.sh\033[0m\n"

# ── Allowed ──────────────────────────────────────────────────────────────
check allow 'ssh debugbot@host.example.com'
check allow 'ssh debugbot@host.example.com uptime'
check allow 'ssh -p 2222 debugbot@host.example.com uptime'
check allow "ssh debugbot@host.example.com 'sudo bay-readlog tail -n 50 /var/log/syslog'"
check allow 'scp debugbot@host.example.com:/var/log/syslog /tmp/syslog'
check allow 'ls -la'                       # unrelated command, untouched
check allow 'git status'

# ── Denied ───────────────────────────────────────────────────────────────
check deny 'ssh root@host.example.com'
check deny 'ssh root@host.example.com cat /etc/shadow # debugbot@'
check deny 'ssh -l root host.example.com'
check deny 'ssh -l root debugbot@host.example.com'
check deny "ssh debugbot@host.example.com 'cat x' && ssh root@host.example.com"
check deny 'ssh -J root@bastion debugbot@host.example.com'
check deny 'ssh -J bastion debugbot@host.example.com'
check deny 'ssh -o ProxyCommand="ssh root@bastion nc %h %p" debugbot@host.example.com'
check deny 'ssh -o ProxyJump=root@bastion debugbot@host.example.com'
check deny 'ssh -o User=root debugbot@host.example.com'
check deny 'ssh -F /tmp/evil-config debugbot@host.example.com'
check deny 'echo hi; ssh debugbot@host.example.com'
check deny 'ssh debugbot@host.example.com | tee /tmp/out'
check deny 'ssh $(echo root@host) uptime'
check deny 'scp /etc/passwd root@host.example.com:/tmp/passwd'
check deny 'rsync -av -e "ssh -l root" /tmp/ debugbot@host.example.com:/tmp/'
check deny 'sftp root@host.example.com'
check deny 'rsync -av /tmp/ host.example.com:/tmp/'

printf "\n%d passed, %d failed\n" "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
