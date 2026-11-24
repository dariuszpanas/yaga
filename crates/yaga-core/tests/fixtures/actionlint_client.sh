#!/bin/sh
# Isolated mock. All state is in the test's temporary directory.
set -eu
[ "$PATH" = "$FIXTURE" ]
[ -z "${HOME-}" ]
stage_id=1111111111111111111111111111111111111111111111111111111111111111
lint_id=2222222222222222222222222222222222222222222222222222222222222222
case "$1 $2" in
    'volume create')
        printf '%s' "$5" > "$FIXTURE/volume"
        printf '%s\n' "$5";;
    'volume ls')
        # Cleanup must prove the last execution container absent before its volume.
        [ ! -f "$FIXTURE/exited-lint" ]
        if [ -f "$FIXTURE/invalid-volume-list" ]; then printf '%s\n' other-volume
        elif [ -f "$FIXTURE/volume" ]; then /bin/cat "$FIXTURE/volume"; fi;;
    'volume rm')
        [ "$4" = "$(/bin/cat "$FIXTURE/volume")" ]
        /bin/rm "$FIXTURE/volume";;
    'create --name')
        case "$3" in
            yaga-actionlint-stage-*) printf '%s' "$3" > "$FIXTURE/stage"; printf '%s\n' "$stage_id";;
            *) exit 2;;
        esac;;
    'create --rm')
        [ "$3" = --name ]
        [ ! -f "$FIXTURE/exited-lint" ]
        case "$4" in
            yaga-actionlint-lint-*) printf '%s' "$4" > "$FIXTURE/lint"; printf '%s\n' "$lint_id";;
            *) exit 2;;
        esac;;
    'cp -')
        [ "$3" = "$stage_id:/workspace" ]
        /bin/cat > "$FIXTURE/archive";;
    'start --attach')
        [ "$4" = "$lint_id" ]
        /bin/cat >> "$FIXTURE/input"
        /bin/mv "$FIXTURE/lint" "$FIXTURE/exited-lint"
        printf '[]';;
    'container ls')
        if [ -f "$FIXTURE/exited-lint" ]; then
            name=$(/bin/cat "$FIXTURE/exited-lint")
            [ "$9" = "name=^/$name\$" ]
            [ "$7" = "label=io.yaga.actionlint.run=${name##*-}" ]
            /bin/rm "$FIXTURE/exited-lint"
        fi
        for phase in stage lint; do
            if [ -f "$FIXTURE/$phase" ]; then
                name=$(/bin/cat "$FIXTURE/$phase")
                [ "$9" = "name=^/$name\$" ]
                [ "$7" = "label=io.yaga.actionlint.run=${name##*-}" ]
                if [ "$phase" = stage ]; then printf '%s' "$stage_id"; else printf '%s' "$lint_id"; fi
            fi
        done;;
    'container rm')
        case "$4" in
            "$stage_id") /bin/rm "$FIXTURE/stage";;
            "$lint_id") /bin/rm "$FIXTURE/lint";;
            *) exit 2;;
        esac;;
    *) exit 2;;
esac
