# 从 TPS 本地数据库（SQL Server LocalDB）读取最近几杆的球杆
# 输出 JSON 到 -Out 指定的文件（UTF-8），供 Python 解析
#
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File tps_club.ps1 -Take 10 -Out C:\tmp\x.json
param(
    [int]$Take = 10,
    [string]$Out = ''
)

$ErrorActionPreference = 'Stop'

function Emit($obj) {
    $json = ConvertTo-Json -InputObject $obj -Depth 4 -Compress
    if ([string]::IsNullOrEmpty($json)) { $json = '[]' }
    if ($Out) {
        [IO.File]::WriteAllText($Out, $json, [Text.Encoding]::UTF8)
    } else {
        [Console]::Out.Write($json)
    }
}

try {
    $cs = "Server=(localdb)\TRACKMAN;Initial Catalog=TrackMan Performance Studio;Integrated Security=true;Connect Timeout=4"
    $conn = New-Object System.Data.SqlClient.SqlConnection($cs)
    $conn.Open()
    $cmd = $conn.CreateCommand()
    $cmd.CommandTimeout = 4
    # Data 是 30KB 的 XML，只取最近几条；只用它拿 MeasurementId 与球杆显示名
    $cmd.CommandText = "SELECT TOP ($Take) ClubType, DateCreated, CAST(Data AS nvarchar(max)) AS D FROM Strokes ORDER BY DateCreated DESC"
    $r = $cmd.ExecuteReader()
    $out = New-Object System.Collections.ArrayList
    while ($r.Read()) {
        $xml = $r["D"]
        $mid = ''; $mname = ''
        if ($xml) {
            $m = [regex]::Match($xml, '<(?:[A-Za-z0-9]+:)?MeasurementId>([^<]+)</')
            if ($m.Success) { $mid = $m.Groups[1].Value }
            $m2 = [regex]::Match($xml, '<(?:[A-Za-z0-9]+:)?MeasuredClubData.*?<(?:[A-Za-z0-9]+:)?Name>([^<]+)</(?:[A-Za-z0-9]+:)?Name>')
            if ($m2.Success) { $mname = $m2.Groups[1].Value }
        }
        [void]$out.Add([pscustomobject]@{
            measurementId = $mid
            clubName      = $mname
            clubType      = [string]$r["ClubType"]
            createdAt     = $r["DateCreated"].ToString('o')
        })
    }
    $r.Close()
    $conn.Close()
    Emit @($out)
} catch {
    Emit @([pscustomobject]@{ error = $_.Exception.Message })
}
