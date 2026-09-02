"""The Git workflow in CODE: review, stage, commit, branch, sync, and merge.

Two rules shape all of it:

* **Nothing happens that the user did not ask for.** Every endpoint here is
  driven by an explicit click. In particular ``commit`` commits exactly what is
  staged and never runs ``git add`` on the user's behalf — a commit button that
  quietly swept in the rest of the working tree would be the most surprising
  thing in the product.
* **Anything that leaves the machine is separate and named.** Fetch, pull and
  push are their own endpoints rather than side effects of anything else, and
  they report what Git said rather than summarising it.

Partial staging works the way every editor does it: build a patch containing
only the chosen hunks and hand it to ``git apply --cached``. The hunk arithmetic
lives in :mod:`daino.git.hunks`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.events import GitChanged
from daino.exceptions import WorkspaceError
from daino.git import hunks as hunk_tools
from daino.server.deps import get_state, language_for, safe_path
from daino.server.state import GuiState

router = APIRouter(prefix="/api/git", tags=["git"])

#: A blob larger than this is reported as too big rather than shipped to Monaco.
_MAX_DIFF_BYTES = 2_000_000


class PathsRequest(BaseModel):
    paths: list[str]


class HunkRequest(BaseModel):
    """Stage or unstage part of one file."""

    path: str = Field(min_length=1)
    #: Hunk indices as the matching ``/api/git/hunks`` response numbered them.
    hunks: list[int] = Field(default_factory=list)
    #: Which side the hunks were read from: the working tree, or the index.
    staged: bool = False


class CommitRequest(BaseModel):
    message: str = Field(min_length=1)
    amend: bool = False
    sign_off: bool = False


class BranchRequest(BaseModel):
    name: str = Field(min_length=1)
    #: Where a new branch starts. Empty means the current HEAD.
    start: str = ""
    create: bool = False


class SyncRequest(BaseModel):
    remote: str = ""
    branch: str = ""
    rebase: bool = False
    set_upstream: bool = False


class MergeRequest(BaseModel):
    ref: str = Field(min_length=1)
    #: Stop before committing, so the merge can be reviewed first.
    no_commit: bool = False


class ResolveRequest(BaseModel):
    path: str = Field(min_length=1)
    #: "ours" keeps this branch's version, "theirs" takes the incoming one.
    side: str = Field(pattern="^(ours|theirs)$")


def _require_repository(state: GuiState) -> None:
    if not state.git.is_repository():
        raise HTTPException(status_code=400, detail="This project is not a Git repository.")


def _fail(result: object, fallback: str) -> None:
    """Turn a failed Git result into a 400 that says what Git said."""
    succeeded = getattr(result, "succeeded", True)
    if succeeded:
        return
    stderr = str(getattr(result, "stderr", "")).strip()
    stdout = str(getattr(result, "stdout", "")).strip()
    raise HTTPException(status_code=400, detail=stderr or stdout or fallback)


def _parse_porcelain(text: str) -> dict[str, list[dict[str, str]]]:
    staged: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    untracked: list[dict[str, str]] = []
    for line in text.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status, path = line[0], line[1], line[3:]
        # Renames arrive as "old -> new"; the new path is the one to act on.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        entry = {"path": path}
        if index_status == "?" and worktree_status == "?":
            untracked.append(entry)
            continue
        if index_status not in (" ", "?"):
            staged.append({"path": path, "status": index_status})
        if worktree_status not in (" ", "?"):
            modified.append({"path": path, "status": worktree_status})
    return {"staged": staged, "modified": modified, "untracked": untracked}


def _blob(state: GuiState, revision: str, path: str) -> tuple[str, bool]:
    """Read one blob at ``revision``; returns (text, exists)."""
    result = state.git.run("show", f"{revision}:{path}", check=False)
    if not result.succeeded:
        return "", False
    return result.stdout, True


def _worktree_text(state: GuiState, path: str) -> tuple[str, bool, bool]:
    """Read the working-tree file; returns (text, exists, binary)."""
    target = safe_path(state, path)
    if not target.is_file():
        return "", False, False
    if target.stat().st_size > _MAX_DIFF_BYTES:
        return "", True, True
    try:
        return target.read_text(encoding="utf-8"), True, False
    except (UnicodeDecodeError, OSError):
        return "", True, True


@router.get("/status")
def status(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "branch": "", "staged": [], "modified": [], "untracked": []}
    parsed = _parse_porcelain(state.git.status(porcelain=True))
    return {"repository": True, "branch": state.git.current_branch(), **parsed}


@router.get("/diff")
def diff(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(default=""),
    staged: bool = Query(default=False),
) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "diff": ""}
    refs = (path,) if path else ()
    text = state.git.diff(*refs, staged=staged)
    return {"repository": True, "path": path, "staged": staged, "diff": text}


@router.get("/file")
def file_diff(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(...),
    staged: bool = Query(default=False),
) -> dict:
    """Whole-file ``original`` and ``modified`` content for one changed path.

    Staged view compares HEAD against the index; the working view compares the
    index (falling back to HEAD for a file that was never staged) against what
    is on disk. Returning full files rather than hunks is what lets the editor
    show surrounding context and let the reader scroll through the file.
    """
    if not state.git.is_repository():
        return {
            "repository": False,
            "path": path,
            "staged": staged,
            "original": "",
            "modified": "",
            "language": "plaintext",
            "binary": False,
        }

    binary = False
    if staged:
        original, _ = _blob(state, "HEAD", path)
        modified, present = _blob(state, "", path)  # ":path" — the index
        if not present:
            modified, _ = _blob(state, "HEAD", path)
    else:
        original, present = _blob(state, "", path)
        if not present:
            original, _ = _blob(state, "HEAD", path)
        modified, _, binary = _worktree_text(state, path)

    if "\x00" in original or "\x00" in modified:
        binary = True
    if binary:
        original = modified = ""

    return {
        "repository": True,
        "path": path,
        "staged": staged,
        "original": original,
        "modified": modified,
        "language": language_for(state.root / path),
        "binary": binary,
    }


@router.post("/stage")
def stage(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    try:
        state.git.run("add", "--", *body.paths)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"staged": body.paths}


@router.post("/unstage")
def unstage(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    # "restore --staged" fails on a repository with no commits; reset works in both.
    result = state.git.run("reset", "-q", "HEAD", "--", *body.paths, check=False)
    if not result.succeeded:
        raise HTTPException(status_code=400, detail=result.stderr.strip() or "Unstage failed")
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"unstaged": body.paths}


@router.post("/discard")
def discard(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    """Throw away working-tree changes for tracked paths. Untracked files are left alone."""
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    result = state.git.run("checkout", "--", *body.paths, check=False)
    if not result.succeeded:
        raise HTTPException(status_code=400, detail=result.stderr.strip() or "Discard failed")
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"discarded": body.paths}


@router.get("/log")
def log(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    if not state.git.is_repository():
        return {"repository": False, "entries": []}
    lines = [line for line in state.git.log(limit).splitlines() if line.strip()]
    return {"repository": True, "entries": lines}


# ----------------------------------------------------------------- hunks


@router.get("/hunks")
def file_hunks(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(min_length=1),
    staged: bool = Query(default=False),
) -> dict:
    """One file's change, split into individually stageable hunks.

    ``staged`` picks the side: false is index→working tree (what you could
    stage), true is HEAD→index (what you could unstage). They are different
    diffs, and a hunk index only means anything within the one it came from.
    """
    _require_repository(state)
    safe_path(state, path)
    patch = state.git.diff("--", path, staged=staged) if staged else state.git.diff("--", path)
    files = hunk_tools.split(patch)
    found = hunk_tools.find(files, path)
    if found is None:
        return {"path": path, "staged": staged, "hunks": [], "binary": False}
    return {
        "path": path,
        "staged": staged,
        "binary": found.binary,
        "hunks": hunk_tools.describe(found),
    }


@router.post("/stage-hunks")
def stage_hunks(state: Annotated[GuiState, Depends(get_state)], body: HunkRequest) -> dict:
    """Move part of a file into the index, leaving the rest unstaged."""
    _require_repository(state)
    safe_path(state, body.path)
    if not body.hunks:
        raise HTTPException(status_code=400, detail="No hunks selected.")
    patch = state.git.diff("--", body.path)
    found = hunk_tools.find(hunk_tools.split(patch), body.path)
    if found is None:
        raise HTTPException(status_code=400, detail=f"{body.path} has no unstaged change.")
    built = hunk_tools.rebuild(found, body.hunks)
    if not built:
        raise HTTPException(status_code=400, detail="No matching hunks.")
    _fail(state.git.apply_patch(built, cached=True), "Could not stage those hunks.")
    state.context.events.publish(GitChanged(paths=[body.path]))
    return {"staged": body.path, "hunks": body.hunks}


@router.post("/unstage-hunks")
def unstage_hunks(state: Annotated[GuiState, Depends(get_state)], body: HunkRequest) -> dict:
    """Take part of a file back out of the index.

    The patch is the *staged* diff applied in reverse, which is what makes this
    the exact inverse of staging rather than an approximation of it.
    """
    _require_repository(state)
    safe_path(state, body.path)
    if not body.hunks:
        raise HTTPException(status_code=400, detail="No hunks selected.")
    patch = state.git.diff("--", body.path, staged=True)
    found = hunk_tools.find(hunk_tools.split(patch), body.path)
    if found is None:
        raise HTTPException(status_code=400, detail=f"{body.path} has nothing staged.")
    built = hunk_tools.rebuild(found, body.hunks)
    if not built:
        raise HTTPException(status_code=400, detail="No matching hunks.")
    _fail(
        state.git.apply_patch(built, cached=True, reverse=True),
        "Could not unstage those hunks.",
    )
    state.context.events.publish(GitChanged(paths=[body.path]))
    return {"unstaged": body.path, "hunks": body.hunks}


# ---------------------------------------------------------------- committing


@router.get("/commit-context")
def commit_context(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """What a commit right now would contain, and what it would amend.

    The staged-file list is here so the commit box can say "3 files" without a
    second request, and the previous message is here so Amend can prefill it —
    an amend that made you retype the message is an amend nobody uses.
    """
    if not state.git.is_repository():
        return {"repository": False}
    staged = _parse_porcelain(state.git.status(porcelain=True))["staged"]
    has_head = state.git.run("rev-parse", "--verify", "HEAD", check=False).succeeded
    return {
        "repository": True,
        "branch": state.git.current_branch(),
        "staged": staged,
        "can_amend": has_head,
        "previous_message": state.git.commit_message_of() if has_head else "",
        **state.git.merge_state(),
    }


@router.post("/commit")
def commit(state: Annotated[GuiState, Depends(get_state)], body: CommitRequest) -> dict:
    """Commit exactly what is staged. Never stages anything itself."""
    _require_repository(state)
    state_now = state.git.merge_state()
    staged = _parse_porcelain(state.git.status(porcelain=True))["staged"]
    if not staged and not body.amend and not state_now["merging"]:
        raise HTTPException(
            status_code=400,
            detail="Nothing is staged. Stage the changes you want in this commit first.",
        )
    if state_now["conflicts"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "This merge still has conflicts: "
                + ", ".join(str(item) for item in state_now["conflicts"][:5])
                + ". Resolve them before committing."
            ),
        )
    result = state.git.commit_staged(
        body.message,
        amend=body.amend,
        sign_off=body.sign_off,
        # A merge commit is legitimately empty of its own changes.
        allow_empty=bool(state_now["merging"]),
    )
    _fail(result, "The commit failed.")
    state.context.events.publish(GitChanged(paths=[item["path"] for item in staged]))
    return {"committed": True, "revision": state.git.revision(), "output": result.stdout}


# ------------------------------------------------------------------ branches


@router.get("/branches")
def branches(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Local branches with their tracking state, plus remotes to check out."""
    if not state.git.is_repository():
        return {"repository": False, "branches": [], "remote_branches": [], "remotes": []}
    return {
        "repository": True,
        "current": state.git.current_branch(),
        "branches": state.git.branches(),
        "remote_branches": state.git.remote_branches(),
        "remotes": state.git.remotes(),
    }


