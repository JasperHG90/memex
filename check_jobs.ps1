$jobIds = @(
    'c7c3874e-70e1-49de-8160-09ed741d23d4',
    '224eba79-6164-48c9-9e39-7f6b37500e16',
    '7927b05f-bd21-47cc-bce6-b1c5f06a24d6',
    'dc7cdbdb-7547-4be7-b78f-e6e627ed8e80',
    'ed89c103-4051-464d-8bf9-e50fe94d0966',
    'cb00aa71-a188-42fe-97e3-21e96c49d32f',
    '64147127-0c40-433f-97f3-9e88618545b2',
    '9114df38-b729-45bb-9054-e443a6a39c83',
    'e91111bc-85d3-4d72-84dd-47e7ad65e1d4',
    '0835c6f1-ba11-441d-bd5e-d92e901455d9',
    'c3fcbec9-0678-4ca3-ad9b-79b83c8f3fd7',
    'f21bdd07-42f0-4002-94a5-edf43b21ebad'
)

Write-Output "=== Job Statuses ==="
foreach ($id in $jobIds) {
    try {
        $r = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/ingestions/$id" -Method GET
        Write-Output "$($id.Substring(0,8))... status=$($r.status) progress=$($r.progress)"
    } catch {
        Write-Output "$($id.Substring(0,8))... ERROR: $($_.Exception.Message)"
    }
}

Write-Output ""
Write-Output "=== Notes Count ==="
try {
    $stats = Invoke-RestMethod -Uri 'http://localhost:8000/api/v1/stats/counts' -Method GET
    Write-Output "Notes: $($stats.notes)  Memories: $($stats.memories)  Entities: $($stats.entities)  Reflection Queue: $($stats.reflection_queue)"
} catch {
    Write-Output "Stats ERROR: $($_.Exception.Message)"
}
