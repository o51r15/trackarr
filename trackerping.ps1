# =============================================================================
# TrackerPing - downloads, pings, and injects trackers into qBittorrent
# =============================================================================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ScriptDir "homelab-config.json"

function Write-Log {
    # Safe in main script body only. Write-Output goes to stdout (bridge streams this).
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts] [$Level] $Message"
    Write-Output $line
    if ($global:LogFile) { Add-Content -Path $global:LogFile -Value $line -ErrorAction SilentlyContinue }
}
function Write-Trace {
    # Safe inside utility functions whose return values are captured.
    # File-only: no Write-Output, so nothing bleeds into the caller's return value.
    param([string]$Message, [string]$Level = "INFO")
    if ($global:LogFile) {
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -Path $global:LogFile -Value "[$ts] [$Level] $Message" -ErrorAction SilentlyContinue
    }
}
function Exit-Failure { param([string]$Message); Write-Log $Message "ERROR"; Start-Sleep -Seconds 2; exit 1 }

function Get-GitHubRepoTrackers($repoUrl, $token, $cacheFile) {
    $repoPath = $repoUrl -replace 'https://github\.com/', '' -replace '\.git$', '' -replace '/$', ''
    if ($repoPath -notmatch '^[^/]+/[^/]+$') { Write-Trace "GitHub: Invalid repo URL: $repoUrl" "WARN"; return @() }
    $headers = @{ 'User-Agent' = 'Trackarr-TrackerPing/1.0' }
    if ($token) { $headers['Authorization'] = "token $token" }
    $cache = @{}
    if (Test-Path $cacheFile) {
        try { $cd = Get-Content $cacheFile -Raw | ConvertFrom-Json; if ($cd -and $cd.PSObject.Properties["repos"]) { $cd.repos.PSObject.Properties | ForEach-Object { $cache[$_.Name] = $_.Value } } } catch {}
    }
    $latestSha = $null
    try {
        $commitResp = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoPath/commits/HEAD" -Headers $headers -UseBasicParsing -ErrorAction Stop -TimeoutSec 10
        $latestSha = $commitResp.sha
    } catch {
        $sc = $_.Exception.Response.StatusCode.Value__
        if ($sc -in @(403,429)) { Write-Trace "GitHub: Rate limit hit for $repoPath. Add a GitHub token." "WARN" } else { Write-Trace "GitHub: API unreachable for $repoPath - $_" "WARN" }
        if ($cache[$repoPath] -and $cache[$repoPath].files) { Write-Trace "GitHub: Using cached file list for $repoPath"; $txtFiles = @($cache[$repoPath].files) } else { return @() }
    }
    if ($latestSha) {
        if ($cache[$repoPath] -and $cache[$repoPath].commitSha -eq $latestSha) {
            Write-Trace "GitHub: $repoPath (cache hit SHA:$($latestSha.Substring(0,8)))"; $txtFiles = @($cache[$repoPath].files)
        } else {
            try {
                $treeResp = Invoke-RestMethod -Uri "https://api.github.com/repos/$repoPath/git/trees/HEAD?recursive=1" -Headers $headers -UseBasicParsing -ErrorAction Stop -TimeoutSec 15
                if ($treeResp.truncated) { Write-Trace "GitHub: Tree truncated for $repoPath" "WARN" }
                $txtFiles = @($treeResp.tree | Where-Object { $_.type -eq 'blob' -and $_.path -match '\.txt$' } | Select-Object -First 20 | ForEach-Object { "https://raw.githubusercontent.com/$repoPath/HEAD/$($_.path)" })
                Write-Trace "GitHub: $repoPath (fetched tree, $($txtFiles.Count) .txt files, SHA:$($latestSha.Substring(0,8)))"
                $cache[$repoPath] = [PSCustomObject]@{ commitSha=$latestSha; checkedAt=(Get-Date).ToString('o'); files=$txtFiles }
                $reposObj = [PSCustomObject]@{}
                foreach ($k in $cache.Keys) { $reposObj | Add-Member -NotePropertyName $k -NotePropertyValue $cache[$k] -Force }
                [System.IO.File]::WriteAllText($cacheFile, ([PSCustomObject]@{cacheVersion=1;repos=$reposObj}|ConvertTo-Json -Depth 10 -Compress), [System.Text.Encoding]::UTF8)
            } catch { Write-Trace "GitHub: Failed to fetch tree for $repoPath - $_" "WARN"; return @() }
        }
    }
    $trackers = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $vs = '^(https?|udp|wss?)://'
    foreach ($fileUrl in $txtFiles) {
        try {
            $content = Invoke-RestMethod -Uri $fileUrl -Headers $headers -UseBasicParsing -ErrorAction Stop -TimeoutSec 10
            $lines = $content -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
            $tl = @($lines | Where-Object { $_ -match $vs })
            if ($tl.Count -ge 5 -and ($tl.Count / [Math]::Max($lines.Count,1)) -gt 0.5) { foreach ($t in $tl) { [void]$trackers.Add($t) } }
        } catch { Write-Trace "GitHub: Failed to download $fileUrl" "WARN" }
    }
    return @($trackers)
}