@router.post("/branch")
def switch_branch(state: Annotated[GuiState, Depends(get_state)], body: BranchRequest) -> dict:
    """Switch to a branch, or create one and switch to it."""
    _require_repository(state)
    result = state.git.checkout(body.name, create=body.create, start=body.start)
    _fail(result, f"Could not switch to {body.name}.")
    state.context.events.publish(GitChanged(paths=[]))
    return {
        "branch": state.git.current_branch(),
        "created": body.create,
        "output": (result.stdout + result.stderr).strip(),
    }


@router.delete("/branch")
def delete_branch(
    state: Annotated[GuiState, Depends(get_state)],
    name: str = Query(min_length=1),
    force: bool = Query(default=False),
) -> dict:
    """Delete a local branch.

    Without ``force`` Git refuses to delete a branch holding unmerged commits,
    and that refusal is passed straight through rather than being worked around:
    it is the only thing standing between the user and losing work.
    """
    _require_repository(state)
    result = state.git.delete_branch(name, force=force)
    _fail(result, f"Could not delete {name}.")
    return {"deleted": name, "output": (result.stdout + result.stderr).strip()}


# ------------------------------------------------------------------- remotes


@router.post("/fetch")
def fetch(state: Annotated[GuiState, Depends(get_state)], body: SyncRequest | None = None) -> dict:
    """Update remote-tracking refs. Changes nothing in the working tree."""
    _require_repository(state)
    request = body or SyncRequest()
    result = state.git.fetch(request.remote)
    _fail(result, "Fetch failed.")
    return {"output": (result.stdout + result.stderr).strip(), "branches": state.git.branches()}


