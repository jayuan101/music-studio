$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.OpenFileDialog
$picker.Title = 'Choose the downloaded Rust Windows apps ZIP'
$picker.Filter = 'ZIP archives (*.zip)|*.zip'
$picker.InitialDirectory = 'D:\'
try {
    if ($picker.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        & (Join-Path $PSScriptRoot 'import-build.ps1') -Archive $picker.FileName
    } else { Write-Output 'No archive selected. Nothing installed.' }
} finally { $picker.Dispose() }
