Register-ArgumentCompleter -Native -CommandName yaga -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $Env:_YAGA_COMPLETE = "complete_powershell"
    $Env:_TYPER_COMPLETE_ARGS = $commandAst.ToString()
    $Env:_TYPER_COMPLETE_WORD_TO_COMPLETE = $wordToComplete
    try {
        yaga | ForEach-Object {
            $commandArray = $_ -Split ":::"
            $command = $commandArray[0]
            $helpString = if ($commandArray.Length -gt 1 -and $commandArray[1]) { $commandArray[1] } else { " " }
            [System.Management.Automation.CompletionResult]::new(
                $command, $command, 'ParameterValue', $helpString)
        }
    } finally {
        Remove-Item Env:\_YAGA_COMPLETE, Env:\_TYPER_COMPLETE_ARGS, Env:\_TYPER_COMPLETE_WORD_TO_COMPLETE -ErrorAction SilentlyContinue
    }
}