@router.post("/pull")
def pull(state: Annotated[GuiState, Depends(get_state)], body: SyncRequest | None = None) -> dict:
    """Bring the current branch up to date with its upstream."""
    _require_repository(state)
    request = body or SyncRequest()
    result = state.git.pull(rebase=request.rebase)
    output = (result.stdout + result.stderr).strip()
    if not result.succeeded:
        # A pull that conflicts is not a failed request — it is a state the user
        # now has to resolve, and saying "error" would hide that.
        merge_state = state.git.merge_state()
        if merge_state["conflicts"]:
            return {"output": output, "conflicted": True, **merge_state}
        raise HTTPException(status_code=400, detail=output or "Pull failed.")
    state.context.events.publish(GitChanged(paths=[]))
    return {"output": output, "conflicted": False, **state.git.merge_state()}


@router.post("/push")
def push(state: Annotated[GuiState, Depends(get_state)], body: SyncRequest | None = None) -> dict:
    """Publish the current branch. The one action here that leaves the machine."""
    _require_repository(state)
    request = body or SyncRequest()
    result = state.git.push(
        remote=request.remote, branch=request.branch, set_upstream=request.set_upstream
    )
    output = (result.stdout + result.stderr).strip()
    if not result.succeeded:
        detail = output or "Push failed."
        if "has no upstream branch" in output:
            detail += "\n\nThis branch has never been pushed. Use 'Publish branch'."
        raise HTTPException(status_code=400, detail=detail)
    state.audit.emit("GitPushed", branch=state.git.current_branch())
    return {"output": output, "branches": state.git.branches()}


