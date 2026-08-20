<#
run-tests.ps1 - trigger the component test flows through the Langflow run API and
harvest the durable results from extrct-postgres by batch tag.

Two lanes by design: the HTTP response mirrors every Chat Output of a flow (the
convenience lane, printed here); the record of truth is extraction_run in
extrct-postgres, which this script queries by the batch tag it injects into every
node that carries a run_tags field (discovered per flow at call time, so re-wired
flows keep working). Every /run call executes the whole graph fresh - programmatic
runs cannot replay stale cached nodes.

Requires LANGFLOW_API_KEY in deploy\.env - create one in the Langflow UI
(Settings -> Langflow API Keys -> Add New), then add the line:
    LANGFLOW_API_KEY=<the key>

Usage:
  .\scripts\run-tests.ps1                                  # all five tests
  .\scripts\run-tests.ps1 -Flows test-04,test-05           # subset
  .\scripts\run-tests.ps1 -InputFile note.txt -Tag pilot3  # own note + own tag
  .\scripts\run-tests.ps1 -ListOnly                        # show registry, no calls
  .\scripts\run-tests.ps1 -SkipHarvest                     # trigger only

NOTE: the flow registry below is an EXAMPLE. Flow exports are deliberately not shipped
with this repo (flow JSON is never an artifact of record), so replace these ids and
endpoint names with your own flows. A flow's UUID changes whenever it is re-imported;
the endpoint name is set in the flow's settings.
#>
param(
    [string[]]$Flows = @("test-01", "test-02", "test-03", "test-04", "test-05"),
    [string]$InputText = "",
    [string]$InputFile = "",
    [string]$Tag = "",
    [string]$BaseUrl = "http://127.0.0.1:7860",
    [int]$TimeoutSec = 900,
    [switch]$SkipHarvest,
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

# --- flow registry (snapshot-20260810) -------------------------------------------
$FlowRegistry = [ordered]@{
    "test-01" = @{ Id = "29b53b0e-20c3-4129-abc4-814484f10c39"; Endpoint = "test-01-schema-registry"
                   Note = "schema builder + registry; writes schema_variable, so 0 tagged extraction runs is expected" }
    "test-02" = @{ Id = "7fda9f60-413c-4ce1-8374-947da6bf0399"; Endpoint = "test-02-clients-extract"
                   Note = "both clients + structured extract end-to-end" }
    "test-03" = @{ Id = "5811e52b-b42f-48d7-a460-928541392882"; Endpoint = "test-03-logprobs"
                   Note = "logprobs through both providers" }
    "test-04" = @{ Id = "81226e39-80d8-4a36-ad7e-26b9282997c8"; Endpoint = "test-04-grounding"
                   Note = "evidence grounding, inline + posthoc chains" }
    "test-05" = @{ Id = "83f701c9-e630-48cb-9f35-1817c9e5f48d"; Endpoint = "test-05-certainty"
                   Note = "certainty scores from stored logprobs" }
}

if ($ListOnly) {
    foreach ($k in $FlowRegistry.Keys) {
        $m = $FlowRegistry[$k]
        Write-Host ("{0}  {1}  {2}" -f $k, $m.Endpoint, $m.Id)
        Write-Host ("         {0}" -f $m.Note)
    }
    exit 0
}

# --- inputs ----------------------------------------------------------------------
if ($Tag -eq "") { $Tag = "batch-" + (Get-Date -Format "yyyyMMdd-HHmmss") }
if ($Tag -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Tag must contain only letters, digits, dot, dash, underscore (it lands in SQL and jsonb)."
}
if ($InputFile -ne "") { $InputText = Get-Content -Raw -Encoding UTF8 $InputFile }
if ($InputText -eq "") {
    # synthetic note (synthetic / de-identified only)
    $InputText = "Echocardiography performed today. LVEF measured at 55 percent. " +
                 "Mild mitral regurgitation noted. Diagnosis: normal systolic function. " +
                 "Plan: follow-up echo in 12 months."
}

# --- secrets: LANGFLOW_API_KEY and DB names from deploy\.env (values never printed)
$envPath = Join-Path $RepoRoot "deploy\.env"
$envMap = @{}
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*#') { continue }
        $idx = $line.IndexOf("=")
        if ($idx -gt 0) {
            $envMap[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim().Trim('"')
        }
    }
}
$apiKey = $envMap["LANGFLOW_API_KEY"]
if (-not $apiKey) {
    throw ("LANGFLOW_API_KEY not found in deploy\.env. Create one in the Langflow UI " +
           "(Settings -> Langflow API Keys -> Add New) and add the line LANGFLOW_API_KEY=<key>.")
}
$dbUser = "extrct"; if ($envMap["EXTRCT_DB_USER"]) { $dbUser = $envMap["EXTRCT_DB_USER"] }; if ($envMap["EXTRCT_DB_USER"]) { $dbUser = $envMap["EXTRCT_DB_USER"] }
$dbName = "extrct"; if ($envMap["EXTRCT_DB_NAME"]) { $dbName = $envMap["EXTRCT_DB_NAME"] }; if ($envMap["EXTRCT_DB_NAME"]) { $dbName = $envMap["EXTRCT_DB_NAME"] }
$headers = @{ "x-api-key" = $apiKey }

