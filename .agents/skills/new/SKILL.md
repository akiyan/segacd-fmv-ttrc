---
name: new
description: Start a repository task on a fresh branch and complete it through small, verified commits. Use when the user invokes `$new`, asks to cut a branch before changing files, or explicitly wants work committed incrementally while it is being done.
---

# Start a New Task

Create the task branch before editing. Keep the user's unrelated work intact,
commit each coherent part after validation, and leave the branch ready for
review.

## Workflow

1. Read the repository instructions that apply to the working directory.
2. Inspect `git status --short --branch`, the current branch, and the recent
   history. Identify the requested change and a suitable validation command.
3. Create a branch from the current `HEAD` before editing:
   - Follow the repository's branch naming policy.
   - Otherwise use `codex/<short-task-slug>`.
   - If that name exists, choose a clear numbered suffix.
   - Do not change the base branch unless the user requests it.
4. Preserve pre-existing changes:
   - Never reset, discard, overwrite, or silently include unrelated work.
   - If task files already contain user changes, separate the new edits when
     safe. Ask before proceeding only when the changes cannot be separated.
5. Work in coherent slices. For each slice:
   - Make the smallest complete change that is worth recording.
   - Run focused validation appropriate to that change.
   - Review `git diff` and `git status --short`.
   - Stage only explicit task paths. Do not use broad staging that could pick
     up unrelated files.
   - Review the staged diff, then commit it.
6. Follow repository rules for commit language and attribution. Do not add AI
   co-author or session trailers, and do not change Git identity.
7. After the last slice, run the strongest practical validation for the whole
   task. Commit any resulting task-owned fixes.
8. Report the branch name, commits, validation results, and any remaining
   uncommitted files. Do not push or open a pull request unless requested.

## Commit Boundaries

Prefer a separate commit when a change can be understood or reverted on its
own, such as:

- adding or updating the requested workflow support;
- changing production behavior;
- adding tests or fixtures that establish a distinct contract;
- updating documentation required by the same behavior.

Keep a change and its directly required test or documentation together when
splitting them would leave either commit misleading or broken.