function Get-WebsiteTrackers($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
        $content = if ($resp.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp.Content) } else { "$($resp.Content)" }
        $scrapePattern = '(?:udp|https?|wss?)://[a-zA-Z0-9._\-\[\]]+:\d+(?:/[^\s"''<>]*)?'
        $regexMatches = [regex]::Matches($content, $scrapePattern)
        $found = @($regexMatches | ForEach-Object { $_.Value.TrimEnd('/') }) | Sort-Object -Unique
        Write-Trace "Website scrape: $($found.Count) trackers from $url"; return $found
    } catch { Write-Trace "Website scrape failed ($url): $_" "WARN"; return @() }
}

# =============================================================================
# 0. Load config
# =============================================================================
if (!(Test-Path $ConfigFile)) { Exit-Failure "homelab-config.json not found." }
$FullCfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$Cfg = $FullCfg.tp
$ConfigDir = $Cfg.dir; $qbt_url = $Cfg.url; $qbt_user = $Cfg.user; $docker_net = $Cfg.dockerNet
$global:LogFile = Join-Path $ConfigDir "trackerping.log"
if ((Test-Path $global:LogFile) -and (Get-Item $global:LogFile).Length -gt 2MB) {
    Move-Item -Path $global:LogFile -Destination ($global:LogFile -replace '\.log$',("_"+(Get-Date -Format 'yyyyMMdd_HHmmss')+".log")) -Force
}
try { $secPass = ConvertTo-SecureString $Cfg.pass; $qbt_pass = [System.Net.NetworkCredential]::new("",$secPass).Password }
catch { Exit-Failure "Failed to decrypt password." }
if (!(Test-Path $ConfigDir)) { New-Item -ItemType Directory -Path $ConfigDir | Out-Null }

$TrackerDataDir = Join-Path $ScriptDir "tracker-data"
$SourcesFile    = Join-Path $TrackerDataDir "tracker-sources.json"
$CacheFile      = Join-Path $TrackerDataDir "tracker-source-cache.json"
$RawFile        = Join-Path $ConfigDir "combined_raw.txt"
$ActiveFile     = Join-Path $ConfigDir "active_raw.txt"
$SleepFile      = Join-Path $TrackerDataDir "tracker-sleep.json"
if (-not (Test-Path $TrackerDataDir)) { New-Item -ItemType Directory -Path $TrackerDataDir | Out-Null }

$PingMode  = if ($Cfg.pingMode  -and $Cfg.pingMode  -ne '') { $Cfg.pingMode  } else { 'docker-vpn' }
$ProxyUrl  = if ($Cfg.proxyUrl)  { $Cfg.proxyUrl  } else { '' }
$PingImage = if ($Cfg.pingImage -and $Cfg.pingImage -ne '') { $Cfg.pingImage } else { 'ghcr.io/o51r15/trackarr:latest' }

$TrackerSources = $null
if (Test-Path $SourcesFile) { try { $TrackerSources = Get-Content $SourcesFile -Raw | ConvertFrom-Json } catch { Write-Log "Could not read tracker-sources.json: $_" "WARN" } }
$GithubToken = ""
if ($TrackerSources -and ![string]::IsNullOrWhiteSpace($TrackerSources.githubToken)) {
    try { $GithubToken = [System.Net.NetworkCredential]::new("",(ConvertTo-SecureString $TrackerSources.githubToken)).Password } catch { Write-Log "Could not decrypt GitHub token." "WARN" }
}

