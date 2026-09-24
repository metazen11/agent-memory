# Contributing

The branching contract for this repo. It lives here — versioned, in the
repo — so it reaches every machine and every agent that clones it, rather
than living in one developer's `~/.claude/`.

## Trunks

| Branch | Role | How it receives commits |
|---|---|---|
| `main` | production | **only** a reviewed PR from `dev` |
| `dev` | integration | direct pushes, after a green local gate |

## Rules

1. **Never push directly to `main`.** Work lands on `dev`, and `dev` reaches
   `main` through a pull request. That PR is the single human review gate.
2. **Never let `dev` fall behind `main`.** If something reaches production
   without passing through `dev`, the next `dev -> main` PR renders those
   commits as **deletions** and silently reverts shipped work. Fix it the
   moment you notice:
   ```
   git checkout dev && git fetch origin && git merge origin/main && git push origin dev
   ```
3. **Don't promote red work.** A `dev -> main` PR merges only with CI green.
4. **Verify the end state, not the action.** "I pushed it" is not "it landed".
   Check the branch, the PR, the check.

## Enforcement — four layers, by strength

| Layer | Where it runs | Bypassable? |
|---|---|---|
| **Branch protection** on `main` | GitHub (server) | No — `enforce_admins` is on |
| **`trunk-drift` workflow** | GitHub Actions | No — fails the check |
| **`.githooks/pre-push`** | your machine | Yes: `ALLOW_PROTECTED_PUSH=1` (audited) |
| **This document** | your eyes | Entirely |

Only the top two travel automatically. The hook needs one command per clone
(below); this file needs someone to read it.

## Setup — once per MACHINE

```
~/_CODING/hooks/repo-contract/bootstrap.sh
```

That does two things, and is safe to re-run:

1. Installs a **git template** so every *future* clone on this machine
   activates its own `.githooks/` automatically — no per-repo step ever again.
2. **Retrofits existing clones** under `~/_CODING` (a template only applies at
   clone time, so repos you already have need this once).

Neither step overrides a `core.hooksPath` you set yourself.

Prefer to do it by hand, or working in one repo only?

```
git config core.hooksPath .githooks
```

Check either way:

```
~/_CODING/hooks/repo-contract/bootstrap.sh --check
git config --get core.hooksPath          # expect: .githooks
```

### Why this step exists at all

Git deliberately refuses to let a cloned repo activate its own hooks — that
would execute a stranger's code the moment you clone. The template inverts
that safely: *you* install it once, locally, and it then applies your choice
to your own clones. There is no way to remove the step entirely, which is
exactly why the two server-side layers above carry the real weight.

## Emergencies

`ALLOW_PROTECTED_PUSH=1 git push ...` skips the local hook and prints a
warning. It does **not** skip GitHub branch protection — that needs an
explicit, logged settings change. If you find yourself reaching for it
regularly, the contract is wrong; fix the contract, not the push.
