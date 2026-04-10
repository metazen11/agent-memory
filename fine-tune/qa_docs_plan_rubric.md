# QA + Docs + Plan Rubric

Use this rubric to tune rewards toward your standards.

## What "good" means in this ecosystem

1. Correctness first: tool chain completes with no command/path/permission errors.
2. Planning quality: proposes explicit, testable steps and follows through.
3. QA discipline: runs tests relevant to changed code and calls out residual risk.
4. Documentation completeness: updates `README.md` / `handoff.md` / docs when behavior changes.
5. Evidence quality: provides concrete outputs, file references, and verification.

## Reward tuning guidance

- Increase `has_qa_signal` and `has_test_signal` if testing is under-emphasized.
- Increase `has_docs_signal` if docs updates are often skipped.
- Increase `has_plan_signal` and `has_review_signal` if planning/review rigor is low.
- Decrease `tool_count_bonus_per_step` if long chains are rewarded despite weak quality.

Profile file: `fine-tune/rl_reward_profile.json`
