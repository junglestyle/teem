# Teem (archived)

> **Archived October 2026.** Teem works and its last release is [v0.0.10](../../releases/tag/v0.0.10), but I'm no longer developing it. The reasons are below.

Teem was a voice-driven coding system for one person. You sent a Telegram voice note, and a pull request came back.

```
voice note ─► decider ─► proposal ─► [Approve] ─► Claude Code implements
                                                        │
                                   objective checks ◄───┘
                                         │
                         Codex adversarial review (fresh context)
                                         │
                   pass ─► PR opened    fail ─► revise (max 2) ─┘
```

I built it in two weeks, from September 23 to October 6, 2026: about 7,000 lines of Python, PostgreSQL, rootless Podman, and an acceptance suite that runs the real agent containers against fake providers.

## What it did

1. **Propose.** A model-backed decider turned speech or text into a bounded Run, with a repository, an objective, acceptance criteria and a base commit. It saw conversation and status, never source code.
2. **Authorize.** Work started only on a tapped **Approve** button bound to that exact proposal, or under a standing grant the user created the same way. Nothing typed or spoken counted as approval, and no model could grant permissions, extend budgets or declare its own work done.
3. **Implement.** Claude Code ran in an isolated rootless Podman container whose only network path was an allowlisting egress proxy. GitHub credentials never entered an agent container.
4. **Verify.** Checks came from the repository. Then Codex, a different vendor, reviewed the exact frozen Candidate in a fresh session. It got the criteria, the diff and the check evidence, but never the implementer's conversation or self-assessment.
5. **Revise or deliver.** Failing checks or a changes-required review triggered up to two revisions. A passing Candidate was pushed to a `teem/<run>` branch as one idempotent pull request. Merging stayed with the human.

The full design is in [docs/architecture/v0.md](docs/architecture/v0.md), with the slices that built it alongside.

## What held up

- **The candidate/review gate.** Freezing an exact Candidate, then reviewing it with a different model family that can't see the implementer's reasoning, was the most valuable part of the design. Binding every review and check result to one immutable Candidate meant a revision could never inherit a stale pass.
- **Authority as data.** Recording each Approval as a durable record tied to a specific action made the system easy to reason about. A model's output was always evidence, never permission.
- **Measuring it.** The stats page tracked first-pass review rate, revisions per Run, and how many Teem pull requests I actually merged, per model. That turned "is this working?" into a number.

## Why I stopped

[Edit this in your own words. A suggested draft:] Most of Teem's code wasn't the interesting part. It was the plumbing around it: the chat interface, voice transcription, the job queue, leases, worker reconnection and deployment. A general agent runtime like Hermes already covers that ground. Maintaining my own version of it pulled time away from the parts worth building.

## Running it anyway

Setup, configuration and tests for v0.0.10 are in [docs/setup.md](docs/setup.md). They're unchanged, apart from the clone URL now pointing at this repository.
