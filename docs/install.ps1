# Install the PyPI distribution into a per-user uv tool environment.
& {
    $ErrorActionPreference = 'Stop'
    $package = 'yaga-cli'
    if ($env:YAGA_VERSION) {
        if ($env:YAGA_VERSION -cnotmatch '\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z') {
            throw 'YAGA_VERSION must be a final X.Y.Z release'
        }
        $package = "yaga-cli==$env:YAGA_VERSION"
    }
    $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uvCommand) {
        $uvExecutable = $uvCommand.Source
    } else {
        $installer = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName() + '.ps1')
        $previousInstall = $env:UV_UNMANAGED_INSTALL
        try {
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.12.7/install.ps1' -OutFile $installer -TimeoutSec 120
            $env:UV_UNMANAGED_INSTALL = Join-Path $HOME '.local/bin'
            & ([scriptblock]::Create([IO.File]::ReadAllText($installer)))
            $uvExecutable = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
            if (-not (Test-Path -LiteralPath $uvExecutable -PathType Leaf)) {
                throw 'uv installation did not produce an executable'
            }
        } finally {
            $env:UV_UNMANAGED_INSTALL = $previousInstall
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
    }
    & $uvExecutable --no-config tool install --python 3.12 $package
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }
    if ($env:YAGA_NO_MODIFY_PATH -ne '1') {
        & $uvExecutable --no-config tool update-shell
        if ($LASTEXITCODE -ne 0) { throw "uv tool update-shell failed with exit code $LASTEXITCODE" }
    }
    Write-Output 'YAGA installed. Open a new terminal and run yaga --help.'
    Write-Output 'Update: yaga self update. Remove: uv tool uninstall yaga-cli.'
}
