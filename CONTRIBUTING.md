# Contributing

The branching contract for this repo. It lives here — versioned, in the
repo — so it reaches every machine and every agent that clones it, rather
than living in one developer's `~/.claude/`.

## One trunk

`main` is the only long-lived branch and is production. Every change lands
as a **squash-merged pull request from a feature branch**. There is no `dev`,
no promotion step, and no drift rule.

This repo is not human-gated: a PR merges once CI, independent review and the
auditor pass. A repo opts into a human gate by requiring approving reviews on
`main`.

## Rules

1. **Feature branch → squash PR to `main`.** Name it `work/issue-<n>-<slug>`
   (or `codex/<slug>`); every change is anchored to a GitHub issue with
   acceptance criteria. Never push directly to `main`.
2. **The merge gate is all three of:**
   - an **independent code review** (a separate context from the author),
   - an **`aa_auditor` PASS** against the originating issue's acceptance
     criteria, with evidence,
   - the **`quality` and `integrity`** checks green.
3. **Squash-only merges.** One commit per PR on `main`; checkpoint commits are
   squashed away.
4. **Every change to `main` auto-deploys** to the live checkout
   (`scripts/deploy_live.sh`, see [docs/DELIVERY.md](docs/DELIVERY.md)). Don't
   merge red or unreviewed work — it ships within minutes.
5. **Verify the end state, not the action.** "I merged it" is not "it landed".
   Check the PR, the checks, and the live `/api/health`.

## Enforcement — layers, by strength

| Layer | Where it runs | Bypassable? |
|---|---|---|
| **Branch protection** on `main` | GitHub (server) | No — `enforce_admins` is on |
| **`.githooks/pre-push`** | your machine | Yes: `ALLOW_PROTECTED_PUSH=1` (audited) |
| **This document** | your eyes | Entirely |

Only the top layer travels automatically. The hook needs one command per
clone (below); this file needs someone to read it. `.reconcile-gate.json`
tells the agent-side `reconcile-gate` hook that feature → `main` PRs are
allowed in this repo.

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
exactly why the server-side layer above carries the real weight.

## Emergencies

`ALLOW_PROTECTED_PUSH=1 git push ...` skips the local hook and prints a
warning. It does **not** skip GitHub branch protection — that needs an
explicit, logged settings change. If you find yourself reaching for it
regularly, the contract is wrong; fix the contract, not the push.

## Tested delivery

See [docs/DELIVERY.md](docs/DELIVERY.md) for the required CI checks, disposable
integration stage, exact-commit release artifacts, local production CD, and
rollback contract. Merging to `main` requires `quality` and `integrity`.
