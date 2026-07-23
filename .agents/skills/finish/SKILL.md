---
name: finish
description: Safely finish the current repository branch by tracing its unmerged parent-branch chain, merging that chain into main, validating and pushing main, deleting only the originally current branch locally and remotely, and closing or commenting on related GitHub issues. Use when the user invokes `/finish`, `$finish`, asks to finish or clean up the current branch through main, or explicitly requests the merge/push/branch-delete/issue-close workflow. Supports explicit forced issue closure.
---

# Finish

Complete one feature-branch lineage without losing commits or closing issues
prematurely. Treat an explicit invocation as authorization for the merge,
`main` push, current-branch deletion, and ordinary issue actions described
below. Do not infer authorization to force-close an issue.

## Accepted directives

- `/finish` or `$finish`: perform the standard workflow.
- `/finish --force-close 44,45`: force-close only the named related issues.
- `/finish --force-close-related`: force-close every related issue found.
- Japanese equivalents such as `#44を強制close` carry the same explicit
  authorization for the named issue.

If a force-close directive is vague about which issue it covers, ask before
changing issue state.

## Non-negotiable guards

- Require a clean worktree and index. Stop for uncommitted or untracked work;
  never stage, stash, discard, or absorb it automatically.
- Require `gh`, authenticated GitHub access, an `origin` remote, and a current
  branch other than `main`.
- Fetch `origin` with pruning before reasoning about ancestry.
- Refuse a detached HEAD, unresolved merge/rebase/cherry-pick, diverged
  current upstream, or a local `main` that is not safely fast-forwardable from
  `origin/main`.
- Inspect `git worktree list --porcelain`. Stop if switching or deleting a
  required branch would disturb another worktree.
- Never use `git reset --hard`, force-push, or `git branch -D` on a user branch.
  `-D` is allowed only for the temporary integration branch created by this
  workflow.
- Do not delete any branch or change issue state until the updated `main` push
  succeeds and `origin/main` is proven to contain the original current SHA.
- Delete only the branch that was current when `/finish` began. Keep ancestor
  branches unless the user separately requests their deletion.

## 1. Record the starting state

Record:

- repository and default branch from `gh repo view`;
- original current branch, local SHA, upstream, and remote existence;
- `origin/main` SHA;
- worktree status and worktree ownership;
- any open or closed PR whose head is a branch in the lineage.

Require the repository default branch to be `main` unless the user explicitly
supplies another target. Show the user the discovered starting branch and SHA
before mutation.

## 2. Discover a linear branch lineage

Git does not store “created from branch” metadata. Resolve the lineage using
these sources in order:

1. Follow GitHub PR `baseRefName` links from the current branch toward `main`.
2. If no usable PR chain exists, inspect local and `origin/*` branch tips.
   Select tips that are:
   - ancestors of the original current SHA;
   - descendants of `origin/main`; and
   - not already ancestors of `origin/main`.
3. Normalize duplicate local and `origin/` names that point to the same tip.
4. Order the remaining tips from oldest to newest by ancestry, then append the
   original current branch.

Every adjacent tip must form one linear ancestor chain. Each PR-derived base
must also exist and be an ancestor of the current tip. If candidates are
incomparable, duplicated under materially different names, missing, or
otherwise ambiguous, stop and ask which lineage is intended. Do not merge all
possible branches.

Report the resolved chain in this form before continuing:

```text
main -> parent-branch -> current-branch
```

## 3. Identify related GitHub work

Before deleting any branch, collect related issues from:

- an explicit issue number in a branch name such as `issue-44-*`;
- `closingIssuesReferences` and explicit issue links in lineage PRs;
- commit messages in `origin/main..CURRENT_SHA` only when they clearly say the
  commit fixes, closes, or implements that issue.

Do not treat an incidental `#N` mention as ownership of an issue. Read each
candidate issue, its current state, checklist, and recent comments. Also
collect open PRs whose head is any lineage branch so they can be commented on
and closed after direct integration.

## 4. Build and validate on a temporary integration branch

Create a uniquely named temporary branch from the freshly fetched
`origin/main`; do not merge directly into local `main` yet.

Merge each lineage branch from oldest to newest with `--no-ff`. Use Japanese
merge commit messages and the repository owner's configured author identity.
Do not add AI attribution trailers.

If any merge conflicts:

1. abort the active merge;
2. switch back to the original branch;
3. delete only the temporary branch;
4. report the exact conflicting paths and stop.

Run the repository's relevant validation on the completed temporary branch.
Read `AGENTS.md` and use the strongest practical existing test entrypoints.
At minimum, run changed-code syntax checks, the relevant unit tests, and
configuration validation. Do not weaken, skip, or rewrite a failing test to
finish the workflow.

On validation failure, switch back to the original branch, delete the
temporary branch, leave `main`, remote branches, PRs, and issues unchanged,
and report the failure.

## 5. Advance and push main

After validation succeeds:

1. switch to local `main` (create it tracking `origin/main` only if absent);
2. fast-forward it to `origin/main`;
3. fast-forward it to the validated temporary integration tip;
4. push `main` normally with no force option;
5. fetch `origin/main`;
6. require:

```sh
git merge-base --is-ancestor "$CURRENT_SHA" origin/main
```

If the push or containment proof fails, do not delete branches, close PRs, or
change issues. Keep or remove the temporary branch as needed for diagnosis,
but never rewrite `main`.

## 6. Resolve PRs and issues

Only after the remote containment proof:

- For each open lineage PR, post a concise Japanese comment saying its commits
  were integrated directly into `main`, include the resulting main SHA, then
  close the PR.
- Close a related issue when the integrated branch fully satisfies its stated
  scope, validation passed, and no unchecked requirement or explicit remaining
  task exists. Use a concise Japanese closing comment with the main commit and
  validation.
- If an issue cannot honestly be closed, leave it open and post a Japanese
  comment stating what was merged, the main commit, and the concrete remaining
  reason.
- If the user explicitly force-closed that issue, close it even when work
  remains. The Japanese closing comment must say it was force-closed by
  instruction and briefly state any known remainder.
- Do nothing to already closed issues except report their state, unless a
  comment is needed to record the new main integration.

An issue API failure does not justify pretending it was handled. Retry safe
transient failures, then report the exact uncompleted action.

## 7. Delete the original current branch

Stay on `main`. After remote-main containment is proven:

1. delete the temporary integration branch;
2. delete the original local current branch with `git branch -d`;
3. if its remote branch exists, delete it with
   `git push origin --delete BRANCH`;
4. fetch with pruning.

Never use `-D` if ordinary deletion says the original branch is not merged;
that means the containment assumptions need investigation.

## 8. Final verification

Require and report:

- current branch is `main`;
- local `main` and `origin/main` have the same SHA;
- worktree and index are clean;
- original current branch is absent locally and remotely;
- merged lineage and validation commands;
- each related issue and PR action, including anything that could not be
  completed.

Lead with the finished main SHA and the deletion result. Never claim an issue,
PR, push, or branch deletion succeeded without reading back the resulting
state.
