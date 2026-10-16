#!/bin/sh
# Keeps the history of only the newest KEEP headless career-ops runs (for looking into errors) and deletes older runs',
# so they don't fill the disk or slow down 'claude -p /usage', which scans Claude's transcripts.
# Run it from cron, e.g. every 15 minutes:  */15 * * * * $HOME/automatic_mailer/prune-run-history.sh
KEEP=${KEEP:-50}

# Claude Code: one <session>.jsonl transcript per run, plus a <session>/ folder of large tool outputs. Its folder is named
# after the career-ops path with '/' and '.' replaced by '-'. The 'memory' folder there is left alone
CAREER_OPS=$(cd "$(dirname "$0")/../career-ops" && pwd)
CLAUDE_RUNS="$HOME/.claude/projects/$(printf %s "$CAREER_OPS" | tr '/.' '--')"
if cd "$CLAUDE_RUNS" 2>/dev/null; then
    ls -t -- *.jsonl 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -rf -- "$f" "${f%.jsonl}"; done
fi

# Antigravity: one conversations/<id>.db and brain/<id>/ per run, and one log/cli-*.log per run. Its settings, login and
# other files are left alone
if cd "$HOME/.gemini/antigravity-cli" 2>/dev/null; then
    ls -t conversations 2>/dev/null | grep '\.db$' | tail -n +$((KEEP + 1)) | while read -r f; do rm -rf -- "conversations/$f" "brain/${f%.db}"; done
    ls -t log/cli-*.log 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f -- "$f"; done
fi
