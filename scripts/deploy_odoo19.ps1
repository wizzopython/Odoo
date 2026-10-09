[CmdletBinding()]
param([switch]$FilesOnly)

$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$addonName = 'attendance_punching_integration'
$sourceAddon = Join-Path $sourceRoot $addonName
$installRoot = 'C:\Program Files\Odoo 19.0.20261008'
$serverRoot = Join-Path $installRoot 'server'
$pythonExe = Join-Path $installRoot 'python\python.exe'
$configFile = Join-Path $serverRoot 'odoo.conf'
$deployedAddon = Join-Path $serverRoot "odoo\addons\$addonName"
$serviceName = 'odoo-server-19.0'
$databaseName = 'attendance_db'
$logFile = Join-Path $sourceRoot 'odoo19-deployment.log'

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell using Run as administrator, then run this script again. No deployment changes were made.'
}

# Verify the exact service and configured directory before copying anything.
$serviceParameters = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName\Parameters"
if ($serviceParameters.Application -ne $pythonExe -or $serviceParameters.AppParameters -notlike "*$configFile*") {
    throw 'Odoo service configuration changed. Recheck its Python and config paths before deployment.'
}
$checkEnvironment = @'
import ast, configparser, importlib.metadata as metadata
from pathlib import Path
import sys
from zk import ZK
import requests, pytz
c = configparser.RawConfigParser()
c.read(sys.argv[1])
expected = Path(sys.argv[2]).resolve()
paths = [Path(p.strip()).resolve() for p in c['options']['addons_path'].split(',')]
assert expected in paths, 'Configured addons_path changed; recheck deployment target.'
manifest = ast.literal_eval(Path(sys.argv[3]).read_text(encoding='utf-8-sig'))
assert manifest['version'].startswith('19.0.') and manifest['installable']
print('Odoo Python dependencies:', {p: metadata.version(p) for p in ('requests','pytz','pyzk')})
'@
& $pythonExe -c $checkEnvironment $configFile (Split-Path -Parent $deployedAddon) (Join-Path $sourceAddon '__manifest__.py')
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed. Review the error above before proceeding.' }

$backupRoot = Join-Path $env:LOCALAPPDATA ('OdooMigrationBackups\deployment19-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupRoot | Out-Null
Copy-Item -LiteralPath $deployedAddon -Destination (Join-Path $backupRoot 'deployed-addon') -Recurse
Write-Host "Backup folder: $backupRoot"

$stopped = $false
try {
    Stop-Service -Name $serviceName
    $stopped = $true
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))

    $backupDatabase = @'
import configparser, os, shutil, subprocess, sys
from pathlib import Path
c = configparser.RawConfigParser()
c.read(sys.argv[1])
options = c['options']
backup = Path(sys.argv[2])
database = sys.argv[3]
env = dict(os.environ, PGPASSWORD=options['db_password'])
subprocess.run([r'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe',
    '-h', options['db_host'], '-p', options['db_port'], '-U', options['db_user'],
    '-Fc', '-f', str(backup / (database + '.dump')), database], env=env, check=True)
filestore = Path(options['data_dir']) / 'filestore' / database
if filestore.exists():
    shutil.copytree(filestore, backup / 'filestore' / database)
print('Database and available filestore backed up.')
'@
    & $pythonExe -c $backupDatabase $configFile $backupRoot $databaseName
    if ($LASTEXITCODE -ne 0) { throw 'Database backup failed; addon files were not replaced.' }

    # Copy source files only; preserve the prior addon in the backup, skip bytecode.
    Get-ChildItem -LiteralPath $sourceAddon -Recurse -File | Where-Object {
        $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc'
    } | ForEach-Object {
        $relative = $_.FullName.Substring($sourceAddon.Length).TrimStart('\')
        $target = Join-Path $deployedAddon $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        if ((Get-FileHash -LiteralPath $_.FullName).Hash -ne (Get-FileHash -LiteralPath $target).Hash) {
            throw "File verification failed: $relative"
        }
    }

    # Dahua now lives in attendance_punching.py; the prior addon was backed up above.
    $obsoleteDahuaFile = Join-Path $deployedAddon 'models\dahua.py'
    if (Test-Path -LiteralPath $obsoleteDahuaFile -PathType Leaf) {
        Remove-Item -LiteralPath $obsoleteDahuaFile -Force
    }

    if (-not $FilesOnly) {
        # Odoo refreshes the module list, installs dependencies, and installs/upgrades
        # this addon. The web service is stopped and no device cron runs here.
        & $pythonExe (Join-Path $serverRoot 'odoo-bin') -c $configFile -d $databaseName `
            -i $addonName -u $addonName --without-demo --stop-after-init --no-http `
            --max-cron-threads=0 --logfile $logFile
        if ($LASTEXITCODE -ne 0) { throw "Odoo installation failed. Read $logFile; backups are in $backupRoot" }

        $verify = @'
module = env['ir.module.module'].search([('name', '=', 'attendance_punching_integration')])
print('Installed module:', module.name, module.state, module.latest_version)
assert module.state == 'installed' and module.latest_version == '19.0.1.0.12'
assert env['ir.module.module'].search([('name', '=', 'hr_attendance')]).state == 'installed'
options = env['attendance.punching'].fields_get(['device_type'])['device_type']['selection']
assert ('dahua', 'Dahua') in options
print('Device types:', options)
print('Device records:', env['attendance.punching'].search_count([]))
env.cr.rollback()
'@
        $verify | & $pythonExe (Join-Path $serverRoot 'odoo-bin') shell -c $configFile -d $databaseName `
            --no-http --max-cron-threads=0 --logfile $logFile
        if ($LASTEXITCODE -ne 0) { throw "Post-install verification failed. Read $logFile" }
    }
}
finally {
    if ($stopped) {
        Start-Service -Name $serviceName
        (Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
    }
}

if ($FilesOnly) {
    Write-Host 'Files deployed and Odoo restarted. In Apps, Update Apps List, remove the Apps filter, and activate Attendance Punching Integration.'
}
else {
    Write-Host 'Deployment verified and Odoo restarted. Refresh http://localhost:8069 and open Attendances > Configuration > Attendance Punching.'
}
Write-Host "Keep the backup folder: $backupRoot"
