#!/bin/sh
# A workspace layer over tmux, applied to every session rather than to repos.
#
# Every session is a workspace. The part of its name before the last "@" is its
# project; a session with no "@" is that project's default workspace, shown as
# "main". A project is an identifier and nothing more — it is often a directory
# path, because that is what `tn` names a session, but it is never used as one.
# A project's directory is the working directory of its default workspace, and a
# workspace can be given its own directory at creation, which is how a worktree
# checkout gets one.
#
# tmux format-expands the arguments of run-shell and display-popup before the
# shell sees them, so a `#{...}` format inside a binding would be substituted
# with the current session's value rather than looped. Keeping the formats in a
# script file avoids that entirely.
#
# Modes: pick | next | prev | new <name> [dir] | add <name> [dir]
#      | move left|right | render | closed <session> | click <range>

# Empty when no client is attached, which the hook-fired modes have to survive:
# a project's last session closing takes the client with it.
project=$(tmux display-message -p '#{client_session}')
project=${project%@*}

# Modes that act on the client's own project have nothing to act on without one.
require_project() {
	[ -n "$project" ] || exit 0
}

config_dir="$HOME/.tmux/workspaces"
legacy_dir="$HOME/.tmux/workspace-order"

# One file per project. The name is percent-encoded rather than flattened, so it
# survives the round trip and nothing has to be stored inside the file to
# recover it. Concurrent writers are possible — a hook-fired render overlaps a
# hand-run one — but both compute the same content for a given project from the
# same tmux state, so the last write wins harmlessly and no lock is needed.
config_file() {
	printf '%s/%s.json\n' "$config_dir" \
		"$(printf '%s' "$1" | sed 's/%/%25/g; s|/|%2F|g')"
}

config_project() {
	printf '%s' "${1##*/}" | sed 's/\.json$//; s|%2F|/|g; s/%25/%/g'
}

config_get() {
	jq -r --arg p "$1" "$2" "$(config_file "$1")" 2>/dev/null
}

# Reads the workspace order from stdin, one per line. Written to a temporary and
# renamed, because a plain redirect truncates first and a reader can catch the
# file half-written; rename is atomic.
config_put() {
	mkdir -p "$config_dir"
	tmp=$(mktemp "$config_dir/.tmp.XXXXXX") || return 1
	# The default workspace is stored as null. Every other slot is the workspace
	# name, and a name is a string, so null is the one marker no workspace can
	# be called — "main" cannot serve, since a workspace may be named that.
	if jq -R -s --arg p "$1" --arg dir "$2" \
		'{dir: $dir,
		  order: (split("\n") | map(select(length > 0))
		          | map(if . == $p then null else . end))}' > "$tmp"; then
		mv "$tmp" "$(config_file "$1")"
	else
		rm -f "$tmp"
	fi
}

