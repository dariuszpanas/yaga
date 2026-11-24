$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    try {
        $parentProcess = [System.Diagnostics.Process]::GetProcessById([int]$env:YAGA_UPDATE_PARENT)
        if ($parentProcess.MainModule.FileName -ine $env:YAGA_UPDATE_IMAGE) { throw 'launcher identity changed' }
        if (-not $parentProcess.WaitForExit(60000)) { throw 'launcher timeout' }
    } catch [System.ArgumentException] {
        # The original launcher has already exited.
    } catch [System.InvalidOperationException] {
        # The launcher exited between opening the process and reading its image.
    }
    $settings = [System.Diagnostics.ProcessStartInfo]::new()
    $settings.UseShellExecute = $false
    $settings.CreateNoWindow = $true
    $settings.FileName = $env:YAGA_UPDATE_TOOL
    $settings.Arguments = $env:YAGA_UPDATE_ARGUMENTS
    $settings.WorkingDirectory = $env:YAGA_UPDATE_ROOT
    $update = [System.Diagnostics.Process]::Start($settings)
    if (-not $update.WaitForExit(600000)) {
        $update.Kill()
        throw 'manager timeout'
    }
    $code = $update.ExitCode
} catch {
    [Console]::Error.WriteLine('The installation manager could not complete the YAGA update.')
    $code = 2
}
[Console]::WriteLine("YAGA_UPDATE_EXIT_CODE=$code")
exit $code
