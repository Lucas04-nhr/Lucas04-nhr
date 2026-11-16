#!/usr/bin/env python3
"""Keep exactly one generated-images commit in main, at the tip.

CI (see .github/scripts/commit-generated-images.sh) amends the image commit
only while it sits at the tip of main. A local commit on top of it buries it,
and the next CI run then adds a second image commit. This tool rewrites the
local main so the newest image commit is the only one and sits at the tip. It
drops every older image commit and re-signs each commit whose signature the
rewrite invalidates, using the configured GPG key
(git fast-import --signed-commits=sign-if-invalid, Git >= 2.52). Commits that
were unsigned stay unsigned.

Usage (run from anywhere inside the repository):
  squash_generated_images.py            rewrite local main (the post-commit hook runs this)
  squash_generated_images.py sync       fetch origin, rebase local commits onto origin/main
                                        while dropping local image commits, then rewrite
  squash_generated_images.py sync --push   ...and force-push with a lease on origin/main
  squash_generated_images.py --check    report what would change, rewrite nothing

After a rewrite, the remote needs `git push --force-with-lease`. A plain push
is refused because the image commit was already pushed.
"""
import os, re, subprocess, sys

BRANCH = "main"
REMOTE = "origin"
BOT = "github-actions[bot]"
MESSAGE = "chore: Update generated images"
SUBJECT_RX = re.compile(
    r"^(chore: Update generated images"
    r"|chore: Update snake image \d{8}"
    r"|Update github-metrics\.svg - \[Skip GitHub Action\])$")
PATHS = ["static/github-snake.svg", "static/github-snake-dark.svg",
         "static/ocean.gif", "github-metrics.svg"]
TMP_REF = "refs/squash-generated-images/tmp"
GUARD = "SQUASH_GENERATED_IMAGES_RUNNING"


def git(*args, input=None, check=True, text=True):
    r = subprocess.run(["git", *args], input=input, capture_output=True, text=text)
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        sys.exit(f"git {' '.join(args)} failed:\n{err}")
    return r.stdout


def say(msg):
    print(f"[squash-images] {msg}", file=sys.stderr)


def image_commits(rev):
    """Return (all commits oldest-first, set of image-commit oids) for rev."""
    log = git("log", "--reverse", "--format=%H%x00%P%x00%an%x00%s", rev)
    commits, images = [], set()
    for line in log.splitlines():
        oid, parents, an, subj = line.split("\0")
        if len(parents.split()) > 1:
            sys.exit(f"merge commit {oid[:7]} found; only linear history is supported")
        commits.append(oid)
        if an == BOT and SUBJECT_RX.match(subj):
            images.add(oid)
    return commits, images


def needs_rewrite(commits, images):
    return bool(images) and not (len(images) == 1 and commits[-1] in images)


def check_paths(commits, images):
    """Refuse if a non-image commit after the first image commit touched an
    image path, or an image commit touched anything else. Either case would
    make dropping or moving the image commits change real content."""
    first = next(i for i, c in enumerate(commits) if c in images)
    for c in commits[first:]:
        files = git("diff-tree", "--no-commit-id", "--name-only", "-r", c).split()
        if c in images:
            other = [f for f in files if f not in PATHS]
            if other:
                sys.exit(f"image commit {c[:7]} also touches {other}; aborting")
        elif any(f in PATHS for f in files):
            sys.exit(f"commit {c[:7]} touches generated image paths; aborting")


def build_stream(stream, images, latest, me):
    """Filter a fast-export stream: drop all image commits and emit `latest`
    last, carrying full copies of every image path, on TMP_REF."""
    extra = []
    for p in PATHS:
        entry = git("ls-tree", latest, "--", p).strip()
        if entry:
            mode, _, sha = entry.split("\t")[0].split()
            extra.append(f"M {mode} {sha} {p}\n".encode())
    ref_from = f"refs/heads/{BRANCH}".encode()
    data, pos, out = stream, 0, []
    mark_map, last_mark, held = {}, None, None

    def readline():
        nonlocal pos
        nl = data.index(b"\n", pos)
        line, pos = data[pos:nl + 1], nl + 1
        return line

    def read_data(header):
        nonlocal pos
        n = int(header.split()[1])
        body, pos = data[pos:pos + n], pos + n
        return header + body

    def emit(blk, parent):
        out.append(b"commit " + TMP_REF.encode() + b"\n")
        out.extend(blk["fields"])
        out.append(blk["msg"])
        if parent is not None:
            out.append(b"from " + parent + b"\n")
        out.extend(blk["changes"])
        out.append(b"\n")

    while pos < len(data):
        line = readline()
        if line.startswith(b"reset "):
            out.append(line.replace(ref_from, TMP_REF.encode()))
            continue
        if not line.startswith(b"commit "):
            out.append(line)
            continue
        blk = {"fields": [], "msg": None, "from": None, "changes": []}
        mark = oid = None
        signed = False
        while True:
            l = readline()
            if l.startswith(b"mark "):
                mark = l.split()[1]; blk["fields"].append(l)
            elif l.startswith(b"original-oid "):
                oid = l.split()[1].decode(); blk["fields"].append(l)
            elif l.startswith(b"gpgsig "):
                signed = True; blk["fields"].append(l + read_data(readline()))
            elif l.startswith(b"data "):
                blk["msg"] = read_data(l); break
            else:
                blk["fields"].append(l)
        while pos < len(data):
            save = pos
            l = readline()
            if l.startswith(b"from "):
                blk["from"] = l
            elif l == b"\n":
                break
            elif l[:2] in (b"M ", b"D ", b"C ", b"R ") or l.startswith(b"deleteall"):
                blk["changes"].append(l)
            else:
                pos = save; break
        if blk["from"]:
            ref = blk["from"].split()[1]
            parent = mark_map.get(ref, ref)
        else:
            parent = last_mark
        if oid in images:
            mark_map[mark] = parent
            if oid == latest:
                blk["changes"] = extra
                msg = MESSAGE.encode() + b"\n"
                blk["msg"] = b"data %d\n" % len(msg) + msg
                held = blk
            continue
        if signed:
            # Commits signed by GitHub's web-flow key get re-signed with the local
            # key, so the committer becomes the local user to keep them Verified on GitHub
            blk["fields"] = [
                (f"committer {me}".encode() + f[f.index(b">") + 1:])
                if f.startswith(b"committer GitHub <noreply@github.com>") else f
                for f in blk["fields"]]
        emit(blk, parent)
        last_mark = mark
    emit(held, last_mark)
    return b"".join(out)


