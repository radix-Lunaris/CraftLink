param(
    [string]$Target = "mc.example.com",
    [int]$Port = 443
)

$ErrorActionPreference = "Continue"

Write-Host "=== TCP connect ==="
$client = New-Object System.Net.Sockets.TcpClient
try {
    $client.Connect($Target, $Port)
    Write-Host "  connected: $($client.Connected)"
} catch {
    Write-Host "  connect failed: $($_.Exception.Message)"
    exit 1
}

$stream = $client.GetStream()
$stream.ReadTimeout = 15000
$stream.WriteTimeout = 15000

# ---- send a TLS ClientHello by hand ----
#
# .NET's SslStream would tell us "the connection was closed", which is what we
# already know. Writing the ClientHello ourselves and reading the raw reply
# distinguishes the three possibilities that matter:
#   * no reply at all          -> nginx/OpenResty is not accepting TLS here
#   * a TLS Alert              -> the certificate or cipher config is broken
#   * a ServerHello            -> TLS is fine and the problem is upstream
$host_bytes = [System.Text.Encoding]::ASCII.GetBytes($Target)

# Minimal TLS 1.2 ClientHello, no extensions except SNI.
$sni = @()
$sni += 0x00, 0x00              # server_name type
$name_len = $host_bytes.Length
$entry_len = $name_len + 3
$sni += [byte](($entry_len -shr 8) -band 0xFF), [byte]($entry_len -band 0xFF)
$sni += [byte](($name_len -shr 8) -band 0xFF), [byte]($name_len -band 0xFF)
$sni += $host_bytes

$ext = @()
$ext += 0x00, 0x00              # extension type: server_name
$ext += [byte](($sni.Length -shr 8) -band 0xFF), [byte]($sni.Length -band 0xFF)
$ext += $sni

$ext_block = @()
$ext_block += [byte](($ext.Length -shr 8) -band 0xFF), [byte]($ext.Length -band 0xFF)
$ext_block += $ext

# cipher suites: TLS_RSA_WITH_AES_128_CBC_SHA + TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
$ciphers = @(0x00, 0x2F, 0xC0, 0x2F)
$cipher_block = @()
$cipher_block += [byte](($ciphers.Length -shr 8) -band 0xFF), [byte]($ciphers.Length -band 0xFF)
$cipher_block += $ciphers

$body = @()
$body += 0x03, 0x03            # client version TLS 1.2
$body += (1..32 | ForEach-Object { Get-Random -Minimum 0 -Maximum 256 })  # random
$body += 0x00                  # session id length
$body += $cipher_block
$body += 0x01, 0x00            # compression: null
$body += $ext_block

$handshake = @()
$handshake += 0x01             # ClientHello
$len = $body.Length
$handshake += [byte](($len -shr 16) -band 0xFF), [byte](($len -shr 8) -band 0xFF), [byte]($len -band 0xFF)
$handshake += $body

$record = @()
$record += 0x16, 0x03, 0x01    # handshake, TLS 1.0 record
$record += [byte](($handshake.Length -shr 8) -band 0xFF), [byte]($handshake.Length -band 0xFF)
$record += $handshake

$bytes = [byte[]]$record

Write-Host "=== sending ClientHello ($($bytes.Length) bytes) for SNI '$Target' ==="

try {
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
} catch {
    Write-Host "  write failed: $($_.Exception.Message)"
    exit 1
}

Write-Host "=== waiting for reply ==="

$buffer = New-Object byte[] 4096

try {
    $read = $stream.Read($buffer, 0, $buffer.Length)

    if ($read -le 0) {
        Write-Host "  server closed the connection without replying"
    } else {
        Write-Host "  got $read bytes"

        $record_type = $buffer[0]
        $ver_major = $buffer[1]
        $ver_minor = $buffer[2]

        $type_name = switch ($record_type) {
            0x16 { "Handshake" }
            0x15 { "Alert" }
            0x17 { "ApplicationData" }
            default { "Unknown(0x{0:X2})" -f $record_type }
        }

        Write-Host "  record type : $type_name"
        Write-Host "  record ver  : $ver_major.$ver_minor"

        if ($record_type -eq 0x15 -and $read -ge 7) {
            $level = $buffer[5]
            $desc = $buffer[6]
            $desc_name = switch ($desc) {
                40 { "handshake_failure" }
                42 { "bad_certificate" }
                46 { "certificate_unknown" }
                48 { "unknown_ca" }
                70 { "protocol_version" }
                80 { "internal_error" }
                112 { "unrecognized_name (SNI!)" }
                default { "code $desc" }
            }
            Write-Host "  ALERT level=$level desc=$desc_name" -ForegroundColor Yellow
        }

        if ($record_type -eq 0x16) {
            Write-Host "  -> TLS is working; the problem is upstream (nginx -> mc-panel)" -ForegroundColor Green
        }

        $hex = ($buffer[0..([Math]::Min(31, $read - 1))] | ForEach-Object { "{0:X2}" -f $_ }) -join " "
        Write-Host "  first bytes : $hex"
    }
} catch {
    Write-Host "  read failed: $($_.Exception.Message)" -ForegroundColor Red
}

$stream.Close()
$client.Close()
