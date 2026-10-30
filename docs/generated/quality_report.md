# Laporan kualitas data (kontrak: config/contract.yaml)

Portfolio score: **65.0/100** (WARN)

| tabel | skor | check gagal |
|---|---|---|
| raw.transaksi | 40.0 | `not_null:kode_skpd` **FAIL** — 12 NULL<br>`unique:id_bku` **FAIL** — 5 nilai ganda |
| staging.stg_transaksi | 40.0 | `freshness:loaded_at` **FAIL** — 4.5 jam sejak update terakhir<br>`row_count:rows` **FAIL** — 8845 baris |
| marts.transaksi_bku | 100.0 | semua PASS |
| marts.fct_realisasi | 45.0 | `invariant:realisasi <= pagu` **FAIL** — 1 baris melanggar<br>`freshness:updated_at` **WARN** — 2.6 jam sejak update terakhir |
| marts.dim_skpd | 100.0 | semua PASS |
