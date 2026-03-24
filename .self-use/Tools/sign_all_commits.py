#!/usr/bin/env python3
"""
Script to re-sign all commits with GPG key using git rebase --exec
Preserves original author date and committer date
"""

import subprocess
import sys
import os
from datetime import datetime

def run_cmd(cmd, check=True):
    """Run a shell command and return output"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Error running: {cmd}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result

def main():
    # Parse arguments
    auto_yes = '--yes' in sys.argv or '-y' in sys.argv
    
    # Change to repo directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(script_dir)
    os.chdir(repo_dir)
    
    print(f"Repository: {repo_dir}")
    
    # Check GPG signing key
    result = run_cmd("git config user.signingkey", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        print("Error: No GPG signing key configured.")
        print("Please set it with: git config user.signingkey <YOUR_KEY_ID>")
        sys.exit(1)
    
    signing_key = result.stdout.strip()
    print(f"Using GPG key: {signing_key}")
    
    # Get current branch
    result = run_cmd("git branch --show-current")
    current_branch = result.stdout.strip()
    print(f"Current branch: {current_branch}")
    
    # Count commits
    result = run_cmd("git rev-list --count HEAD")
    commit_count = int(result.stdout.strip())
    print(f"Total commits to sign: {commit_count}")
    
    # Confirm with user
    if not auto_yes:
        response = input(f"\nThis will rewrite ALL {commit_count} commits with GPG signatures. Continue? (y/N) ")
        if response.lower() != 'y':
            print("Aborted.")
            sys.exit(0)
    else:
        print(f"\nAuto-confirming: will rewrite ALL {commit_count} commits with GPG signatures.")
    
    print("\nStarting re-signing process...")
    print(f"This may take a while for {commit_count} commits...\n")
    
    # Create backup branch
    backup_branch = f"backup-before-signing-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_cmd(f"git branch {backup_branch}")
    print(f"Created backup branch: {backup_branch}\n")
    
    # Set GPG_TTY
    os.environ['GPG_TTY'] = subprocess.run(['tty'], capture_output=True, text=True).stdout.strip()
    
    # Ensure GPG agent is running
    run_cmd("gpgconf --launch gpg-agent", check=False)
    
    # Create a signing script that preserves dates
    sign_script = os.path.join(repo_dir, '.git', 'sign-commit.sh')
    with open(sign_script, 'w') as f:
        f.write('''#!/bin/bash
# Get original metadata
ORIG_AUTHOR_NAME=$(git log -1 --format='%an')
ORIG_AUTHOR_EMAIL=$(git log -1 --format='%ae')
ORIG_AUTHOR_DATE=$(git log -1 --format='%ad')
ORIG_COMMITTER_DATE=$(git log -1 --format='%cd')

# Export dates for git
export GIT_AUTHOR_NAME="$ORIG_AUTHOR_NAME"
export GIT_AUTHOR_EMAIL="$ORIG_AUTHOR_EMAIL"
export GIT_AUTHOR_DATE="$ORIG_AUTHOR_DATE"
export GIT_COMMITTER_DATE="$ORIG_COMMITTER_DATE"

# Amend commit with GPG signature
git commit --amend -S --no-edit --no-verify
''')
    os.chmod(sign_script, 0o755)
    
    # Run rebase with exec to sign each commit
    print("Running git rebase to sign all commits...")
    print("(GPG may ask for your passphrase multiple times)\n")
    
    # Use rebase --exec to run the sign script for each commit
    result = subprocess.run(
        f"git rebase --exec '{sign_script}' --root",
        shell=True,
        env=os.environ
    )
    
    if result.returncode != 0:
        print("\nRebase failed! You may need to resolve conflicts or abort with:")
        print("  git rebase --abort")
        sys.exit(1)
    
    # Clean up
    os.remove(sign_script)
    
    print("\n" + "="*60)
    print("Re-signing complete!")
    print("="*60)
    print(f"\nBackup branch created at: {backup_branch}")
    print("\nTo verify signatures, run:")
    print("  git log --format='%H %G? %GK' -5")
    print("\nTo verify all commits are signed (G = good):")
    print("  git log --format='%G?' | grep -v G | wc -l")
    print("\nIMPORTANT: You need to force push to update the remote:")
    print("  git push --force-with-lease origin main")

if __name__ == "__main__":
    main()
