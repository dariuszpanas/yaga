#!/bin/sh
# Install the PyPI distribution into a per-user uv tool environment.
set -eu

install_yaga() (
    package=yaga-cli
    if [ -n "${YAGA_VERSION:-}" ]; then
        case "$YAGA_VERSION" in
            *[!0-9.]*|'') echo 'YAGA_VERSION must be a final X.Y.Z release' >&2; exit 2 ;;
        esac
        if ! printf '%s\n' "$YAGA_VERSION" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'; then
            echo 'YAGA_VERSION must be a final X.Y.Z release' >&2
            exit 2
        fi
        package="yaga-cli==$YAGA_VERSION"
    fi
    if command -v uv >/dev/null 2>&1; then
        uv_bin=$(command -v uv)
    else
        installer=$(mktemp)
        trap 'rm -f "$installer"' EXIT HUP INT TERM
        if command -v curl >/dev/null 2>&1; then
            curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
                --connect-timeout 15 --max-time 120 \
                https://astral.sh/uv/0.12.7/install.sh --output "$installer"
        elif command -v wget >/dev/null 2>&1; then
            wget --https-only --timeout=120 --tries=1 \
                --output-document "$installer" https://astral.sh/uv/0.12.7/install.sh
        else
            echo 'Installing uv requires curl or wget; install either tool or uv, then retry.' >&2
            exit 2
        fi
        UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh "$installer"
        uv_bin="$HOME/.local/bin/uv"
    fi
    "$uv_bin" --no-config tool install --python 3.12 "$package"
    case "$(uname -s)" in
        MSYS*|MINGW*|CYGWIN*)
            tool_bin=$("$uv_bin" --no-config tool dir --bin)
            shell_bin=$(cygpath -u "$tool_bin" | sed "s/'/'\\\\''/g")
            shell_uv_bin=$(cygpath -u "$(dirname "$uv_bin")" | sed "s/'/'\\\\''/g")
            printf '%s\n' 'YAGA installed. In MSYS2, Git Bash, or Cygwin, run in your current shell:'
            printf "  export PATH='%s':'%s':\"\$PATH\"\n" "$shell_bin" "$shell_uv_bin"
            printf '%s\n' \
                '  hash -r' \
                '  yaga --help' \
                'Add the export line to ~/.zshrc (zsh) or ~/.bashrc (bash) to keep it for new shells.'
            ;;
        *)
            if [ "${YAGA_NO_MODIFY_PATH:-0}" != 1 ]; then
                "$uv_bin" --no-config tool update-shell
            fi
            printf '%s\n' 'YAGA installed. Open a new terminal and run yaga --help.'
            ;;
    esac
    printf '%s\n' \
        'Update: yaga self update. Remove: uv tool uninstall yaga-cli.'
)

install_yaga
