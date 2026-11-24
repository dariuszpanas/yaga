_yaga_completion() {
    local IFS=$'\n'
    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \
                   COMP_CWORD=$COMP_CWORD \
                   _YAGA_COMPLETE=complete_bash $1 ) )
    return 0
}

complete -o default -F _yaga_completion yaga