def rewrite(check_only=False):
    head = git("rev-parse", f"refs/heads/{BRANCH}").strip()
    commits, images = image_commits(head)
    if not needs_rewrite(commits, images):
        say("nothing to do: main has at most one image commit, at the tip")
        return False
    latest = [c for c in commits if c in images][-1]
    check_paths(commits, images)
    say(f"{len(images)} image commit(s); keeping {latest[:7]} and moving it to the tip")
    if check_only:
        return True
    me = f"{git('config', 'user.name').strip()} <{git('config', 'user.email').strip()}>"
    stream = git("fast-export", "--no-data", "--signed-commits=verbatim",
                 "--show-original-ids", head, text=False)
    filtered = build_stream(stream, images, latest, me)
    git("update-ref", "-d", TMP_REF, check=False)
    git("fast-import", "--quiet", "--signed-commits=sign-if-invalid",
        input=filtered, text=False)
    new = git("rev-parse", TMP_REF).strip()
    git("update-ref", "-d", TMP_REF)
    if git("rev-parse", f"{new}^{{tree}}") != git("rev-parse", f"{head}^{{tree}}"):
        sys.exit("rewritten tree differs from the original; main left unchanged")
    # The tree is identical, so the index and working tree stay valid
    git("update-ref", "-m", "squash-generated-images", f"refs/heads/{BRANCH}", new, head)
    unsigned = [l for l in git("log", "--format=%G? %h %s", new).splitlines()
                if l[0] not in "GN"]
    say(f"main: {head[:7]} -> {new[:7]}; push with: git push --force-with-lease {REMOTE} {BRANCH}")
    if unsigned:
        say("WARNING: commits without a good signature:\n  " + "\n  ".join(unsigned))
    return True


def todo_editor(path):
    """GIT_SEQUENCE_EDITOR for sync: drop every image commit from the todo list."""
    lines = open(path).read().splitlines(keepends=True)
    out = []
    for l in lines:
        parts = l.split()
        if len(parts) >= 2 and parts[0] == "pick":
            an, subj = git("log", "-1", "--format=%an%x00%s", parts[1]).rstrip("\n").split("\0")
            if an == BOT and SUBJECT_RX.match(subj):
                l = "drop" + l[4:]
        out.append(l)
    open(path, "w").writelines(out)


def sync(push):
    if git("symbolic-ref", "--short", "HEAD", check=False).strip() != BRANCH:
        sys.exit(f"sync must run with {BRANCH} checked out")
    git("fetch", "--quiet", REMOTE, BRANCH)
    upstream = git("rev-parse", f"{REMOTE}/{BRANCH}").strip()
    head = git("rev-parse", "HEAD").strip()
    if head != upstream:
        if subprocess.run(["git", "merge-base", "--is-ancestor", head, upstream]).returncode == 0:
            r = subprocess.run(["git", "merge", "--ff-only", "--quiet", upstream])
        else:
            editor = f"{sys.executable} -I {os.path.abspath(__file__)} _todo"
            r = subprocess.run(["git", "-c", f"sequence.editor={editor}",
                                "rebase", "-i", "--autostash", upstream])
        if r.returncode != 0:
            sys.exit("rebase/merge stopped; resolve it, then run sync again")
    rewrite()
    if push and git("rev-parse", "HEAD").strip() != upstream:
        r = subprocess.run(["git", "push", f"--force-with-lease={BRANCH}:{upstream}",
                            REMOTE, f"{BRANCH}:{BRANCH}"])
        sys.exit(r.returncode)


def main():
    args = sys.argv[1:]
    if args[:1] == ["_todo"]:
        return todo_editor(args[1])
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    os.environ[GUARD] = "1"
    if args[:1] == ["sync"]:
        return sync("--push" in args)
    if args and args != ["--check"]:
        sys.exit(__doc__)
    rewrite(check_only=bool(args))


if __name__ == "__main__":
    main()