# =============================================================================
# Security check (docker-vpn mode only)
# =============================================================================
if ($PingMode -eq 'docker-vpn') {
    Write-Log "Verifying connection security (ping mode: docker-vpn)..."
    try { $hostIp = (Invoke-RestMethod "https://api.ipify.org" -UseBasicParsing -TimeoutSec 10).Trim() } catch { Exit-Failure "Could not determine Host IP." }
    $containerIpRaw = & docker run --rm --network=$docker_net alpine wget --timeout=10 -qO- https://api.ipify.org 2>$null
    $containerIp    = if ($containerIpRaw) { "$containerIpRaw".Trim() } else { "" }
    if ([string]::IsNullOrWhiteSpace($containerIp)) { Exit-Failure "Could not determine container IP. Is '$docker_net' running?" }
    elseif ($hostIp -eq $containerIp) { Write-Log "CRITICAL: Container IP matches Host IP! Traffic is NOT routed through VPN." "ERROR"; Exit-Failure "Aborting - VPN routing not confirmed." }
    else { Write-Log "Connection SECURE. Container: $containerIp (Hidden Host: $hostIp)" "INFO" }
} else {
    Write-Log "Ping mode: $PingMode. VPN security check skipped."
}

# =============================================================================
# 1. Collect trackers
# =============================================================================
Write-Log "=== Collecting trackers from all sources ==="
$AllTrackers  = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$ValidSchemes = '^(https?|udp|wss?)://'

