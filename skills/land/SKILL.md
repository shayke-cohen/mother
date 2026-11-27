---
name: land
description: Use when this session opened pull requests that must reach the default branch — "land these", "babysit the PRs until merged", "merge all that is ready", "ship these" — or when the manager asks "are we done?", "is it all merged?", "what's the status of the PRs?", "are we progressing?". Works in any repo.
---

# Land

A pull request this session opened is this session's job until it is **merged or closed**. "CI is running",
"waiting on review" and "blocked on approval" are states to work through, not reasons to hand back. The
manager should never have to ask "are we progressing?" or "merge it" twice.

Two modes: **land** (drive the PRs to merged, reporting as you go) and **status** (the "are we done?" report).
Both answer for the track (every worktree and branch this work touched), not only the current checkout.

## Before you start: the repo's own rules win

Read the repo's `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md` and PR template. If they define how PRs merge
(merge queue, required reviewers, merge method, auto-merge labels or comment directives, release trains), follow
them. If the repo or its plugins ship their own PR-babysitting skill or command, use that one instead of this
mode and keep only the heartbeat and the status report from here.

## Land mode

Make the list first: the track is the PRs this session (or the track's other sessions, when the manager named
them) opened, and the branches of the worktrees it used. `gh pr list --author @me` also returns other work under
the same account; keep only PRs whose head branch is one of the track's. A track branch with commits and no PR
gets pushed and a PR opened. Then, per PR, check `mergeStateStatus` and `reviewDecision` (`gh pr view <n> --json
isDraft,mergeStateStatus,reviewDecision`) and loop:

| `mergeStateStatus` / state | It means | Do |
|---|---|---|
| `DIRTY` | Conflict with the base. **No CI runs at all** on a dirty PR | Rebase onto the remote base (or merge it in, if the repo forbids force-push), resolve, run the affected tests, push with `--force-with-lease` |
| `BEHIND` | Base moved; branch protection wants it current | Rebase or update the branch, push |
| `UNSTABLE` / a check red | A check failed | Read the log (`gh run view <id> --log-failed`), fix the cause, push. Retry once only when the log shows a flake |
| `BLOCKED` | Missing review, unresolved thread, `CHANGES_REQUESTED`, or a required check pending | Answer each review thread in code or with a reply, resolve the ones you fixed, re-request review from whoever asked for changes; request a reviewer from `CODEOWNERS` if none is set |
| `CLEAN` | Mergeable | Merge, if the approval below covers it |
| Draft | Not ready by intent | Never merge. Mark ready only when the manager asked for it or the repo's flow says so |

Read `mergeStateStatus` before you wait on anything. Forty minutes of "no checks yet" is usually `DIRTY`.

**Waiting without polling.** After every push, re-arm one watch per PR in the background:
`gh pr checks <n> --watch --fail-fast` (or a Monitor on it), so the session wakes when checks finish or fail.
Never `sleep N; gh …` loops in the foreground. While a watch runs, do the work that isn't blocked: the next PR,
a local review of the diff, the tests for what you changed.

**Stop only where a person must act**, and then say exactly who and what: "#42 needs a review from a code
owner of `api/`; I requested one at 14:10". If you can unblock it yourself (rebase, fix, reply), do that instead.

## Merging: the approval boundary

Merge only inside an approval already given:

- the manager said so in this conversation ("merge it", "merge all that is ready", "ship these and merge"), or
- standing instructions in the repo or workspace say this kind of PR is merged by the agent.

"Ship these", "land these" or "babysit the PRs" mean drive them to mergeable; they are not a yes to merge.
Otherwise ask **once**, with a recommendation: "#41 is green, approved and current. Merge it now?" Then keep
working the other PRs while you wait, and carry the open question at the end of each heartbeat until it is
answered; don't re-ask it as a new message. A repo convention ("the team merges its own PRs") is not the manager's
yes. If an approval guard prompts on the merge, the manager's answer to that prompt is the decision.

Even inside an approval, never merge a draft ("merge all that is ready" does not cover drafts: list them and ask), a PR with a required check red or pending, or one with an
unresolved blocking review ("changes requested"). Use the merge method the repo requires.

Auto-merge, merge-queue labels and some comment directives land a PR seconds after approval or a green run, so
you cannot catch it by watching. **Draft is the only reliable brake.** Keep a PR draft until it should land.

After a merge, confirm it on the remote: `gh pr view <n> --json state,mergeCommit` says `MERGED`, and
`git fetch` then `git merge-base --is-ancestor <mergeCommit> origin/<default>` succeeds.

## Heartbeat

While watches or sub-agents run, post one line to the session every 30-60 minutes, woken by a timed wake-up
(the watch's own timeout, a Monitor with a deadline, or a one-shot scheduled prompt):

```
Landed #41 · #42 rebased, CI 12/20 · #43 waiting on code-owner review (requested 14:10) · ETA ~15:30
```

What landed, what each open PR waits on, an ETA when known. A change is a PR changing state: landed, a check
failed, a new blocker, a review arrived, something now needs the manager. Check counts creeping up are not a
change. **If nothing changed since the last heartbeat, say nothing.** Report those changes as they happen, not
only on the timer.

In a MOTHER workspace (`"${CLAUDE_PLUGIN_ROOT}/bin/mother" path` succeeds and `config.json` sets
`manager.chat_dm`), send the heartbeat to the manager's DM as well, only when the manager asked for it there.

## Status mode: "are we done?"

`git fetch` first, and judge every claim against the remote default branch, never the local checkout.

Gather: `gh pr list --author @me --state all --json number,title,state,mergedAt,mergeStateStatus,updatedAt`
for the track's window; `gh issue list --author @me --state open`; `git worktree list`, then per worktree
`git -C <path> status --porcelain`, `git -C <path> log --oneline @{upstream}..HEAD` (or `origin/<default>..HEAD`
when there is no upstream) and `du -sh <path>`.

Write exactly these sections, in this order. A section with nothing in it says "none"; never drop a heading.

1. **Not landed**: each open PR and its one blocker (a named check, a review thread, a missing reviewer).
2. **Stranded**: uncommitted changes, unpushed commits, branches with commits and no PR. This is where work is
   lost; name the worktree and what is in it.
3. **Landed**: merged PRs, number and title, verified on the remote.
4. **Filed, not fixed**: findings that became issues instead of code.
5. **Disk**: worktrees still on disk, with size.

Then say which it is:

- 1 and 2 empty: **done**. Say so plainly, and offer to remove the listed worktrees (don't remove them unasked).
- 2 not empty: **not done, and not safe to clean**. Lead with what would be lost.
- 1 not empty, 2 empty: not done; name what each open PR waits on, and which worktrees can go now.

The report comes first. If land mode is running (or the manager asked to land these), go back to it after
the report: push the stranded branches, fix what is red.

## Done

Done means every PR from this track is `MERGED` or `CLOSED`, verified on the remote, and nothing is stranded.
Until then the session keeps landing. Never answer "done" from the current worktree alone.

## Common mistakes

| Mistake | Instead |
|---|---|
| "Blocked on approval, nothing to do but wait", then handing back | Request the reviewer, ask the one merge question, keep the watch armed, heartbeat |
| Waiting on a PR that shows no checks | Read `mergeStateStatus`; `DIRTY` runs nothing until rebased |
| A push without re-arming the watch | Every push starts a new run; re-arm |
| Opening the report with what merged | Lead with Not landed and Stranded; a merged-first list reads as done |
| "It's on main" from the local checkout | Verify with `merge-base` against `origin/<default>` |
| Merging because the PR looks ready | Only inside the manager's approval or standing instructions |
