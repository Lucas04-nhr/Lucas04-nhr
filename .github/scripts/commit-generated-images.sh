#!/usr/bin/env bash
# Commit generated images while keeping a single image commit at the tip of main.
# If HEAD is already the bot's image commit, amend it and force-push.
# Otherwise create a new commit and push normally.
# Writes result=unchanged|updated|push_failed to $GITHUB_OUTPUT.
# Usage: commit-generated-images.sh <path>...
set -euo pipefail

BOT_NAME="github-actions[bot]"
BOT_EMAIL="github-actions[bot]@users.noreply.github.com"
MESSAGE="chore: Update generated images"
out="${GITHUB_OUTPUT:-/dev/null}"

git config user.name "$BOT_NAME"
git config user.email "$BOT_EMAIL"

git add -- "$@"
if git diff --cached --quiet; then
  echo "result=unchanged" >> "$out"
  exit 0
fi

old_head=$(git rev-parse HEAD)
if [ "$(git log -1 --format=%an)" = "$BOT_NAME" ] && [ "$(git log -1 --format=%s)" = "$MESSAGE" ]; then
  # Fold this update into the previous image commit so its old blobs drop out of history
  git commit --quiet --amend --reset-author -m "$MESSAGE"
  push=(git push --force-with-lease="main:$old_head" origin HEAD:main)
else
  git commit --quiet -m "$MESSAGE"
  push=(git push origin HEAD:main)
fi

# A failed push (e.g. main moved during the run) stays non-fatal and is reported
if "${push[@]}"; then
  echo "result=updated" >> "$out"
else
  echo "result=push_failed" >> "$out"
  echo "::warning title=Image push failed::The generated images were not pushed."
fi