# A config outlives its project on purpose: reopening a checkout should find the
# order it was left in. What it must not outlive is the project's existence, so
# a config with no live workspace is kept only while its name still points at a
# directory — true of every project `tn` creates, and of none of the numbered
# sessions a bare `tmux` leaves behind.
gc() {
	# A writer killed mid-save leaves its mktemp behind; nothing else will.
	find "$config_dir" -name '.tmp.*' -mmin +5 -delete 2>/dev/null
	live=$(projects)
	for f in "$config_dir"/*.json; do
		[ -f "$f" ] || continue
		proj=$(config_project "$f")
		printf '%s\n' "$live" | grep -qxF "$proj" && continue
		[ -d "$proj" ] || rm -f "$f"
	done
}

# A workspace's session name: the default workspace is the bare project.
session_name() {
	if [ "$2" = "$1" ]; then printf '%s\n' "$1"; else printf '%s@%s\n' "$1" "$2"; fi
}

# Every workspace of a project, as "<session id>\t<workspace>". The workspace of
# a default session is its whole name, which is the project — session_name and
# the "main" label both key off that equality.
workspaces() {
	tmux list-sessions -F '#{session_id}	#{session_name}' |
		awk -F'\t' -v p="${1:-$project}" '
			{ prefix = $2; sub(/@[^@]*$/, "", prefix)
			  if (prefix != p) next
			  name = $2; sub(/^.*@/, "", name)
			  print $1 "\t" name }'
}

# Every project with a live workspace, one per line.
projects() {
	tmux list-sessions -F '#{session_name}' | sed 's|@[^@]*$||' | sort -u
}

# Where a project lives: the default workspace's working directory, recorded in
# the config so it survives that workspace being killed — a sibling's directory
# would do as a stand-in but a checkout workspace has a different one, and
# respawning main into a worktree is wrong. The project identifier is never
# consulted; it is frequently a path and just as frequently not one.
project_dir() {
	proj=${1:-$project}
	dir=$(tmux list-sessions -F '#{session_name}	#{session_path}' |
		awk -F'\t' -v p="$proj" '$1 == p { print $2; exit }')
	[ -n "$dir" ] || dir=$(config_get "$proj" '.dir // empty')
	[ -n "$dir" ] || dir=$(tmux display-message -p '#{session_path}')
	printf '%s\n' "$dir"
}

# The project's worktree checkouts, taken from git rather than from a glob: they
# are nested to no fixed depth, so a glob deep enough to reach the nested ones
# descends into the shallow ones and offers their subdirectories.
checkouts() {
	[ -n "$CHECKOUTS" ] || CHECKOUTS=$(
		root=$(project_dir)
		git -C "$root" worktree list --porcelain 2>/dev/null |
			sed -n 's|^worktree ||p' |
			awk -v d="$root/.claude/worktrees/" 'index($0, d) == 1'
	)
	printf '%s\n' "$CHECKOUTS"
}

# A checkout is known by its leaf directory, qualified with the path back to the
# worktree root only when another checkout shares that leaf.
checkout_name() {
	rel=${1#"$(project_dir)/.claude/worktrees/"}
	leaf=${rel##*/}
	if [ "$(checkouts | sed 's|.*/||' | grep -cxF "$leaf")" -gt 1 ]; then
		printf '%s\n' "$rel"
	else
		printf '%s\n' "$leaf"
	fi
}

# Checkouts with no workspace yet, so the picker can offer one.
unclaimed() {
	live=$(workspaces | cut -f2)
	# Materialised before the loop: each iteration is a subshell, so a cache
	# filled inside one dies with it.
	CHECKOUTS=$(checkouts)
	checkouts | while read -r dir; do
		[ -n "$dir" ] || continue
		name=$(checkout_name "$dir")
		printf '%s\n' "$live" | grep -qxF "$name" || printf '%s\t%s\n' "$name" "$dir"
	done
}

# The project's workspaces in the user's order, as "<session id>\t<workspace>".
# Live workspaces absent from the file are appended in the order tmux reports
# them, so a newly created one lands at the end; names in the file that no
# longer exist are dropped. The file is rewritten so both corrections stick.
order() {
	proj=${1:-$project}
	live=$(workspaces "$proj")
	stored=$(config_get "$proj" '.order[]? | if . == null then $p else . end')
	# A plain-text order file from before the config existed seeds the new one.
	legacy="$legacy_dir/$(printf '%s' "$proj" | tr / %)"
	if [ -z "$stored" ] && [ -f "$legacy" ]; then
		stored=$(cat "$legacy")
		rm -f "$legacy"
	fi
	ordered=$(
		{
			printf '%s\n' "$stored" | while read -r name; do
				[ -n "$name" ] || continue
				printf '%s\n' "$live" | awk -F'\t' -v n="$name" '$2 == n { print; exit }'
			done
			printf '%s\n' "$live"
		} | awk -F'\t' '!seen[$2]++ && NF == 2'
	)
	printf '%s\n' "$ordered" | cut -f2 | config_put "$proj" "$(project_dir "$proj")"
	printf '%s\n' "$ordered"
}

