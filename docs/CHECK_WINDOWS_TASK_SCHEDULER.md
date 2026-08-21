# Checking Windows Task Scheduler for references to repo `.ps1` files

Purpose: several legacy versioned `.ps1` runners/installers in this repo have **zero internal
code/doc callers** as far as static repo analysis can prove, but a Windows Task Scheduler job on the
production machine could still be invoking one by its literal file path — something a repo-only audit
cannot see. **Do not delete any file in the "REVIEW_EXTERNAL_SCHEDULER" list until an operator runs the
commands below on the actual production machine and confirms no scheduled task references it.**

This file only provides read-only inspection commands. None of them modify, disable, or delete anything.

## Verification result (2026-08-21, production Windows machine)

An operator ran the checks below on the actual production machine:

- Filtered search for `run_daily`, `run_daily_pnl`, `run_daily_inventory`, `install_ceo` → **zero results**.
- Broader search for `ETL_production_v3_exact_codes`, `powershell`, `pwsh`, `python`, `cmd.exe`, `.bat`,
  `.cmd` → returned only standard Windows system tasks (`Microsoft\Windows\Hotpatch\Monitoring`,
  `Microsoft\Windows\Workplace Join\Automatic-Device-Join`, `Microsoft\Windows\Workplace Join\Recovery-Check`)
  — **none reference this repository**.

**Conclusion**: Windows Task Scheduler on the production machine does not invoke any of the 15 legacy
`.ps1` files audited in this pass. Combined with a final repo-wide reference scan (`REFACTOR_LATER.md` §2),
those 15 files — plus 3 newly-orphaned Python dependencies and 2 already-applied historical patchers — were
deleted 2026-08-21. See `CHANGELOG.md` and `docs/PRODUCTION_CLEANUP_MANIFEST.md` for the full list.

## 1. List every scheduled task and its action (command line)

```powershell
Get-ScheduledTask | ForEach-Object {
    $task = $_
    $task.Actions | ForEach-Object {
        [PSCustomObject]@{
            TaskName = $task.TaskName
            TaskPath = $task.TaskPath
            State    = $task.State
            Execute  = $_.Execute
            Arguments = $_.Arguments
            WorkingDirectory = $_.WorkingDirectory
        }
    }
} | Format-Table -AutoSize
```

## 2. Search specifically for this repo's runner/installer filenames

Run this after #1, or standalone — it filters to just the actions whose `Execute` or `Arguments` field
contains one of the filename patterns this cleanup pass is asking about:

```powershell
$patterns = @("run_daily", "run_daily_pnl", "run_daily_inventory", "install_ceo")

Get-ScheduledTask | ForEach-Object {
    $task = $_
    $task.Actions | ForEach-Object {
        $hit = $patterns | Where-Object {
            ($_.Execute -and $task.Actions.Execute -match $_) -or
            ($_.Arguments -and $_.Arguments -match $_)
        }
        if ($hit) {
            [PSCustomObject]@{
                TaskName  = $task.TaskName
                TaskPath  = $task.TaskPath
                State     = $task.State
                Execute   = $_.Execute
                Arguments = $_.Arguments
            }
        }
    }
} | Format-Table -AutoSize
```

If the above doesn't filter cleanly on your PowerShell version, use this simpler two-step form instead:

```powershell
$all = Get-ScheduledTask | ForEach-Object {
    $t = $_
    $t.Actions | Select-Object @{n="TaskName";e={$t.TaskName}}, @{n="TaskPath";e={$t.TaskPath}},
        @{n="State";e={$t.State}}, Execute, Arguments
}
$all | Where-Object {
    $_.Execute -match "run_daily|run_daily_pnl|run_daily_inventory|install_ceo" -or
    $_.Arguments -match "run_daily|run_daily_pnl|run_daily_inventory|install_ceo"
} | Format-Table -AutoSize
```

## 3. Check for each exact filename individually

For a definitive per-file answer, run one check per candidate filename (copy/paste as needed — this list
matches the REVIEW_EXTERNAL_SCHEDULER table from this cleanup pass):

```powershell
$candidates = @(
    "run_daily_pnl_v4.ps1",
    "run_daily_pnl_v4_6.ps1",
    "run_daily_pnl_v4_6_1.ps1",
    "run_daily_pnl_v4_7.ps1",
    "run_daily_pnl_v4_7_1.ps1",
    "run_daily_pnl_v4_7_2.ps1",
    "run_daily_pnl_v4_7_3.ps1",
    "run_daily_inventory_and_ceo_v4_7.ps1",
    "run_daily_inventory_and_ceo_v4_7_1.ps1",
    "run_daily_inventory_and_ceo_v4_7_2.ps1",
    "run_daily_inventory_and_ceo_v4_7_3.ps1",
    "install_ceo_daily_pnl_v4_6.ps1",
    "install_ceo_daily_pnl_v4_7.ps1",
    "install_ceo_daily_pnl_v4_7_1.ps1",
    "install_ceo_daily_pnl_v4_7_2.ps1"
)

$all = Get-ScheduledTask | ForEach-Object {
    $t = $_
    $t.Actions | Select-Object @{n="TaskName";e={$t.TaskName}}, @{n="TaskPath";e={$t.TaskPath}},
        @{n="State";e={$t.State}}, Execute, Arguments
}

foreach ($name in $candidates) {
    $matches = $all | Where-Object { $_.Execute -match [regex]::Escape($name) -or $_.Arguments -match [regex]::Escape($name) }
    if ($matches) {
        Write-Host "REFERENCED: $name" -ForegroundColor Yellow
        $matches | Format-Table -AutoSize
    } else {
        Write-Host "no scheduled task references: $name" -ForegroundColor Green
    }
}
```

## 4. Also check the legacy Windows `schtasks` CLI (belt-and-suspenders, some environments restrict `Get-ScheduledTask`)

```powershell
schtasks /query /fo LIST /v | Select-String -Pattern "run_daily|run_daily_pnl|run_daily_inventory|install_ceo" -Context 5,0
```

## 5. What to do with the results

- **No match found** for a filename across all four commands above → safe to treat as confirmed
  unreferenced by Task Scheduler on this machine. Still verify no *other* automation (CI, a different
  machine, a manual desktop shortcut, Windows startup folder) points at it before deleting.
- **Match found** → do not delete that file. Move it from `DELETE_CANDIDATE`/`REVIEW_EXTERNAL_SCHEDULER`
  to `KEEP`, and treat it as a live production entrypoint going forward — update `CLAUDE.md`'s
  "Current production entry points" section accordingly.
- Report results back per-filename; do not summarize as a single yes/no for the whole batch, since some
  legacy runners may be scheduled while others are not.
