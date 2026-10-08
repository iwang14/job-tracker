#!/usr/bin/env bash
# Persist the SQLite state on a dedicated branch (default: "state") as a single gzip'd file.
#   scripts/state.sh restore   # CI: fetch state.db from the state branch (fresh DB if branch doesn't exist yet)
#   scripts/state.sh save      # CI: VACUUM, gzip and force-push a one-commit branch (no history bloat)
#   scripts/state.sh pull      # local: same as restore, for the referral/tailor/suggestions helpers
set -euo pipefail
BRANCH="${STATE_BRANCH:-state}"
DB="${JOBPIPE_DB:-state.db}"

restore() {
  set +e
  git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1
  rc=$?
  set -e
  if [ "$rc" -eq 2 ]; then
    echo "No '$BRANCH' branch yet: starting with a fresh database."
    return 0
  elif [ "$rc" -ne 0 ]; then
    # Never silently start fresh on a network/auth error: the save step would overwrite real state.
    echo "::error::could not query origin for the '$BRANCH' branch (git exit $rc)"; exit 1
  fi
  git fetch --quiet --depth=1 origin "$BRANCH"
  git show "FETCH_HEAD:state.db.gz" | gunzip > "$DB"
  python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'" "$DB"
  echo "Restored $DB ($(du -h "$DB" | cut -f1))."
}

save() {
  [ -f "$DB" ] || { echo "no $DB to save"; exit 1; }
  python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('VACUUM'); c.close()" "$DB"
  tmp="$(mktemp -d)"
  gzip -9 -c "$DB" > "$tmp/state.db.gz"
  cd "$tmp"
  git init -q
  git checkout -q -b "$BRANCH"
  git add state.db.gz
  git -c user.name="jobpipe-bot" -c user.email="jobpipe-bot@users.noreply.github.com" \
      commit -q -m "state $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for i in 1 2 3; do
    remote="${STATE_REMOTE:-https://x-access-token:${GITHUB_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"
    if git push -q -f "$remote" "$BRANCH"; then
      echo "Saved state ($(du -h state.db.gz | cut -f1) compressed)."; return 0
    fi
    sleep $((i * 3))
  done
  echo "::error::failed to push state"; exit 1
}

case "${1:-}" in
  restore|pull) restore ;;
  save) save ;;
  *) echo "usage: $0 restore|save|pull"; exit 2 ;;
esac
