# ADR-001: Consolidate scattered local copies of LavaCast 40

- **Status:** Accepted — archival executed 2026-08-27; deletion NOT authorised
- **Date:** 2026-08-27
- **Repo:** github.com/lavabeard/Lavacaster-code
- **Supersedes:** nothing. First ADR in this repo.
- **Related:** vault `20_Decisions/2026-08-22-vault-and-agent-setup.md` (ADRs live in the code repo) — followed, not contradicted.

## Context

Six local copies of this repo existed on one machine, plus seven loose `setup_lavacast40*.sh`
scripts in `$HOME`. Nobody could say which copy was live without running `git log` in each.

| Path | State before consolidation |
|---|---|
| `~/Documents/Github/Lavacaster` | Only path GitHub Desktop tracks. `main` @ `5c325fd`, 52 behind `origin/main`, 1 local-only commit, `streamer.py` staged conflicted (`UU`, markers 312-320), no `MERGE_HEAD` |
| `~/Lavacaster-code` | Head of a chain of **six** clones nested six levels deep — `764d939`, `67fc7cd`, `05dc44c`, `2379a97`, `da360b8`, `f7c1fff`. Each holds one local-only commit; all trees clean. The path the vault project note named |
| `~/Documents/Github/Lavacaster-code-main` | Not a repo — zip extract, mtime 2026-02-28 |
| `~/Downloads/lavacaster` | Snapshot, no `.git` |
| `~/Downloads/Lavacaster-code-34aff6b… 2` | Snapshot, no `.git` |

`origin/main` = `61fc380` (2026-03-02), confirmed current by an actual `git fetch`, not a cached ref.

**All seven local-only commits were redundant.** The six in the nested chain are the same hand-rolled
revert of main back to the PR #38 state (`9c95042`); the canonical copy's `5c325fd` was a separate
half-finished revert of PR #37, matching its conflicted `streamer.py`. Upstream had already done this
work properly — `d884846`, merged as PR #44 — then built 40+ commits on top through PR #58.

### The AV constraint

This is show kit. It runs on a machine carried to a venue, plugged into a client network, and asked
to push 40 multicast streams while somebody waits. The binding requirement is not elegance — it is
that at 02:00 on a load-in there is exactly one directory and you know which one. Every minute spent
in `git log` comparing four clones is a minute the room is dark.

### The remote has been idle ~6 months

Last upstream commit 2026-03-02. Work stopped at the start of March. This was a fossil, not an
active divergence — no in-flight work to coordinate with, and no time pressure to rush the cleanup.

## Evidence

Four independent checks, all completed before anything moved:

