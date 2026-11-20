# Veröffentlicht die aktuelle Version öffentlich auf GitHub (RyvenDE/RyvenPanel):
# baut Panel + Installer, spiegelt den Quellcode in das öffentliche Repo, setzt den Tag v<Version>
# und legt ein Release mit dem Installer als Download an.
#
#   powershell -ExecutionPolicy Bypass -File publish-github.ps1 -Notes "Was ist neu"
#   (nur Text/README geändert:  ... -Message "README: Hinweis ergänzt")
#
# Anmeldung: nutzt die in Git gespeicherte GitHub-Anmeldung (Git Credential Manager).
param([string]$Notes = "", [string]$Message = "")
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = $PSScriptRoot
$repo = 'RyvenDE/RyvenPanel'
$pub = Join-Path (Split-Path (Split-Path $root)) 'RyvenPanel-GitHub'
$version = [regex]::Match((Get-Content (Join-Path $root 'src\Version.cs') -Raw), 'Version = "([^"]+)"').Groups[1].Value
$tag = "v$version"

function RunGit { & git.exe @args; if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') ist fehlgeschlagen" } }

# 1. bauen
& (Join-Path $root 'build.ps1')
$setup = Join-Path $root "dist\RyvenPanel-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "Installer fehlt: $setup" }

# 2. GitHub-Anmeldung aus dem Credential Manager
$q = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($q, "protocol=https`nhost=github.com`n`n", (New-Object Text.UTF8Encoding $false))   # Windows PowerShell would add a BOM to a pipe
$cred = cmd /c "git credential fill < `"$q`""
Remove-Item $q
$token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
if (-not $token) { throw "Keine GitHub-Anmeldung gefunden – einmal 'git push' in einem Repo ausführen und anmelden." }
$h = @{ Authorization = "token $token"; Accept = 'application/vnd.github+json'; 'User-Agent' = 'RyvenPanel-Publish' }

# 3. öffentliches Repo anlegen, falls es noch nicht existiert
try { Invoke-RestMethod "https://api.github.com/repos/$repo" -Headers $h | Out-Null }
catch {
    Write-Host "Lege öffentliches Repo $repo an …"
    $body = @{ name = ($repo -split '/')[1]; description = 'Kostenloses Windows-Control-Panel für Minecraft-Netzwerke mit Crafty Controller – Server, Spieler, Ränge, Wirtschaft, Team-Zugang.'; private = $false; has_wiki = $false } | ConvertTo-Json
    Invoke-RestMethod 'https://api.github.com/user/repos' -Method Post -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ContentType 'application/json; charset=utf-8' | Out-Null
}

# 4. Quellcode spiegeln (nur was öffentlich gehört)
if (-not (Test-Path (Join-Path $pub '.git'))) { RunGit clone -q "https://github.com/$repo.git" $pub }
# same author as in the private repo (GitHub no-reply address)
foreach ($k in 'user.name', 'user.email') {
    if (-not (git.exe -C $pub config $k)) { $v = git.exe -C $root config $k; if ($v) { RunGit -C $pub config $k $v } }
}
RunGit -C $pub config core.autocrlf false
foreach ($d in 'src', 'setup', 'gateway', 'docs') {
    robocopy (Join-Path $root $d) (Join-Path $pub $d) /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Kopieren fehlgeschlagen ($d)" }
}
foreach ($f in 'build.ps1', 'publish-github.ps1', 'README.md', 'LICENSE') { Copy-Item (Join-Path $root $f) $pub -Force }
Set-Content (Join-Path $pub '.gitignore') "# Build-Ergebnisse (Installer gibt es unter Releases)`r`n*.exe`r`n*.old`r`n*.new`r`ndist/`r`nobj/`r`nbin/`r`n__pycache__/`r`n" -Encoding ASCII

# Sicherheitsnetz: nichts Privates veröffentlichen
$leaks = Get-ChildItem $pub -Recurse -File | Where-Object { $_.FullName -notlike '*\.git\*' } |
    Select-String -Pattern '5\.175\.221\.133', 'BEGIN [A-Z ]*PRIVATE KEY', 'ghp_[A-Za-z0-9]{20}', 'crafty_token"\s*:\s*"[^"]'
if ($leaks) { $leaks | ForEach-Object { Write-Host "  $($_.Path):$($_.LineNumber)" }; throw "Abgebrochen: private Daten im öffentlichen Code gefunden." }

# 5. Commit, Tag, Push
Push-Location $pub
try {
    if (-not (Get-ChildItem '.git\refs\heads' -ErrorAction SilentlyContinue)) { RunGit symbolic-ref HEAD refs/heads/main }
    RunGit add -A
    if (git status --porcelain) { RunGit commit -q -m $(if ($Message) { $Message } else { "RyvenPanel $version" }) }
    if (-not (git tag -l $tag)) { RunGit tag $tag }
    RunGit push -q origin HEAD:main
    RunGit push -q origin $tag
}
finally { Pop-Location }

# 6. Release mit Installer
$rel = $null
try { $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers $h } catch { }
if (-not $rel) {
    if (-not $Notes) { $Notes = "Neue Version $version." }
    $warn = "> [!WARNING]`n> **Speziell für das RyvenMC-Netzwerk angepasst – kein Plug-and-Play-Panel.**`n> - **KVM-Server mit Root/SSH ist Pflicht** – auf Minecraft-Hostern oder Webspace funktioniert es nicht.`n> - **Docker** wird vorausgesetzt (Crafty Controller + MariaDB/MySQL als Container).`n> - **Eigene Plugins** sind nötig, um auf die Daten zuzugreifen (z. B. RyvenEco, RyvenLink, LuckPerms mit MySQL).`n>`n> Details: [README](https://github.com/RyvenDE/RyvenPanel#readme)`n`n"
    $text = $warn + "$Notes`n`n**Installation:** ``RyvenPanel-Setup-$version.exe`` herunterladen und starten – keine Adminrechte nötig.`nBereits installiert? Das Panel bietet Updates automatisch an, sobald der Owner sie verteilt."
    $body = @{ tag_name = $tag; name = "RyvenPanel $version"; body = $text; make_latest = 'true' } | ConvertTo-Json
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases" -Method Post -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ContentType 'application/json; charset=utf-8'
}
$name = Split-Path $setup -Leaf
if (-not ($rel.assets | Where-Object { $_.name -eq $name })) {
    Invoke-RestMethod "https://uploads.github.com/repos/$repo/releases/$($rel.id)/assets?name=$name" -Method Post -Headers $h -InFile $setup -ContentType 'application/octet-stream' | Out-Null
}
Write-Host "OK -> $($rel.html_url)"
