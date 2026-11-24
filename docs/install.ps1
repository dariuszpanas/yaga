# Install the release executable or an explicitly selected Python tool environment.
& {
    $ErrorActionPreference = 'Stop'
    $versionPattern = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'

    function Install-PythonYaga {
        $package = 'yaga-cli'
        if ($version) { $package = "yaga-cli==$version" }
        $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($uvCommand) {
            $uvExecutable = $uvCommand.Source
        } else {
            if (!$HOME) { throw 'Installing uv requires a user profile; set USERPROFILE or install uv on PATH' }
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

    function Get-ReleaseFile([string]$url, [string]$path, [int]$limit) {
        $deadline = [Threading.CancellationTokenSource]::new(120000)
        try {
            for ($redirect = 0; $redirect -le 5; $redirect++) {
                $response = $client.GetAsync($url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $deadline.Token).GetAwaiter().GetResult()
                try {
                    $status = [int]$response.StatusCode
                    if ($status -eq 404) { return $false }
                    if ($status -in 301, 302, 303, 307, 308) {
                        if ($loopback -or !$response.Headers.Location) { throw 'Unexpected release redirect' }
                        $next = [Uri]::new([Uri]$url, $response.Headers.Location)
                        if ($next.Scheme -ne 'https' -or $next.UserInfo) { throw 'Unsafe release redirect' }
                        $url = $next.AbsoluteUri
                        continue
                    }
                    if ($status -ne 200) { throw "Release server returned HTTP $status" }
                    if ($response.Content.Headers.ContentLength -gt $limit) { throw 'Release download exceeds size limit' }
                    $source = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                    $file = [IO.File]::Open($path, 'CreateNew', 'Write', 'None')
                    try {
                        $buffer = [byte[]]::new(65536)
                        while (($count = $source.ReadAsync($buffer, 0, $buffer.Length, $deadline.Token).GetAwaiter().GetResult()) -gt 0) {
                            if ($file.Length + $count -gt $limit) { throw 'Release download exceeds size limit' }
                            $file.Write($buffer, 0, $count)
                        }
                    } finally { $file.Dispose() }
                    return $true
                } finally { $response.Dispose() }
            }
            throw 'Too many release redirects'
        } finally { $deadline.Dispose() }
    }

    function Assert-RegularFile([string]$path) {
        $attrs = [IO.File]::GetAttributes($path)
        if ($attrs -band ([IO.FileAttributes]::ReparsePoint -bor [IO.FileAttributes]::Directory)) {
            throw 'Refusing to replace a linked file or directory'
        }
    }

    $method = $env:YAGA_INSTALL_METHOD
    if (!$method) { $method = 'auto' }
    $version = $env:YAGA_VERSION
    $dir = $env:YAGA_INSTALL_DIR
    for ($i = 0; $i -lt $args.Count; $i++) {
        switch -Regex ([string]$args[$i]) {
            '^--?native$' { $method = 'native' }
            '^--?python$' { $method = 'python' }
            '^--?version$' {
                if (++$i -ge $args.Count -or !$args[$i]) { throw '--version requires X.Y.Z' }
                $version = [string]$args[$i]
            }
            '^(-InstallDir|--install-dir)$' {
                if (++$i -ge $args.Count -or !$args[$i]) { throw '--install-dir requires an absolute path' }
                $dir = [string]$args[$i]
            }
            '^(-h|--?help)$' { 'Usage: install.ps1 [-Native|-Python] [-Version X.Y.Z] [-InstallDir C:\absolute\path]'; return }
            default { throw 'Unknown installer argument; see --help' }
        }
    }
    if ($version -and $version -cnotmatch "\A$versionPattern\z") { throw 'YAGA_VERSION must be a final X.Y.Z release' }
    if ($method -ceq 'python') { Install-PythonYaga; return }
    if ($method -cnotin 'auto', 'native') { throw 'Unknown YAGA_INSTALL_METHOD' }
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use install.sh on Linux and macOS' }
    # The OS architecture can differ from the PowerShell process architecture under emulation.
    try { $machine = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }
    catch { $machine = $env:PROCESSOR_ARCHITEW6432; if (!$machine) { $machine = $env:PROCESSOR_ARCHITECTURE } }
    switch ($machine) {
        { $_ -in 'X64', 'AMD64' } { $arch = 'x86_64' }
        'Arm64' { $arch = 'aarch64' }
        default { throw 'No native release for this architecture; select --python' }
    }
    $asset = "yaga-$arch-pc-windows-msvc.exe"
    $base = $env:YAGA_RELEASE_BASE_URL
    if (!$base) { $base = 'https://github.com/dariuszpanas/yaga/releases' }
    $loopback = $base -cmatch '\Ahttp://127\.0\.0\.1:[0-9]+\z'
    if (!$loopback -and $base -cnotmatch '\Ahttps://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?\z') { throw 'Release URL requires HTTPS or explicit loopback' }
    if (!$dir) {
        if (!$HOME) { throw 'Set --install-dir or YAGA_INSTALL_DIR when a user profile is unavailable' }
        $dir = Join-Path $HOME '.local/bin'
    }
    if ($dir -notmatch '\A([A-Za-z]:[/\\]|\\\\[^\\]+\\[^\\]+)') { throw '--install-dir requires an absolute path' }
    $dir = [IO.Directory]::CreateDirectory($dir).FullName
    $tmp = [IO.Directory]::CreateDirectory((Join-Path $dir ('.yaga-install.' + [Guid]::NewGuid()))).FullName
    $stage = Join-Path $tmp 'yaga.exe'
    $dest = Join-Path $dir 'yaga.exe'
    $receipt = Join-Path $dir '.yaga-native-receipt'
    $stagedReceipt = Join-Path $tmp 'receipt'
    $backup = Join-Path $tmp 'previous-receipt'
    $manifest = Join-Path $tmp 'manifest'
    $lock = Join-Path $dir '.yaga-native-lock'
    $pendingLock = Join-Path $tmp 'lock'
    $locked = $false
    $client = $null
    try {
        Add-Type -AssemblyName System.Net.Http
        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [Net.Http.HttpClient]::new($handler)
        $location = 'latest/download'
        if ($version) { $location = "download/v$version" }
        if (!(Get-ReleaseFile "$base/$location/yaga-native-sha256.txt" $manifest 2048)) {
            if ($method -ceq 'auto' -and !(Test-Path -LiteralPath $receipt)) {
                if (!$version) {
                    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Head, "$base/latest")
                    try {
                        $response = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
                        try {
                            $tag = [string]$response.Headers.Location
                            if ([int]$response.StatusCode -in 301, 302, 303, 307, 308 -and
                                $tag -cmatch ("\A" + [Regex]::Escape($base) + "/tag/v($versionPattern)\z")) {
                                $version = $Matches[1]
                            }
                        } finally { $response.Dispose() }
                    } finally { $request.Dispose() }
                }
                if ($version -cmatch '\A(0\.[0-4]\.[0-9]+|0\.5\.0)\z') { Install-PythonYaga; return }
            }
            throw 'Native release manifest unavailable; for a Python installation select --python'
        }
        $raw = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($manifest))
        $lines = $raw.Split([char]10)
        if ($lines.Count -ne 8 -or $lines[7] -cne '' -or $lines[0] -cnotmatch "\Ayaga-native-v1 ($versionPattern)\z") { throw 'Invalid release manifest' }
        $release = $Matches[1]
        if ($version -and $release -cne $version) { throw 'Release version mismatch' }
        $previous = ''
        foreach ($line in $lines[1..6]) {
            if ($line -cnotmatch '\A([0-9a-f]{64})  (yaga-(x86_64|aarch64)-(unknown-linux-musl|apple-darwin|pc-windows-msvc\.exe))\z') { throw 'Invalid release checksums' }
            $name = $Matches[2]
            if ([string]::CompareOrdinal($previous, $name) -ge 0) { throw 'Invalid release checksum ordering' }
            if ($name -ceq $asset) { $expected = $Matches[1] }
            $previous = $name
        }
        if (!(Get-ReleaseFile "$base/download/v$release/$asset" $stage 134217728)) { throw 'Native binary download failed' }
        $actual = (Get-FileHash -LiteralPath $stage -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -cne $expected) { throw 'Native binary checksum mismatch' }
        $identity = & $stage --version
        if ($LASTEXITCODE -ne 0 -or @($identity).Count -ne 1 -or $identity -cne "yaga $release") { throw 'Native executable version mismatch' }
        # A directory lock is shared with the POSIX installer under Git Bash.
        # Move an empty, private directory into place. Only one contender can
        # rename to the shared name while another installer holds it.
        [void][IO.Directory]::CreateDirectory($pendingLock)
        [IO.Directory]::Move($pendingLock, $lock)
        $locked = $true
        $exists = Test-Path -LiteralPath $dest
        if ($env:YAGA_NATIVE_UPDATE_FROM -and !$exists) { throw 'Native installation changed since self update started' }
        if ($exists -or (Test-Path -LiteralPath $receipt)) {
            if (!$exists -or !(Test-Path -LiteralPath $receipt)) { throw 'Existing executable belongs to another installer' }
            Assert-RegularFile $dest
            Assert-RegularFile $receipt
            if ((Get-Item -LiteralPath $receipt).Length -gt 256) { throw 'Invalid install receipt' }
            $record = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($receipt))
            if ($record -cnotmatch "\Ayaga-native-install-v1 $versionPattern\n([0-9a-f]{64})  yaga\.exe\n\z") { throw 'Invalid install receipt' }
            if ((Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash -ne $Matches[4]) { throw 'Installed executable or receipt has changed' }
            if ($env:YAGA_NATIVE_UPDATE_FROM) {
                if ($env:YAGA_NATIVE_UPDATE_FROM -cne $Matches[4]) { throw 'Native installation changed since self update started' }
                $new = $release.Split('.')
                foreach ($i in 0..2) {
                    $comparison = ([bigint]$new[$i]).CompareTo([bigint]$Matches[($i + 1)])
                    if ($comparison -lt 0) { throw 'Latest release is older than the installed version' }
                    if ($comparison -gt 0) { break }
                }
            }
        }
        [IO.File]::WriteAllText($stagedReceipt, "yaga-native-install-v1 $release`n$actual  yaga.exe`n", [Text.Encoding]::ASCII)
        if ($exists) { [IO.File]::Replace($stagedReceipt, $receipt, $backup) }
        else { [IO.File]::Move($stagedReceipt, $receipt) }
        try {
            if ($exists) { [IO.File]::Replace($stage, $dest, (Join-Path $tmp 'previous.exe')) }
            else { [IO.File]::Move($stage, $dest) }
        } catch {
            [IO.File]::Delete($receipt)
            if ($exists) { [IO.File]::Move($backup, $receipt) }
            throw
        }
        Write-Output "Installed YAGA $release at $dest"
        if ($env:YAGA_NO_MODIFY_PATH -ne '1') {
            try {
                $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                if ($dir -notin ($userPath -split ';')) {
                    [Environment]::SetEnvironmentVariable('Path', ("$dir;$userPath").TrimEnd(';'), 'User')
                    Write-Output 'Open a new terminal to use yaga.'
                }
            } catch {
                [Console]::Error.WriteLine('Could not update your user PATH. Add the installation directory to PATH manually.')
                $quoted = [System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($dir)
                Write-Output "To use yaga in this PowerShell session, run:`n  `$env:Path = '$quoted;' + `$env:Path"
            }
        }
    } finally {
        try {
            if ($client) { $client.Dispose() }
            foreach ($file in $manifest, $stage, $stagedReceipt, $backup, (Join-Path $tmp 'previous.exe')) { [IO.File]::Delete($file) }
            if ([IO.Directory]::Exists($pendingLock)) { [IO.Directory]::Delete($pendingLock) }
            [IO.Directory]::Delete($tmp)
        } finally {
            if ($locked) { [IO.Directory]::Delete($lock) }
        }
    }
} @args
