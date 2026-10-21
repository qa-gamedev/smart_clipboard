# Установка языковых пакетов OCR для Windows.
# Скрипт сам запросит права администратора и НЕ закрывается в конце — прочитайте результат и нажмите Enter.
# Свой список языков:  .\install_ocr_languages.ps1 ko-KR ja-JP

param([string[]]$Languages)

function Wait-Exit([int]$code) {
    Write-Host ""
    Read-Host "Нажмите Enter, чтобы закрыть окно"
    exit $code
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Нужны права администратора — открываю новое окно с запросом (нажмите «Да»)..." -ForegroundColor Yellow
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Languages) { $argList += $Languages }
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList
    } catch {
        Write-Host "Запуск от администратора отменён." -ForegroundColor Red
        Wait-Exit 1
    }
    exit 0
}

if (-not $Languages -or $Languages.Count -eq 0) {
    # русский, английский, корейский, японский, китайский, украинский, европейские, арабский
    $Languages = @("ru-RU","en-US","ko-KR","ja-JP","zh-CN","zh-TW","uk-UA","de-DE","fr-FR","es-ES","it-IT","pl-PL","tr-TR","ar-SA")
}

Write-Host "Проверяю доступные пакеты OCR (нужен доступ к Центру обновления Windows)..." -ForegroundColor Cyan
try {
    $available = Get-WindowsCapability -Online -Name "Language.OCR*" -ErrorAction Stop
} catch {
    Write-Host "Не удалось получить список: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Проверьте интернет, службу «Центр обновления Windows» и повторите." -ForegroundColor Yellow
    Wait-Exit 1
}

$ok = 0; $skipped = 0; $failed = 0
foreach ($lang in $Languages) {
    $cap = $available | Where-Object { $_.Name -like "Language.OCR~~~$lang~*" } | Select-Object -First 1
    if (-not $cap) {
        Write-Host "[нет такого пакета]  $lang" -ForegroundColor DarkYellow
        $skipped++
        continue
    }
    if ($cap.State -eq "Installed") {
        Write-Host "[уже установлен]     $lang" -ForegroundColor Gray
        $ok++
        continue
    }
    Write-Host "[устанавливаю]       $lang ..." -ForegroundColor Cyan
    try {
        Add-WindowsCapability -Online -Name $cap.Name -ErrorAction Stop | Out-Null
        Write-Host "[готово]             $lang" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "[ошибка]             $lang : $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "Итог: установлено/есть $ok, не найдено $skipped, ошибок $failed." -ForegroundColor White
if ($failed -gt 0) {
    Write-Host "Ошибки 0x800f0954 или 0x8024... обычно значат, что Windows Update заблокирован (корпоративная сеть/WSUS)." -ForegroundColor Yellow
}
Write-Host "Перезапустите Умный буфер обмена, чтобы он увидел новые языки." -ForegroundColor Cyan
Wait-Exit 0
