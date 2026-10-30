"""Seed the data plane: raw -> staging -> marts, with *deliberate* defects.

A data-quality platform that only ever sees clean data proves nothing. The
seeder therefore plants the exact defect classes the PRD's quality gates are
meant to catch, so every FAIL in the committed evidence was detected for a
real reason:

  1. raw.transaksi: 12 rows with NULL kode_skpd, 5 duplicate id_bku values
     -> not_null + unique FAIL at the source, as in any real feeder.
  2. staging.stg_transaksi: the loader silently dropped too many rows
     (8,850 vs contract min 9,000) and its last run was 4.5h ago
     -> row_count FAIL + freshness FAIL (PRD: error at 4h).
  3. marts.fct_realisasi: one SKPD reports realisasi > pagu (the P2
     invariant again) and was last updated 2.6h ago
     -> invariant FAIL + freshness WARN (between 2h and 4h).
  4. marts.transaksi_bku and marts.dim_skpd are healthy -> score 100,
     proving the scorer is not simply failing everything.

Deterministic: fixed value patterns, seeded RNG, timestamps relative to a
single anchor so a re-run reproduces the same shapes.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import psycopg

SKPDS = [
    ("1.01", "Sekretariat Daerah"),
    ("1.03", "Dinas Pendidikan"),
    ("1.05", "Dinas Sosial"),
    ("1.06", "Dinas Kesehatan"),
    ("1.09", "Dinas Pekerjaan Umum"),
]

SCHEMA_SQL = """
DROP SCHEMA IF EXISTS raw CASCADE;
DROP SCHEMA IF EXISTS staging CASCADE;
DROP SCHEMA IF EXISTS marts CASCADE;
CREATE SCHEMA raw;
CREATE SCHEMA staging;
CREATE SCHEMA marts;

CREATE TABLE raw.transaksi (
    id_bku       BIGINT NOT NULL,  -- no PK: the feeder is untrusted; duplicates
    kode_skpd TEXT,               -- are exactly what the `unique` check detects
    nik_pelapor  TEXT,
    uraian       TEXT,
    nilai        NUMERIC(18,2),
    tanggal      DATE,
    loaded_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON raw.transaksi (id_bku);

CREATE TABLE staging.stg_transaksi (
    id_bku       BIGINT PRIMARY KEY,
    kode_skpd    TEXT NOT NULL,
    nik_pelapor  TEXT,
    uraian       TEXT,
    nilai        NUMERIC(18,2),
    tanggal      DATE,
    loaded_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE marts.transaksi_bku (
    id_bku       BIGINT PRIMARY KEY,
    kode_skpd    TEXT NOT NULL,
    nik_pelapor  TEXT,
    uraian       TEXT,
    nilai        NUMERIC(18,2),
    tanggal      DATE,
    loaded_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE marts.dim_skpd (
    kode_skpd    TEXT PRIMARY KEY,
    nama_skpd    TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE marts.fct_realisasi (
    kode_skpd    TEXT PRIMARY KEY REFERENCES marts.dim_skpd(kode_skpd),
    pagu         NUMERIC(18,2) NOT NULL,
    realisasi    NUMERIC(18,2) NOT NULL,
    persen       NUMERIC(5,2) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
"""


def seed(conn: psycopg.Connection, anchor: datetime | None = None) -> dict:
    """(Re)create the schemas and load deterministic data. Returns row counts."""
    anchor = anchor or datetime.now().astimezone()
    rng = random.Random(2026)

    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)

        # dim_skpd: healthy.
        for kode, nama in SKPDS:
            cur.execute(
                "INSERT INTO marts.dim_skpd VALUES (%s, %s, %s)",
                (kode, nama, anchor - timedelta(minutes=20)),
            )

        # raw.transaksi: 9,400 rows, loaded 1h ago (fresh), with planted defects.
        raw_loaded = anchor - timedelta(hours=1)
        rows = []
        for i in range(1, 9401):
            kode = SKPDS[i % len(SKPDS)][0]
            nik = f"3273{i:012d}"
            nilai = round(rng.uniform(50_000, 950_000), 2)
            rows.append(
                (i, kode, nik, f"Belanja {i % 7} untuk {kode}", nilai,
                 anchor.date() - timedelta(days=i % 30), raw_loaded)
            )
        # Defect 1a: 12 NULL kode_skpd (row 9401..9412).
        for i in range(9401, 9413):
            rows.append(
                (i, None, f"3273{i:012d}", "Tanpa kode SKPD", 10_000.0,
                 anchor.date(), raw_loaded)
            )
        # Defect 1b: 5 duplicate id_bku values (same id, different nilai).
        for i in range(1, 6):
            rows.append(
                (i, SKPDS[i % len(SKPDS)][0], f"32739{i:011d}", "Duplikat feeder",
                 999_999.99, anchor.date(), raw_loaded)
            )
        cur.executemany(
            "INSERT INTO raw.transaksi VALUES (%s,%s,%s,%s,%s,%s,%s)", rows
        )

        # staging: the loader ran 4.5h ago (freshness FAIL past 4h) and kept
        # only 8,850 rows — below the contract min of 9,000 (row_count FAIL).
        stg_loaded = anchor - timedelta(hours=4, minutes=30)
        stg_rows = [
            (r[0], r[1], r[2], r[3], r[4], r[5], stg_loaded)
            for r in rows[:8850]
            if r[1] is not None and r[0] > 5  # drops nulls and the duplicates
        ][:8850]
        cur.executemany(
            "INSERT INTO staging.stg_transaksi VALUES (%s,%s,%s,%s,%s,%s,%s)",
            stg_rows,
        )

        # marts.transaksi_bku: healthy copy of staging.
        mart_loaded = anchor - timedelta(minutes=35)
        cur.executemany(
            "INSERT INTO marts.transaksi_bku VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], mart_loaded) for r in stg_rows],
        )

        # fct_realisasi: per-SKPD totals; one SKPD inflated above its pagu.
        fct_updated = anchor - timedelta(hours=2, minutes=36)
        for kode, _nama in SKPDS:
            cur.execute(
                "SELECT COALESCE(SUM(nilai), 0) FROM staging.stg_transaksi "
                "WHERE kode_skpd = %s",
                (kode,),
            )
            total = float(cur.fetchone()[0])
            pagu = round(total * 1.4, 2)
            realisasi = round(total, 2)
            if kode == "1.05":  # Defect 3: realisasi exceeds pagu by design.
                pagu = round(total * 0.9, 2)
            persen = round(100 * realisasi / pagu, 2) if pagu else 0
            cur.execute(
                "INSERT INTO marts.fct_realisasi VALUES (%s,%s,%s,%s,%s)",
                (kode, pagu, realisasi, persen, fct_updated),
            )

        cur.execute("ANALYZE")
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 'raw.transaksi', COUNT(*) FROM raw.transaksi
            UNION ALL SELECT 'staging.stg_transaksi', COUNT(*) FROM staging.stg_transaksi
            UNION ALL SELECT 'marts.transaksi_bku', COUNT(*) FROM marts.transaksi_bku
            UNION ALL SELECT 'marts.dim_skpd', COUNT(*) FROM marts.dim_skpd
            UNION ALL SELECT 'marts.fct_realisasi', COUNT(*) FROM marts.fct_realisasi
            """
        )
        return {name: int(n) for name, n in cur.fetchall()}
