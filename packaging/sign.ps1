# Authenticode signing for File Cleaner (called by build.py and by Inno Setup for setup/uninstaller).
# Certificate: $env:FC_PFX (+ $env:FC_PASSWORD) or $env:FC_THUMB (certificate in CurrentUser\My).
# Timestamp server: $env:FC_TIMESTAMP (empty = no timestamp, e.g. for a local test).
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]] $Files)
$ErrorActionPreference = 'Stop'

if ($env:FC_PFX) {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:FC_PFX, $env:FC_PASSWORD)
} elseif ($env:FC_THUMB) {
    $cert = Get-Item ('Cert:\CurrentUser\My\' + $env:FC_THUMB)
} else {
    throw 'No certificate: set FC_PFX (+ FC_PASSWORD) or FC_THUMB.'
}
if (-not $cert.HasPrivateKey) { throw 'The certificate has no private key - it cannot sign.' }

foreach ($file in $Files) {
    $params = @{ FilePath = $file; Certificate = $cert; HashAlgorithm = 'SHA256' }
    if ($env:FC_TIMESTAMP) { $params.TimestampServer = $env:FC_TIMESTAMP }
    $result = Set-AuthenticodeSignature @params
    if (-not $result.SignerCertificate) { throw "Not signed: $file - $($result.StatusMessage)" }
    Write-Output "Signed: $file"
}