# Swap the active workspace with its neighbour, and stop at either end rather
# than wrapping — a move that silently teleports across the bar is worse than
# one that does nothing.
move() {
	current=$(tmux display-message -p '#{client_session}')
	order | awk -F'\t' -v cur="${current##*@}" -v dir="$1" '
		{ s[NR] = $2; if ($2 == cur) at = NR }
		END {
			if (!at) at = 1
			i = (dir == "right") ? at + 1 : at - 1
			if (i >= 1 && i <= NR) { t = s[at]; s[at] = s[i]; s[i] = t }
			for (j = 1; j <= NR; j++) print s[j]
		}' | config_put "$project" "$(project_dir)"
	render
}

# One project's entries. Only the order is built here; everything client
# relative stays a live format, because status-format is a server-global option
# and a fully resolved string would show one client's project and highlight to
# every other client. So every project's entries are emitted, each guarded by a
# test against the drawing client's own project, with the highlight a second
# test against its current workspace. Entries carry a session id: tmux caps a
# user range string at 16 bytes, which a session name overruns. A "#" in a label
# would open a format sequence, so it is doubled, and the commas inside a style
# must be escaped or they terminate the enclosing conditional.
# The workspaces of one project tend to share a head — a ticket, a component —
# that repeats in every entry and distinguishes none of them. It is dropped, cut
# back to a separator so a word is never split in two, and only when at least two
# named workspaces share it. The default workspace is labelled "main" and takes
# no part in the calculation, which would otherwise always come out empty.
common_prefix() {
	awk '
		NR == 1 { p = $0; next }
		{
			n = 0
			while (n < length(p) && substr(p, n + 1, 1) == substr($0, n + 1, 1)) n++
			p = substr(p, 1, n)
		}
		END {
			if (NR < 2) exit
			while (length(p) && p !~ /[-_.]$/) p = substr(p, 1, length(p) - 1)
			print p
		}'
}

# What is left after the shared head, clipped from the front: a name too long to
# fit differs from its siblings at the end, having already shared its beginning.
shorten() {
	printf '%s\n' "$1" | awk -v pre="$2" -v max=24 '
		{
			s = $0
			if (pre != "" && index(s, pre) == 1) s = substr(s, length(pre) + 1)
			if (s == "") s = $0
			if (length(s) > max) s = "…" substr(s, length(s) - max + 2)
			print s
		}'
}

# The leftmost segment, in both renderings: where the drawing client sits in the
# order, and how many workspaces there are to sit in. It opens the picker, which
# is the one action worth a click when the list is too long to click through.
counter() {
	mine="#{==:#{s|@[^@]*\$||:client_session},$1}"
	rows=$(order "$1")
	count=$(printf '%s\n' "$rows" | grep -c .)
	i=0
	printf '%s\n' "$rows" | while IFS="	" read -r id name; do
		[ -n "$id" ] || continue
		i=$((i + 1))
		printf '#{?%s,#{?#{==:#{client_session},%s},#[range=user|S:pick]#[fg=#6c7086] %s/%s #[default]#[norange],},}' \
			"$mine" "$(session_name "$1" "$name")" "$i" "$count"
	done
}

# The narrowest client attached, since the bar is one server-global string that
# every client draws. No client yet means the bar is being built at load, before
# anyone can see it; a width of zero fails the fit and the resize hook redraws.
narrowest_client() {
	w=$(tmux list-clients -F '#{client_width}' | sort -n | head -1)
	printf '%s\n' "${w:-0}"
}

# The entries a project contributes, listed while the list fits and summarised
# when it does not. Clipping silently is the one thing not on offer: a list cut
# off at the screen edge still reads as the whole list, and the entry it drops
# first is the active one, the moment you have moved rightwards through them.
entries() {
	mine="#{==:#{s|@[^@]*\$||:client_session},$1}"
	rows=$(order "$1")
	shared=$(printf '%s\n' "$rows" | cut -f2 | grep -vFx "$1" | common_prefix)
	labels=$(printf '%s\n' "$rows" | while IFS="	" read -r id name; do
		[ -n "$id" ] || continue
		[ "$name" = "$1" ] && printf 'main\n' || shorten "$name" "$shared"
	done)
	# Each entry costs its label plus the space either side, against what the
	# heading and the prefix indicator leave.
	total=$(printf '%s\n' "$labels" | awk '{ w += length($0) + 2 } END { print w + 0 }')
	if [ "$total" -le $(( $(narrowest_client) - 12 - 9 )) ]; then
		list "$1" "$rows" "$shared" "$mine"
	else
		summary "$1" "$rows" "$shared" "$mine"
	fi
}