Write-Host ("tag={0}  flows={1}  input={2} chars" -f $Tag, ($Flows -join ","), $InputText.Length)

# --- trigger ---------------------------------------------------------------------
$results = @()
foreach ($name in $Flows) {
    if (-not $FlowRegistry.Contains($name)) {
        Write-Warning "unknown flow '$name' - known: $($FlowRegistry.Keys -join ', ')"
        continue
    }
    $meta = $FlowRegistry[$name]
    Write-Host ""
    Write-Host ("=== {0}  ({1}) ===" -f $name, $meta.Endpoint)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # discover every node with a run_tags field; inject the batch tag via tweaks
        $flowDoc = Invoke-RestMethod -Uri "$BaseUrl/api/v1/flows/$($meta.Id)" -Headers $headers -TimeoutSec 60
        $tweaks = @{}
        foreach ($node in @($flowDoc.data.nodes)) {
            $tpl = $node.data.node.template
            if ($null -ne $tpl -and $null -ne $tpl.PSObject.Properties["run_tags"]) {
                $existing = ""
                if ($null -ne $tpl.run_tags.value) { $existing = ([string]$tpl.run_tags.value).Trim() }
                if ($existing.Length -gt 0) { $merged = $existing + "," + $Tag } else { $merged = $Tag }
                $tweaks[[string]$node.id] = @{ run_tags = $merged }
            }
        }
        $body = @{
            input_value = $InputText
            input_type  = "chat"
            output_type = "chat"
            session_id  = "$Tag-$name"
            user_id     = "run-tests-ps1"      # lands in Langfuse as the trace user
            tweaks      = $tweaks
        } | ConvertTo-Json -Depth 6
        $resp = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/run/$($meta.Endpoint)?stream=false" `
            -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec $TimeoutSec
        $sw.Stop()
        $nOut = 0
        foreach ($outer in @($resp.outputs)) {
            foreach ($comp in @($outer.outputs)) {
                $nOut++
                $label = $comp.component_display_name
                if (-not $label) { $label = $comp.component_id }
                $txt = $null
                if ($comp.results -and $comp.results.message) { $txt = [string]$comp.results.message.text }
                if (-not $txt) { $txt = ($comp.results | ConvertTo-Json -Compress -Depth 4) }
                $txt = ($txt -replace "(\r?\n)+", " | ")
                if ($txt.Length -gt 160) { $txt = $txt.Substring(0, 160) + "..." }
                Write-Host ("  [{0}] {1}" -f $label, $txt)
            }
        }
        $results += [pscustomobject]@{ Flow = $name; Status = "ok"; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
                                       TaggedNodes = $tweaks.Count; Outputs = $nOut }
    }
    catch {
        $sw.Stop()
        $msg = $_.Exception.Message
        if ($_.Exception.Response) {
            try {
                $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $errBody = $sr.ReadToEnd()
                if ($errBody.Length -gt 300) { $errBody = $errBody.Substring(0, 300) + "..." }
                $msg = "$msg :: $errBody"
            } catch {}
        }
        Write-Warning ("{0} FAILED after {1}s: {2}" -f $name, [math]::Round($sw.Elapsed.TotalSeconds, 1), $msg)
        $results += [pscustomobject]@{ Flow = $name; Status = "FAILED"; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
                                       TaggedNodes = 0; Outputs = 0 }
    }
}

# --- summary + harvest (record of truth: extrct-postgres, not the chat mirror) ----
Write-Host ""
$results | Format-Table -AutoSize | Out-String | Write-Host
if (-not $SkipHarvest) {
    Write-Host ("--- v_run_report rows tagged '{0}' ---" -f $Tag)
    $q = "SELECT left(run_uid,12) AS run, final_status, provider, left(model_on_wire,30) AS model, " +
         "attempts, http_status, latency_ms, cost_usd FROM v_run_report WHERE tags ? '$Tag' ORDER BY started_at;"
    & docker compose --project-directory (Join-Path $RepoRoot "deploy") exec -T extrct-postgres `
        psql -U $dbUser -d $dbName -P pager=off -c $q
    Write-Host ("Re-query any time:  ... FROM v_run_report WHERE tags ? '{0}'" -f $Tag)
}
$failed = @($results | Where-Object { $_.Status -eq "FAILED" })
if ($failed.Count -gt 0) { exit 1 }
