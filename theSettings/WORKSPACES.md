# Workspaces

A workspace layer over tmux. tmux gives you sessions, windows and panes; this
adds one grouping above the window and below the session list, so a single
project can hold several sessions that you move between like windows.

## Concepts

**Workspace** — a tmux session. Every session is one, with no exceptions and
nothing to opt into.

**Project** — a group of workspaces. It is not a tmux object; it exists as the
part of a session's name before its last `@`. Two sessions share a project when
that part matches.

```
/Users/you/dev/api@billing
└────── project ──────┘ └ workspace ┘
```

**Default workspace** — the workspace of a session whose name has no `@`. Its
project is its whole name. It is displayed as `main`.

A project's name is an identifier and nothing else. It usually looks like a
directory because the `tn` alias names a session after `$(pwd)`, but it is never
used as a path.

**Project directory** — where the project lives: the working directory of its
default workspace. New workspaces start there unless given a directory of their
own.

**Checkout workspace** — a workspace rooted in one of the project's git worktree
checkouts under `<project directory>/.claude/worktrees/`. Its windows open in
that checkout.

**Plain workspace** — a workspace sharing the project directory.

## The bar

A project with two or more workspaces shows a second status line listing them.
A project with one workspace does not, so an ordinary tmux session looks
ordinary until you add to it.

The bar lists the workspaces of the current session's project only, in the order
you have arranged, labelled with the part after the `@` — or `main` for the
default workspace. The active workspace is highlighted.

At its right edge sits a `PREFIX` chip, lit while the prefix key table is
active: after the prefix key, and through the window a repeating binding leaves
open, during which the next press needs no prefix. A project with one workspace
has no bar, so its chip appears at the right of the status line instead.

## Keys

Workspaces answer the same shape of keys as windows. All four repeat, so hold
the prefix once and keep tapping.

| Key | Action | Window equivalent |
|---|---|---|
| `prefix ;` | next workspace | `prefix .` |
| `prefix l` | previous workspace | `prefix ,` |
| `prefix :` | move active workspace right | `prefix >` |
| `prefix L` | move active workspace left | `prefix <` |
| `prefix s` | workspace picker | `prefix W` |
| `prefix N` | new workspace, by name | `prefix n` |

A move stops at either end rather than wrapping, and persists.

Mouse: click a bar entry to switch to it, or scroll over the bar to cycle.
Scrolling over the window row still cycles windows.

## Picker

`prefix s` opens a fuzzy picker over the project's workspaces, plus its git
worktree checkouts that have no workspace yet. Those are marked `+`; choosing
one creates a checkout workspace rooted there.

A checkout is listed by its directory name, qualified with the path back to
`.claude/worktrees/` only when another checkout shares that name.

## Creating workspaces

`prefix N` prompts for a name. A name matching a checkout creates a checkout
workspace; any other name creates a plain workspace.

From a shell:

```sh
~/.tmux/scripts/workspace.sh new <name> [directory]   # create if absent, switch to it
~/.tmux/scripts/workspace.sh add <name> [directory]   # create if absent, leave detached
```

Both are idempotent — a second call finds the existing workspace. `add` never
moves the client, so a script can create a workspace without pulling the user
out of theirs. Pass a directory to root the workspace explicitly; this is how a
worktree checkout is attached to one.

## Lifecycle

A workspace is a session and dies with its last window, like any tmux session.

The default workspace is the exception: while any other workspace of its project
remains, closing it brings it back, detached, in the project directory. Alone,
it dies and the project goes with it.

## Configuration

Each project has a file under `~/.tmux/workspaces/`, named after the project
with `/` written `%2F` and `%` written `%25`.

```json
{
  "dir": "/Users/you/dev/api",
  "order": [null, "billing", "search"]
}
```

`dir` is the project directory, kept current from the default workspace and used
to place a new workspace and to respawn the default one.

`order` is the bar's order. `null` is the default workspace; every other element
is a workspace name. A new workspace is appended, so it appears at the end of
the bar until you move it.

The file is advisory. Names in it that no longer exist are ignored, live
workspaces missing from it are appended, and duplicates collapse — the file is
rewritten to match on the next update, so hand-editing cannot corrupt the bar.

A project's file is removed once it has no live workspaces, unless its name
still points at a directory on disk. So a project you reopen finds the order you
left it in, while a numbered throwaway session leaves nothing behind.

## Files

| Path | Purpose |
|---|---|
| `~/.tmux/scripts/workspace.sh` | all behaviour |
| `~/.tmux/workspaces/` | one config per project |
| `~/.tmux.conf` | key bindings and the hooks that keep the bar current |