list() {
	printf '%s\n' "$2" | while IFS="	" read -r id name; do
		[ -n "$id" ] || continue
		[ "$name" = "$1" ] && label=main || label=$(shorten "$name" "$3")
		printf '#{?%s,#[range=user|S:%s]#{?#{==:#{client_session},%s},#[fg=#11111b#,bg=#89dceb#,bold],#[fg=#89dceb]} %s #[default]#[norange] ,}' \
			"$4" "$id" "$(session_name "$1" "$name")" \
			"$(printf '%s' "$label" | sed 's/#/##/g')"
	done
}

# Only the workspace being drawn for, and its place in the order. Every entry is
# emitted and all but one guarded away, because which is active is the drawing
# client's to answer and the string is written before any client is known.
summary() {
	count=$(printf '%s\n' "$2" | grep -c .)
	i=0
	printf '%s\n' "$2" | while IFS="	" read -r id name; do
		[ -n "$id" ] || continue
		i=$((i + 1))
		[ "$name" = "$1" ] && label=main || label=$(shorten "$name" "$3")
		printf '#{?%s,#{?#{==:#{client_session},%s},#[fg=#11111b#,bg=#89dceb#,bold] %s #[default] ,},}' \
			"$4" "$(session_name "$1" "$name")" \
			"$(printf '%s' "$label" | sed 's/#/##/g')"
	done
}

# Where a closing session hands its client. tmux reads "next" and "previous" off
# the session list, which is name order across every project, so the direction
# that lands on a sibling differs per session and has to be worked out for each.
# A project's only session has no such direction: it detaches, since anything
# else surfaces an unrelated project.
destroy_policy() {
	tmux list-sessions -F '#{session_id}	#{session_name}' |
		LC_ALL=C sort -t'	' -k2 |
		awk -F'\t' '
			{ id[NR] = $1; proj[NR] = $2; sub(/@[^@]*$/, "", proj[NR]) }
			END {
				for (i = 1; i <= NR; i++) {
					n = i % NR + 1
					p = (i + NR - 2) % NR + 1
					if (n != i && proj[n] == proj[i]) v = "next"
					else if (p != i && proj[p] == proj[i]) v = "previous"
					else v = "on"
					print id[i] "\t" v
				}
			}' |
		while IFS='	' read -r id v; do
			tmux set -t "$id" detach-on-destroy "$v"
		done
}

# The bar, plus the status height each session gets: a project of one is
# indistinguishable from a plain tmux session, so it keeps a single status line
# and only grows the bar once there is somewhere to switch to.
render() {
	bar="#[align=left]"
	for proj in $(projects); do
		ids=$(workspaces "$proj" | cut -f1)
		height=2
		[ "$(printf '%s\n' "$ids" | wc -l)" -gt 1 ] || height=on
		for id in $ids; do tmux set -t "$id" status "$height"; done
		[ "$height" = 2 ] && bar="$bar$(counter "$proj")$(entries "$proj")"
	done
	# Right-aligned tail: lit while the prefix key table is active, which covers
	# the prefix itself and the repeat-time window an -r binding leaves open.
	bar="$bar#[align=right]#{?client_prefix,#[bg=#fab387#,fg=#11111b#,bold] PREFIX #[default] ,}"
	tmux set -g 'status-format[1]' "$bar"
}