1. **Unique commit objects, but zero unique content.** This one was initially got wrong and is
   recorded precisely because of it. The first pass reported "no unique commits"; that is false.
   The six nested clones hold six commit objects — `764d939`, `67fc7cd`, `05dc44c`, `2379a97`,
   `da360b8`, `f7c1fff` — none of which resolve in the canonical repo. But all six share **one
   identical tree**, `cfdcc377b4230da56ef7f4675fcfcaa246afe5e9`, and **one identical parent**,
   `4b41986`. That tree is byte-identical to upstream `d884846` (PR #44), whose parent is an
   ancestor of `main`. The six differ from upstream and from each other only by author timestamp.
   So: six unique commit *objects*, zero unique *content*. Every clone had only `main`, no stashes,
   and no extra branches.
2. **No unique file content.** Every file in the three non-git copies was hashed with
   `git hash-object` and located in git history: the zip extract sits around `479f907`,
   `~/Downloads/lavacaster` around `07a9654`, and the other Downloads copy around `4e45c4b`/`6af16a6`.
   All are plain historical snapshots.
3. **No unique runtime state.** `.gitignore` excludes `lavacast_channels.json`, `channel_state.json`,
   `media/`, `logs/`, `venv/`, and `frontend/static/thumbnails/` — all stored *inside* the repo
   directory, so a clean `git status` says nothing about them. A filesystem search found **none of
   them anywhere on this Mac**. The app requires Ubuntu/Debian with `apt` (`install.sh:42`) and has
   never run here; this machine is a source-editing box and the streamer lives on a Linux host.
   No hand-tuned 40-channel map was ever at risk.
4. **No hardcoded references.** No hits in shell rc files, zero in `~/.zsh_history`, no launchd jobs,
   no systemd units. Only `~/.claude/projects/-Users-jeremymoulton-Lavacaster-code` orphans, which
   is session metadata.

Check 3 is the one that mattered most. The first two verified against *git*; only the third
verified against the *filesystem*, and it is the one that would have caused real loss had it come
back differently.

## Decision

### 1. `~/Documents/Github/Lavacaster` is the single canonical checkout

Chosen despite having the worst git state of the six, because its dirt had a deterministic fix —
four commands with four independent undo paths — while the alternative's cost was *tool
reconfiguration*, which is neither deterministic nor scriptable. GitHub Desktop keys repositories by
absolute path in its own application state; there is no config file to edit. Repointing means
Remove-then-Add through a GUI whose Remove dialog offers "also move to Trash" as an adjacent option.
Between running four git commands correctly and clicking through a destructive GUI dialog correctly,
the git commands are the safer bet.

Both preconditions the design depended on were verified rather than assumed:

- **GitHub Desktop is genuinely in use** — installed at `/Applications/GitHub Desktop.app` with a
  production log written the same day (2026-08-27 08:05). Its tracking is live and worth preserving.
- **`~/Documents` is not iCloud-synced** — a real directory, not redirected, with no `.icloud`
  placeholders under `Github/`. No risk of `.git` corruption or media eviction.

Had either failed, the fallback was `~/Lavacaster-code` after moving its nested clones out first.
Neither failed.

**Why `~/Lavacaster-code` lost:** it was the parent of two nested clones *of itself* — the specific
structure that produced this confusion, and blessing it invites a third recurrence. It is not what
GitHub Desktop sees. And the vault note's claim on it was one line of YAML, revertible in seconds.

**Accepted wart:** the directory is named `Lavacaster`, the repo is `Lavacaster-code`. Renaming
would break the GH Desktop tracking that is the reason for choosing it. The mismatch stays, recorded
here so a future session does not "fix" it.

### 2. Every other copy is archived, not deleted

All five copies and all seven setup scripts were moved to
`~/Archive/lavacast-consolidation-2026-08-27/`. Moving preserves reflogs; `rm` does not.

**Nothing was deleted.** Deleting that archive is a separate decision requiring separate approval,
after the canonical copy has been used successfully at least once, and no earlier than 2026-09-26.
A `README-DELETE-AFTER-2026-09-26.txt` in the archive names this ADR as the rationale.

### 3. Getting the canonical copy to origin/main — preserve, then discard

`git reset --hard origin/main`, but only after the discarded commit was made recoverable four ways:
a local tag `archive/pre-consolidation-2026-08-27`; a verified `git bundle --all`; a plain `cp` of
the conflicted `streamer.py` (the only artifact in the situation that existed in no commit
anywhere); and the reflog, which survives in place and is the fastest undo
(`git reset --hard HEAD@{1}`, good for 90 days at the `gc.reflogExpire` default).

`git bundle verify` ran *before* the reset, not after — the riskiest command in the plan executes
against an index with an unmerged entry and no `MERGE_HEAD`, a state git does not document
exhaustively because it should not occur.

**`git clean -xdf` must never be run in this repo.** `-x` ignores `.gitignore` and would delete
`lavacast_channels.json`, `media/`, `logs/`, and `venv/` on any machine where the app has actually
run. Only `git clean -nd` (dry run) was used here.

**Alternatives rejected:**

- **`git rebase origin/main`** — the local commit is a revert to the PR #38 state. It would conflict
  against 52 commits of upstream work, and if it applied cleanly it would re-introduce the exact
  regression upstream fixed by moving past PR #44. Replaying a revert on top of the work that
  superseded it is strictly harmful.
- **`git merge origin/main`** — produces a merge commit permanently recording the confusion in the
  graph. The goal was to make local identical to remote, not to negotiate with it.
- **Delete and re-clone to the same path** — would preserve GH Desktop tracking, but destroys the
  reflog (the fastest undo), destroys any gitignored runtime state, and leaves zero recovery if the
  "redundant work" conclusion were wrong. The reset costs four extra commands and keeps all of it.

### 4. The PR #37 channel-preservation fix — CONFIRMED still broken; reinstate

`7b99871` "Fix: prevent channel data loss when media files are temporarily missing on load" was
collateral damage of the PR #44 revert and was never reinstated. Current main has zero occurrences
of `_saved_channels` or `_removed_cids`.

The architect flagged this as unverified, because the `src_path` fallback at `streamer.py:363-371`
might have narrowed it. **Adversarial review has since confirmed the bug reproduces on current main,
unchanged.** Both halves of the root cause are intact:

- `_load_state` (`streamer.py:363-377`) `continue`s past any channel where neither `filepath` nor
  `src_path` resolves — the entry never enters `self.metadata`.
- `_save_state` (`streamer.py:306-309`) rebuilds `"channels"` purely from `self.metadata`, so the
  first save after a lossy load erases the skipped entries permanently.

The `src_path` fallback narrows the trigger to *both* paths missing, which is mundane: media on an
unmounted external or NFS volume, a changed `media_path`, or a late mount at boot. The systemd unit
(`install.sh:172-173`) orders only on `network-online.target` with no `RequiresMountsFor=`, so a
late mount drops all 40 channels — and then *any* settings save (NIC, bitrate, encap, auto-start,
global transcode) writes `"channels": {}` and the entire 40-channel assignment map is gone.
`_channel_entry` storing absolute paths rooted at `_BASE_DIR` means moving the repo directory is
itself a trigger, which is why archiving preceded any move here.

**Decision: reinstate, as its own PR**, plus a guard that refuses to write an empty `channels`
section when the previous on-disk file was non-empty unless every channel was explicitly removed.
It is a port, not a revert-of-a-revert: a cherry-pick of `7b99871` cannot apply, since both
`_saved_channels` and `_removed_cids` must be reintroduced. Empirical confirmation on a scratch copy
should still be run before the fix ships, and the procedure filed in `60_QA/Items/` as a
commissioning check.

**Named hazard for whoever ports it:** `_removed_cids` is what stops the merge resurrecting
deliberately deleted channels. If `remove_channel` (:463-470) fails to add to it, or it is not
persisted across restarts, a deleted channel reappears on the next save — a worse bug than the one
being fixed, and one that surfaces at a venue as a phantom multicast group.

### 5. The setup scripts

The seven `setup_lavacast40*.sh` files are self-contained generators (51KB→106KB, v1→v8) that write
`streamer.py`, `app.py`, `metrics.py`, `logger.py`, `index.html`, `boot_launch.sh`, and a systemd
unit via heredocs. All dated Feb 26-27, predating the git repo. The repo's in-tree `install.sh`
supersedes them. Archived, after a diff to confirm no operational knowledge was being discarded and
a credential grep before anything landed in an archive directory.

## Consequences

### What this makes better

One directory. GitHub Desktop, the vault note, and the filesystem all agree. `git status` in the
canonical copy means something. At 02:00 there is nothing to work out.

### What is lost

- **Six copies was, accidentally, a backup.** After this there is one local clone plus GitHub. Any
  full clone holds all history, so the real exposure is local disk failure with no internet — which
  is precisely the venue scenario. Backup status of this machine is unknown and should be confirmed;
  if there is no Time Machine target, that is a bigger finding than this ADR.
- **The ability to answer "what exactly was I running on 28 February."** Mitigated: the zip extract
  is archived rather than deleted, and its contents were confirmed reachable in git history.
- **Muscle memory.** `cd ~/Lavacaster-code` will fail. The reference sweep found nothing hardcoded,
  so the blast radius is human habit only.

### What breaks if the vault note is left stale

`50_Projects/Lavacaster-code/Lavacaster-code.md` carries `repo: ~/Lavacaster-code`. If that path is
archived and the field is not updated, the standing instruction "search the vault before answering"
starts returning a path that does not exist.

**The failure is quiet, which is what makes it bad.** An agent that cannot resolve the path will not
stop — it will fall back to searching the filesystem, find the canonical copy, and carry on, having
effectively ignored the vault. Worse, during the cooling window it might find an archived copy first
and work confidently in a stale tree, producing a diff against `5c325fd` that applies to nothing.
The vault's value is that it is authoritative; a stale `repo:` field makes it confidently wrong,
which is worse than no note at all.

Two edits are therefore part of this change, not follow-up: update `repo:`, and rewrite the
"Flagged, not resolved" paragraph about the nested clone — that flag is **resolved** by this ADR.

### Reversibility

| Action | Cost to undo |
|---|---|
| Canonical path choice | Cheap — one `mv`, one GH Desktop re-add |
| `git reset --hard` | Cheap — reflog, tag, or bundle |
| Moving copies to archive | Cheap — `mv` back |
| Vault note edits | Cheap — git-tracked vault |
| **`rm -rf` of the archive** | **One-way. Nothing recovers it.** |

Exactly one one-way door, and this ADR does not open it.

### Named failure mode of this design

**The archive becomes copy number seven.** "Move to archive, delete after 30 days" is a deferred
decision wearing a safety measure's clothing. In 30 days nobody will remember, and the next person
to grep for "Lavacaster" finds four clones in it. Partial mitigation: the dated
`README-DELETE-AFTER-2026-09-26.txt`. Honest assessment: if that fails, the outcome is one dated
directory instead of six scattered ones — a smaller mess, still a mess, and better said now than
discovered later.

## Open items

1. Port the channel-preservation fix (Decision 4) in its own PR, with empirical confirmation on a
   scratch copy first and the procedure filed in `60_QA/Items/`.
2. Act on the separate adversarial code review of `61fc380`, which found 18 confirmed defects
   including a config/documentation mismatch that sends streams to the wrong multicast group.
3. Vault note edits — `repo:` field and the resolved nested-clone flag.
4. Archive deletion approval, no earlier than 2026-09-26.
5. Confirm this machine has a backup target.
