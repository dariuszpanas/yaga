#compdef yaga

_yaga_completion() {
  eval $(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" _YAGA_COMPLETE=complete_zsh yaga)
}

compdef _yaga_completion yaga