# A name matching a checkout gives a checkout workspace rooted there; anything
# else shares the project's directory unless one is given. Prints
# "<workspace>\t<directory>".
resolve() {
	if [ -n "$2" ]; then
		printf '%s\t%s\n' "$1" "$2"
		return
	fi
	root=$(project_dir)
	dir=$(checkouts | awk -v d="$root/.claude/worktrees/" -v n="$1" '
		{ rel = substr($0, length(d) + 1); leaf = rel; sub(/^.*\//, "", leaf)
		  if (rel == n || leaf == n) { print; exit } }')
	if [ -n "$dir" ]; then
		printf '%s\t%s\n' "$(checkout_name "$dir")" "$dir"
	else
		printf '%s\t%s\n' "$1" "$root"
	fi
}

# A session per workspace, created on demand. tmux rewrites ":" and "." in a
# session name, so the id is read back from the creation rather than
# reconstructed from the name we asked for.
create() {
	id=$(order | awk -F'\t' -v n="$1" '$2 == n { print $1; exit }')
	[ -n "$id" ] || id=$(tmux new-session -dP -F '#{session_id}' \
		-s "$(session_name "$project" "$1")" -c "$2")
	printf '%s\n' "$id"
}

ensure() {
	tmux switch-client -t "$(create "$1" "$2")"
}

hop() {
	# The client's workspace, not the calling pane's: a switch leaves the two
	# pointing at different workspaces.
	current=$(tmux display-message -p '#{client_session}')
	order | awk -F'\t' -v cur="${current##*@}" -v dir="$1" '
		{ s[NR] = $1; if ($2 == cur) at = NR }
		END {
			if (NR == 0) exit 1
			if (!at) at = 1
			i = (dir == "next") ? at % NR + 1 : (at + NR - 2) % NR + 1
			print s[i]
		}'
}

# A default workspace dies with its last window like any vanilla session, but
# only when it is the last of its project: while siblings remain the project
# still needs the workspace they are siblings of, so it comes back, rooted where
# a sibling lives.
closed() {
	proj=${1%@*}
	[ "$1" != "$proj" ] || {
		remaining=$(workspaces "$proj" | wc -l)
		[ "$remaining" -gt 0 ] && tmux new-session -d -s "$proj" -c "$(project_dir "$proj")"
	}
	gc
}

case "$1" in
pick)
	require_project
	sel=$(
		{
			order      | awk -F'\t' -v p="$project" '{ print "  " ($2 == p ? "main" : $2) "\ts\t" $1 }'
			unclaimed  | awk -F'\t' '{ print "+ " $1 "\tc\t" $2 }'
		} | fzf --reverse --with-nth=1 --delimiter='\t' \
			--bind 'right-click:abort' \
			--prompt='workspace> ' \
			--header='+ marks a checkout with no workspace yet · right-click to close'
	) || exit 0
	[ -n "$sel" ] || exit 0
	arg=$(printf '%s' "$sel" | cut -f3)
	case $(printf '%s' "$sel" | cut -f2) in
	s) tmux switch-client -t "$arg" ;;
	c) ensure "$(printf '%s' "$sel" | cut -f1 | sed 's/^+ //')" "$arg" ;;
	esac
	;;
next | prev)
	require_project
	target=$(hop "$1") && [ -n "$target" ] && tmux switch-client -t "$target"
	;;
new | add)
	require_project
	[ -n "$2" ] || { tmux display-message "workspace: no name given"; exit 1; }
	r=$(resolve "$2" "$3")
	name=$(printf '%s' "$r" | cut -f1)
	dir=$(printf '%s' "$r" | cut -f2)
	if [ "$1" = add ]; then
		# Created detached and left that way, so a caller running inside another
		# workspace does not move the user's viewport. Prints the session name.
		tmux display-message -p -t "$(create "$name" "$dir")" '#{session_name}'
	else
		ensure "$name" "$dir"
	fi
	;;
move)
	require_project
	case "$2" in
	left | right) move "$2" ;;
	*) echo "usage: workspace.sh move left|right" >&2; exit 2 ;;
	esac
	;;
render)
	destroy_policy
	render
	;;
closed)
	closed "$2"
	destroy_policy
	render
	;;
click)
	# The bar tags each entry with a session id ($N); tmux caps a user range
	# string at 16 bytes, which a session name overruns.
	case "$2" in
	S:pick) tmux display-popup -E "workspace.sh pick" ;;
	S:\$*) tmux switch-client -t "${2#S:}" ;;
	esac
	;;
*)
	echo "usage: workspace.sh pick|next|prev|new <name> [dir]|add <name> [dir]|move left|right|render|closed <session>|click <range>" >&2
	exit 2
	;;
esac