$UrlFile = Join-Path $ScriptDir "tracker_urls.txt"
if (-not (Test-Path $UrlFile)) { @("https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt") | Out-File $UrlFile -Encoding UTF8 }
$ListURLs = @(Get-Content $UrlFile | Where-Object { $_.Trim() -ne "" -and $_ -notmatch '^\s*#' })
Write-Log "Step 1a: Downloading $($ListURLs.Count) raw list URL(s)..."
foreach ($url in $ListURLs) {
    try { $resp = Invoke-RestMethod -Uri $url -UseBasicParsing -ErrorAction Stop -TimeoutSec 15; $lines = $resp -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" -and $_ -match $ValidSchemes }; foreach ($t in $lines) { [void]$AllTrackers.Add($t) }; Write-Log "  $($lines.Count) from: $url" }
    catch { Write-Log "  Failed: $url" "WARN" }
}

$githubRepos = if ($TrackerSources -and $TrackerSources.PSObject.Properties["githubRepos"]) { @($TrackerSources.githubRepos) } else { @() }
if ($githubRepos.Count -gt 0) {
    Write-Log "Step 1b: Fetching $($githubRepos.Count) GitHub repo(s)..."
    $ghBefore = $AllTrackers.Count
    foreach ($repo in $githubRepos) {
        if (-not $repo -or [string]::IsNullOrWhiteSpace($repo.url)) { continue }
        $repoLabel = if ($repo.label) { $repo.label } else { $repo.url }
        $ghTrackers = @(Get-GitHubRepoTrackers $repo.url $GithubToken $CacheFile | Where-Object { $_ -match $ValidSchemes })
        foreach ($t in $ghTrackers) { [void]$AllTrackers.Add($t) }
        Write-Log "  $($ghTrackers.Count) from: $repoLabel"
    }
    Write-Log "  GitHub total: $($AllTrackers.Count - $ghBefore) new unique trackers."
} else { Write-Log "Step 1b: No GitHub repos configured." }

$webScrapes = if ($TrackerSources -and $TrackerSources.PSObject.Properties["websiteScrape"]) { @($TrackerSources.websiteScrape) } else { @() }
if ($webScrapes.Count -gt 0) {
    Write-Log "Step 1c: Scraping $($webScrapes.Count) website(s)..."
    $wbBefore = $AllTrackers.Count
    foreach ($site in $webScrapes) { if (-not $site -or [string]::IsNullOrWhiteSpace($site.url)) { continue }; $st = @(Get-WebsiteTrackers $site.url | Where-Object { $_ -match $ValidSchemes }); foreach ($t in $st) { [void]$AllTrackers.Add($t) } }
    Write-Log "  Website total: $($AllTrackers.Count - $wbBefore) new unique trackers."
} else { Write-Log "Step 1c: No website scrape sources configured." }

$manualTrackers = if ($TrackerSources -and $TrackerSources.PSObject.Properties["manual"]) { @($TrackerSources.manual) } else { @() }
if ($manualTrackers.Count -gt 0) {
    $manBefore = $AllTrackers.Count; $vm = @($manualTrackers | Where-Object { $_ -match $ValidSchemes }); foreach ($t in $vm) { [void]$AllTrackers.Add($t) }
    Write-Log "Step 1d: $($AllTrackers.Count - $manBefore) manual trackers added."
} else { Write-Log "Step 1d: No manual trackers configured." }

$ipv6Pattern = '^(udp|https?|wss?)://\['; $toRemove = @($AllTrackers | Where-Object { $_ -match $ipv6Pattern }); $ipv6Count = $toRemove.Count
$dirty = @($AllTrackers | Where-Object { $_ -match '["\''\\]+$' })
foreach ($d in $dirty) { $clean = $d -replace '["\''\\]+$',''; [void]$AllTrackers.Remove($d); if ($clean -match $ValidSchemes) { [void]$AllTrackers.Add($clean) } }
foreach ($t in $toRemove) { [void]$AllTrackers.Remove($t) }
if ($AllTrackers.Count -eq 0) { Exit-Failure "No trackers collected." }
Write-Log "[OK] Collection complete: $($AllTrackers.Count) unique trackers. IPv6-only filtered: $ipv6Count."
[System.IO.File]::WriteAllLines($RawFile, @($AllTrackers), [System.Text.Encoding]::UTF8)

# =============================================================================
# 1.5. Sleep/hibernate state
# =============================================================================
$sleepState = @{}
if (Test-Path $SleepFile) {
    try {
        $rawSleepBytes = [System.IO.File]::ReadAllBytes($SleepFile)
        $rawSleepStr   = [System.Text.Encoding]::UTF8.GetString($rawSleepBytes).TrimStart([char]0xFEFF)
        $rawSleep = $rawSleepStr | ConvertFrom-Json
        $rawSleep.PSObject.Properties | ForEach-Object { $sleepState[$_.Name] = $_.Value }
    } catch { Write-Log "Could not read tracker-sleep.json: $_" "WARN" }
}

@($sleepState.Keys) | Where-Object { -not $AllTrackers.Contains($_) } | ForEach-Object { $sleepState.Remove($_) }

$now = Get-Date
$sleepingSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($url in @($sleepState.Keys)) {
    $entry = $sleepState[$url]
    if ($entry.state -in @("sleep","hibernate") -and $entry.sleepUntil) {
        try { if ([datetime]::Parse($entry.sleepUntil) -gt $now) { [void]$sleepingSet.Add($url) } } catch {}
    }
}

$ActiveTrackers = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($t in $AllTrackers) { if (-not $sleepingSet.Contains($t)) { [void]$ActiveTrackers.Add($t) } }

$sleepCount     = @($sleepState.Keys | Where-Object { $sleepState[$_].state -eq "sleep"     -and $sleepingSet.Contains($_) }).Count
$hibernateCount = @($sleepState.Keys | Where-Object { $sleepState[$_].state -eq "hibernate" -and $sleepingSet.Contains($_) }).Count
Write-Log "Active: $($ActiveTrackers.Count) | Sleeping (48h): $sleepCount | Hibernating (7d): $hibernateCount"

function Save-SleepState {
    try {
        $sleepObj = [PSCustomObject]@{}
        foreach ($k in $sleepState.Keys) { $sleepObj | Add-Member -NotePropertyName $k -NotePropertyValue $sleepState[$k] -Force }
        [System.IO.File]::WriteAllText($SleepFile, ($sleepObj | ConvertTo-Json -Depth 5 -Compress), [System.Text.Encoding]::UTF8)
    } catch { Write-Log "Could not save sleep state: $_" "WARN" }
}

if ($ActiveTrackers.Count -eq 0) {
    Write-Log "[OK] All trackers are sleeping or hibernating. Skipping ping - qBittorrent list unchanged."
    Save-SleepState
    Write-Log "SCRIPT_FINISHED_SUCCESSFULLY" "SYSTEM"; exit 0
}

# FIX BUG-01: WriteAllLines uses UTF-8 without BOM (Out-File -Encoding UTF8 adds BOM in PS 5.1)
[System.IO.File]::WriteAllLines($ActiveFile, @($ActiveTrackers), [System.Text.Encoding]::UTF8)
Write-Log "Wrote $($ActiveTrackers.Count) active trackers to active_raw.txt"

# =============================================================================
# 2. Ping via local-trackerping (built from ping/Dockerfile in this repo)
# =============================================================================
$TrackerFile = Join-Path $ConfigDir "working_trackers.txt"
if (Test-Path $TrackerFile) { Remove-Item -Path $TrackerFile -Force }

Write-Log "Starting ping tests (mode: $PingMode)..."
$noUdpFlag = ''
$pingResult = 0

switch ($PingMode) {
    'docker-vpn' {
        docker run --rm --network=$docker_net -v "$($ConfigDir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
        $pingResult = $LASTEXITCODE
    }
    'socks5' {
        Write-Log "Using SOCKS5 proxy: $ProxyUrl" "INFO"
        Write-Log "NOTE: UDP trackers are skipped - SOCKS5 proxies cannot tunnel UDP traffic." "WARN"
        docker run --rm `
            -e ALL_PROXY=$ProxyUrl -e all_proxy=$ProxyUrl `
            -v "$($ConfigDir):/data" `
            $PingImage trackerping -l --no-udp -o /data/working_trackers.txt /data/active_raw.txt
        $pingResult = $LASTEXITCODE
    }
    'https-proxy' {
        Write-Log "Using HTTPS proxy: $ProxyUrl" "INFO"
        Write-Log "NOTE: UDP trackers are skipped - HTTP/HTTPS proxies cannot tunnel UDP traffic." "WARN"
        docker run --rm `
            -e HTTPS_PROXY=$ProxyUrl -e HTTP_PROXY=$ProxyUrl `
            -e https_proxy=$ProxyUrl -e http_proxy=$ProxyUrl `
            -v "$($ConfigDir):/data" `
            $PingImage trackerping -l --no-udp -o /data/working_trackers.txt /data/active_raw.txt
        $pingResult = $LASTEXITCODE
    }
    'direct' {
        Write-Log "Pinging directly (no VPN or proxy)." "INFO"
        docker run --rm `
            -v "$($ConfigDir):/data" `
            $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
        $pingResult = $LASTEXITCODE
    }
    default {
        Write-Log "Unknown pingMode '$PingMode' - falling back to docker-vpn." "WARN"
        docker run --rm --network=$docker_net -v "$($ConfigDir):/data" $PingImage trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
        $pingResult = $LASTEXITCODE
    }
}

Write-Log "Docker finished (exit $pingResult)."
if ($pingResult -ne 0) { Exit-Failure "Ping container exited with code $pingResult." }

# =============================================================================
# 3. Validate surviving trackers
# =============================================================================
if (!(Test-Path $TrackerFile)) { Exit-Failure "Tracker file not generated: $TrackerFile" }
$TrackersRaw = Get-Content -Path $TrackerFile -ErrorAction Stop | Where-Object { $_ -match $ValidSchemes }
if ($TrackersRaw.Count -eq 0) { Exit-Failure "No valid trackers survived the ping test." }
Write-Log "$($TrackersRaw.Count) trackers passed validation."
$TrackersString = $TrackersRaw -join "`n"

# =============================================================================
# 3.5. Latency measurement + structured output
# =============================================================================
$latencyTimeout = if ($Cfg.trackerLatencyTimeoutMs) { [int]$Cfg.trackerLatencyTimeoutMs } else { 3000 }
Write-Log "Measuring latency (timeout: ${latencyTimeout}ms)..."
$WorkingSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$TrackersRaw | ForEach-Object { [void]$WorkingSet.Add($_.Trim()) }
$pool = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(1, 20); $pool.Open(); $runspaces = @()
foreach ($tracker in $WorkingSet) {
    $ps = [System.Management.Automation.PowerShell]::Create(); $ps.RunspacePool = $pool
    [void]$ps.AddScript({ param($url,$tms); try { $uri=[System.Uri]$url; $port=if($uri.Port -gt 0 -and $uri.Port -le 65535){$uri.Port}else{80}; $sw=[System.Diagnostics.Stopwatch]::StartNew(); $tcp=[System.Net.Sockets.TcpClient]::new(); $ok=$tcp.BeginConnect($uri.Host,$port,$null,$null).AsyncWaitHandle.WaitOne($tms); $sw.Stop(); try{$tcp.Close()}catch{}; [PSCustomObject]@{url=$url;latency=if($ok){[int]$sw.ElapsedMilliseconds}else{0}} } catch { [PSCustomObject]@{url=$url;latency=0} } }).AddArgument($tracker).AddArgument($latencyTimeout)
    $runspaces += [PSCustomObject]@{ PS=$ps; Handle=$ps.BeginInvoke() }
}
$latencyMap = @{}
foreach ($r in $runspaces) { $res=$r.PS.EndInvoke($r.Handle); if ($res -and $res.url) { $latencyMap[$res.url]=$res.latency }; $r.PS.Dispose() }
$pool.Close()
Write-Log "Latency complete for $($WorkingSet.Count) trackers."

foreach ($t in $ActiveTrackers) {
    if ($WorkingSet.Contains($t)) { $lat=if($latencyMap.ContainsKey($t)){$latencyMap[$t]}else{0}; Write-Output "[TRACKER_RESULT] url=$t status=UP latency=$lat" }
    else { Write-Output "[TRACKER_RESULT] url=$t status=DOWN latency=0" }
}

# =============================================================================
# 3.7. Update sleep state
# =============================================================================
foreach ($t in $ActiveTrackers) {
    $passed = $WorkingSet.Contains($t)
    if ($passed) { if ($sleepState.ContainsKey($t)) { $sleepState.Remove($t) } }
    else {
        $prevFails  = if ($sleepState.ContainsKey($t)) { [int]$sleepState[$t].failures } else { 0 }
        $newFails   = $prevFails + 1
        $newState   = if ($newFails -lt 2) { "watching" } elseif ($newFails -le 5) { "sleep" } else { "hibernate" }
        $sleepHours = if ($newFails -lt 2) { $null } elseif ($newFails -le 5) { 48 } else { 168 }
        $sleepUntil = if ($sleepHours) { $now.AddHours($sleepHours).ToString("o") } else { $null }
        $sleepState[$t] = [PSCustomObject]@{ state=$newState; failures=$newFails; sleepUntil=$sleepUntil; lastFailure=$now.ToString("o") }
    }
}
$ns = @($sleepState.Keys | Where-Object { $sleepState[$_].state -eq "sleep" }).Count
$nh = @($sleepState.Keys | Where-Object { $sleepState[$_].state -eq "hibernate" }).Count
Save-SleepState
Write-Log "Sleep state saved. Watching: $($sleepState.Count-$ns-$nh) | Sleeping: $ns | Hibernating: $nh"

# =============================================================================
# 4-6. qBittorrent auth, inject, verify
# =============================================================================
Write-Log "Logging into qBittorrent at $qbt_url..."
try { $LoginResponse = Invoke-WebRequest -Uri "$qbt_url/api/v2/auth/login" -Method Post -Body @{username=$qbt_user;password=$qbt_pass} -SessionVariable qbtSession -Headers @{Referer=$qbt_url} -UseBasicParsing -ErrorAction Stop }
catch { Exit-Failure "Could not reach qBittorrent: $_" }
$loginStr = if ($LoginResponse.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($LoginResponse.Content) } else { "$($LoginResponse.Content)" }
if ($LoginResponse.StatusCode -notin @(200,204)) { Exit-Failure "qBittorrent login rejected. HTTP $($LoginResponse.StatusCode)" }
Write-Log "Authenticated."

Write-Log "Injecting $($TrackersRaw.Count) trackers..."
try { Invoke-WebRequest -Uri "$qbt_url/api/v2/app/setPreferences" -Method Post -Body @{json=(@{add_trackers_enabled=$true;add_trackers=$TrackersString}|ConvertTo-Json -Compress)} -WebSession $qbtSession -Headers @{Referer=$qbt_url} -UseBasicParsing -ErrorAction Stop | Out-Null }
catch { Exit-Failure "Failed to update qBittorrent preferences: $_" }
Write-Log "Done. $($TrackersRaw.Count) trackers active in qBittorrent."

Write-Log "Verifying..."
try {
    $vr = Invoke-WebRequest -Uri "$qbt_url/api/v2/app/preferences" -Method Get -WebSession $qbtSession -Headers @{Referer=$qbt_url} -UseBasicParsing -ErrorAction Stop
    $vs = if ($vr.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($vr.Content) } else { "$($vr.Content)" }
    $stored = @(($vs|ConvertFrom-Json).add_trackers -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    $storedSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase); $stored | ForEach-Object { [void]$storedSet.Add($_) }
    $missing = @($TrackersRaw | Where-Object { -not $storedSet.Contains($_.Trim()) })
    if ($missing.Count -eq 0) { Write-Log "[OK] Verification PASSED: $($TrackersRaw.Count) trackers confirmed (stored: $($stored.Count))." }
    else { Write-Log "Verification WARNING: $($missing.Count) trackers not found after update." "WARN" }
} catch { Write-Log "Verification skipped: $_" "WARN" }

Start-Sleep -Seconds 1
Write-Log "SCRIPT_FINISHED_SUCCESSFULLY" "SYSTEM"
exit 0
