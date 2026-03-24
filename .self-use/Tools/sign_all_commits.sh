#!/bin/bash
# Script to re-sign all commits with GPG key
# Uses git rebase --exec to amend each commit with GPG signature
# Preserves original author date and committer date

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "Repository: $REPO_DIR"
echo "Current branch: $(git branch --show-current)"
echo ""

# Check if GPG signing key is configured
SIGNING_KEY=$(git config user.signingkey)
if [ -z "$SIGNING_KEY" ]; then
  echo "Error: No GPG signing key configured."
  echo "Please set it with: git config user.signingkey <YOUR_KEY_ID>"
  exit 1
fi

echo "Using GPG key: $SIGNING_KEY"
echo ""

# Count commits
COMMIT_COUNT=$(git rev-list --count HEAD)
echo "Total commits to sign: $COMMIT_COUNT"
echo ""

# Confirm with user
read -p "This will rewrite ALL $COMMIT_COUNT commits with GPG signatures. Continue? (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

echo ""
echo "Starting re-signing process..."
echo "This may take a while for $COMMIT_COUNT commits..."
echo ""

# Create backup branch first
BACKUP_BRANCH="backup-before-signing-$(date +%Y%m%d-%H%M%S)"
git branch "$BACKUP_BRANCH"
echo "Created backup branch: $BACKUP_BRANCH"
echo ""

# Export GPG_TTY for GPG to work in terminal
export GPG_TTY=$(tty)

# Ensure GPG agent is running
gpgconf --launch gpg-agent 2>/dev/null || true

# Create a helper script to amend the commit
# We use a temporary file to ensure we handle dates and authors correctly
SIGN_SCRIPT=$(mktemp)
cat > "$SIGN_SCRIPT" << 'EOF'
#!/bin/bash
# Preserve original author date and author identity
ORIG_AUTHOR_DATE=$(git log -1 --format='%ad')
ORIG_AUTHOR=$(git log -1 --format='%an <%ae>')
ORIG_COMMITTER_DATE=$(git log -1 --format='%cd')

# Amend commit with signature, preserving original metadata
# GIT_COMMITTER_DATE is exported to override committer date
export GIT_COMMITTER_DATE="$ORIG_COMMITTER_DATE"

git commit --amend -S --no-edit --date="$ORIG_AUTHOR_DATE" --author="$ORIG_AUTHOR"
EOF
chmod +x "$SIGN_SCRIPT"

# Run rebase to edit every commit
# We use 'exec' to run the signing script for each commit
# --exec adds the command after every commit in the rebase todo list
# We need to do this interactively but automated. 
# However, git rebase --exec creates a new todo list that runs the command.
# But --exec doesn't persist the edit state, it just runs the command.
# To truly amend, we need to be in edit mode.
# So we use GIT_SEQUENCE_EDITOR to replace 'pick' with 'exec' first? 
# No, if we replace with 'exec', we can't amend.
# If we replace with 'edit', we have to resume manually.
# The best way is to use GIT_SEQUENCE_EDITOR to insert 'exec <script>' after every line.

# Create the editor script that inserts the exec command
EDITOR_SCRIPT=$(mktemp)
cat > "$EDITOR_SCRIPT" << EDITOR_EOF
#!/bin/bash
# Insert 'exec <script>' after every pick line
sed -e 's/^pick .*/&\\nexec "$SIGN_SCRIPT"/' "\$1" > "\$1.tmp"
mv "\$1.tmp" "\$1"
EDITOR_EOF
chmod +x "$EDITOR_SCRIPT"

# NOTE: macOS sed requires a backup extension for -i, hence using a temp file
# Also, we need to escape the path to SIGN_SCRIPT in the editor script or just rely on env
# Let's rewrite the editor script using python for portability, or just simpler sed logic.

# Re-create editor script compatible with macOS
cat > "$EDITOR_SCRIPT" << EDITOR_EOF
#!/bin/bash
sed 's/^pick /exec "$SIGN_SCRIPT"\\npick /' "\$1" > "\$1.tmp"
mv "\$1.tmp" "\$1"
EDITOR_EOF

# Run the rebase
GIT_SEQUENCE_EDITOR="$EDITOR_SCRIPT" git rebase -i --root

# Clean up
rm -f "$SIGN_SCRIPT" "$EDITOR_SCRIPT"

echo ""
echo "Re-signing complete!"
echo ""
echo "Backup branch created at: $BACKUP_BRANCH"
echo ""
echo "To verify signatures, run:"
echo "  git log --format='%H %G? %GK' -5"
echo ""
echo "IMPORTANT: You need to force push to update the remote:"
echo "  git push --force-with-lease origin main"