# --------------------------------------------------------------------- merge


@router.post("/merge")
def merge(state: Annotated[GuiState, Depends(get_state)], body: MergeRequest) -> dict:
    """Merge another ref into the current branch."""
    _require_repository(state)
    result = state.git.merge(body.ref, no_commit=body.no_commit)
    output = (result.stdout + result.stderr).strip()
    merge_state = state.git.merge_state()
    state.context.events.publish(GitChanged(paths=[]))
    if not result.succeeded and not merge_state["conflicts"]:
        raise HTTPException(status_code=400, detail=output or f"Could not merge {body.ref}.")
    return {"output": output, "conflicted": bool(merge_state["conflicts"]), **merge_state}


@router.post("/merge/abort")
def abort_merge(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    _require_repository(state)
    result = state.git.merge_abort()
    _fail(result, "There is no merge to abort.")
    state.context.events.publish(GitChanged(paths=[]))
    return {"aborted": True, **state.git.merge_state()}


@router.get("/conflicts")
def conflicts(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """What is unresolved, and whether a merge is in progress at all.

    A merge whose conflicts are all resolved is still an unfinished merge, which
    is why the state is read from the git directory rather than inferred from
    the conflict list being empty.
    """
    if not state.git.is_repository():
        return {"repository": False, "merging": False, "conflicts": []}
    return {"repository": True, **state.git.merge_state()}


@router.get("/conflict")
def conflict_sides(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(min_length=1),
) -> dict:
    """All three sides of one conflict, for a three-way view.

    Returned rather than written anywhere: showing the alternatives must not
    disturb the file the user is in the middle of editing.
    """
    _require_repository(state)
    safe_path(state, path)
    if path not in state.git.conflicts():
        raise HTTPException(status_code=404, detail=f"{path} is not conflicted.")
    return {
        "path": path,
        "base": state.git.conflict_stage(path, 1),
        "ours": state.git.conflict_stage(path, 2),
        "theirs": state.git.conflict_stage(path, 3),
        "merged": _worktree_text(state, path)[0],
        "language": language_for(state.root / path),
    }


@router.post("/conflict/resolve")
def resolve_conflict(
    state: Annotated[GuiState, Depends(get_state)], body: ResolveRequest
) -> dict:
    """Take one whole side of a conflict and mark the file resolved."""
    _require_repository(state)
    safe_path(state, body.path)
    result = state.git.resolve_with(body.path, body.side)
    _fail(result, f"Could not resolve {body.path}.")
    state.context.events.publish(GitChanged(paths=[body.path]))
    return {"resolved": body.path, "side": body.side, **state.git.merge_state()}


@router.post("/conflict/mark-resolved")
def mark_resolved(state: Annotated[GuiState, Depends(get_state)], body: PathsRequest) -> dict:
    """Accept the file as the user has edited it, markers and all removed.

    Deliberately does not check for leftover conflict markers: a file can
    legitimately contain the string "<<<<<<<", and refusing to stage it would be
    a rule that fires on the wrong thing. The Problems panel and the diff are
    where a leftover marker gets caught.
    """
    _require_repository(state)
    if not body.paths:
        raise HTTPException(status_code=400, detail="No paths given")
    for path in body.paths:
        safe_path(state, path)
    result = state.git.run("add", "--", *body.paths, check=False)
    _fail(result, "Could not mark those files resolved.")
    state.context.events.publish(GitChanged(paths=list(body.paths)))
    return {"resolved": body.paths, **state.git.merge_state()}
