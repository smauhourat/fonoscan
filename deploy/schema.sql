-- =====================================================================
-- fonoscan — esquema operativo
-- PostgreSQL 15+
--
-- Tres dominios separados a propósito:
--   1. catálogo y titularidad   (dato maestro, viene del sistema de socios)
--   2. detección                (lo que produce el motor)
--   3. liquidación y auditoría  (lo que se informa y hay que poder defender)
--
-- Principio rector: la detección NUNCA se edita en el lugar. Una corrección
-- genera una fila nueva con supersede_id; el histórico queda intacto porque es
-- la base probatoria del reparto.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- 1. Catálogo
-- ---------------------------------------------------------------------

CREATE TABLE track (
    track_id        BIGSERIAL PRIMARY KEY,
    isrc            CHAR(12),
    title           TEXT NOT NULL,
    artist          TEXT NOT NULL,
    label           TEXT,
    album           TEXT,
    release_year    SMALLINT,
    duration_s      REAL NOT NULL DEFAULT 0,
    audio_uri       TEXT,                 -- objeto en almacenamiento (S3/MinIO)
    audio_sha256    CHAR(64),             -- integridad del máster de referencia
    -- Titularidad: se resuelve contra el sistema de socios. Se guarda
    -- desnormalizado para poder reconstruir un reparto histórico tal como se
    -- liquidó, aunque después cambie la titularidad.
    rights_owner_id TEXT,
    territory       CHAR(2),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','withdrawn','duplicate','pending_audio')),
    duplicate_of    BIGINT REFERENCES track(track_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Un ISRC puede repetirse entre territorios/licencias; la unicidad fuerte es
-- (isrc, territory). Se permite NULL para altas sin ISRC asignado todavía.
CREATE UNIQUE INDEX track_isrc_territory_uidx
    ON track (isrc, COALESCE(territory,'--')) WHERE isrc IS NOT NULL;
CREATE INDEX track_artist_title_idx ON track USING gin (
    to_tsvector('simple', coalesce(artist,'') || ' ' || coalesce(title,'')));

-- Referencias indexadas (una por variante de velocidad y versión de config).
CREATE TABLE fp_reference (
    ref_id       BIGSERIAL PRIMARY KEY,
    track_id     BIGINT NOT NULL REFERENCES track(track_id) ON DELETE CASCADE,
    speed        REAL NOT NULL DEFAULT 1.0,
    config_id    TEXT NOT NULL,
    n_landmarks  INTEGER NOT NULL DEFAULT 0,
    duration_s   REAL NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (track_id, speed, config_id)
);

CREATE TABLE fp_landmark (
    hash   INTEGER NOT NULL,
    ref_id BIGINT  NOT NULL,
    t      INTEGER NOT NULL
) PARTITION BY HASH (hash);

DO $$
BEGIN
  FOR i IN 0..31 LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS fp_landmark_p%s PARTITION OF fp_landmark
         FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS fp_landmark_p%s_hash_idx
         ON fp_landmark_p%s (hash) INCLUDE (ref_id, t)', i, i);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 2. Medios monitoreados
-- ---------------------------------------------------------------------

CREATE TABLE channel (
    channel_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    medium          TEXT NOT NULL CHECK (medium IN ('radio','tv','streaming','ambiental','otro')),
    stream_url      TEXT,
    capture_method  TEXT DEFAULT 'http'
                    CHECK (capture_method IN ('http','hls','udp','rtsp','sdr','line_in','file')),
    territory       CHAR(2),
    license_id      TEXT,                 -- contrato/licencia del usuario de música
    tariff_class    TEXT,
    weight          REAL NOT NULL DEFAULT 1.0,   -- coeficiente de ponderación
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Salud de captura: sin esto no se puede distinguir "no hubo música" de
-- "el capturador estuvo caído", y esa distinción es la diferencia entre un
-- reparto defendible y uno impugnable.
CREATE TABLE capture_session (
    session_id   BIGSERIAL PRIMARY KEY,
    channel_id   TEXT NOT NULL REFERENCES channel(channel_id),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    seconds_captured  DOUBLE PRECISION NOT NULL DEFAULT 0,
    seconds_silent    DOUBLE PRECISION NOT NULL DEFAULT 0,
    disconnections    INTEGER NOT NULL DEFAULT 0,
    worker_host  TEXT,
    engine_version TEXT,
    config_id    TEXT
);
CREATE INDEX capture_session_channel_time_idx
    ON capture_session (channel_id, started_at DESC);

-- ---------------------------------------------------------------------
-- 3. Detección
-- ---------------------------------------------------------------------

CREATE TABLE play (
    play_id        BIGSERIAL,
    channel_id     TEXT   NOT NULL REFERENCES channel(channel_id),
    track_id       BIGINT NOT NULL REFERENCES track(track_id),
    ref_id         BIGINT,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ NOT NULL,
    detected_seconds REAL NOT NULL,
    work_coverage  REAL NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('full','partial','fragment')),
    speed          REAL NOT NULL DEFAULT 1.0,
    offset_s       DOUBLE PRECISION,
    -- Métricas de confianza: se persisten para poder recalibrar umbrales sobre
    -- datos históricos sin volver a procesar el audio.
    hits           INTEGER NOT NULL,
    best_score     INTEGER NOT NULL,
    mean_score     REAL NOT NULL,
    min_margin     REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'confirmed'
                   CHECK (status IN ('confirmed','review','rejected','corrected','superseded')),
    supersede_id   BIGINT,               -- fila que reemplaza a ésta
    engine_version TEXT NOT NULL,
    config_id      TEXT NOT NULL,
    index_version  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (play_id, started_at)
) PARTITION BY RANGE (started_at);

-- Particionado mensual: el volumen crece linealmente con canales x tiempo y
-- la consulta típica es por período de liquidación.
CREATE TABLE play_default PARTITION OF play DEFAULT;

-- Ejemplo de partición; crear con pg_partman o un job mensual.
-- CREATE TABLE play_2026_09 PARTITION OF play
--     FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

CREATE INDEX play_channel_time_idx ON play (channel_id, started_at DESC);
CREATE INDEX play_track_time_idx   ON play (track_id, started_at DESC);
CREATE INDEX play_status_idx       ON play (status) WHERE status IN ('review','rejected');

-- Evidencia de audio asociada al pase.
CREATE TABLE evidence (
    evidence_id  BIGSERIAL PRIMARY KEY,
    channel_id   TEXT NOT NULL REFERENCES channel(channel_id),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ NOT NULL,
    storage_uri  TEXT NOT NULL,           -- s3://evidencia/FM100/2026-09-11T09-00-00.opus
    sha256       CHAR(64) NOT NULL,
    codec        TEXT NOT NULL DEFAULT 'opus',
    bitrate_kbps SMALLINT,
    retention_until DATE NOT NULL,        -- política de retención documentada
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX evidence_channel_time_idx ON evidence (channel_id, started_at);

-- ---------------------------------------------------------------------
-- 4. Revisión humana
-- ---------------------------------------------------------------------

CREATE TABLE review_task (
    task_id      BIGSERIAL PRIMARY KEY,
    play_id      BIGINT NOT NULL,
    play_started_at TIMESTAMPTZ NOT NULL,
    reason       TEXT NOT NULL,           -- low_margin | fragment | duplicate_catalog | claim
    priority     SMALLINT NOT NULL DEFAULT 5,
    assigned_to  TEXT,
    resolved_at  TIMESTAMPTZ,
    resolution   TEXT CHECK (resolution IN ('confirm','reassign','reject','unresolved')),
    resolved_track_id BIGINT REFERENCES track(track_id),
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX review_task_open_idx ON review_task (priority, created_at)
    WHERE resolved_at IS NULL;

-- Reclamos de socios o de usuarios de música sobre un período.
CREATE TABLE claim (
    claim_id     BIGSERIAL PRIMARY KEY,
    claimant_id  TEXT NOT NULL,
    claim_type   TEXT NOT NULL CHECK (claim_type IN ('missing_play','wrong_attribution','excess_play')),
    channel_id   TEXT REFERENCES channel(channel_id),
    track_id     BIGINT REFERENCES track(track_id),
    period_start DATE NOT NULL,
    period_end   DATE NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','investigating','accepted','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- 5. Liquidación y auditoría
-- ---------------------------------------------------------------------

CREATE TABLE usage_report (
    report_id     BIGSERIAL PRIMARY KEY,
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    weighting     TEXT NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_by  TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    config_id     TEXT NOT NULL,
    -- Congelamiento: el reporte se calcula una vez y se sella. Si después se
    -- corrige una detección, se emite un reporte de ajuste, no se reescribe.
    frozen        BOOLEAN NOT NULL DEFAULT false,
    sha256        CHAR(64),
    row_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE usage_report_row (
    report_id     BIGINT NOT NULL REFERENCES usage_report(report_id) ON DELETE CASCADE,
    channel_id    TEXT   NOT NULL,
    track_id      BIGINT NOT NULL,
    isrc          CHAR(12),
    plays_full    INTEGER NOT NULL DEFAULT 0,
    plays_partial INTEGER NOT NULL DEFAULT 0,
    plays_fragment INTEGER NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0,
    weighted_units DOUBLE PRECISION NOT NULL DEFAULT 0,
    plays_in_review INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (report_id, channel_id, track_id)
);

-- Bitácora inmutable: quién hizo qué. Requisito habitual de las auditorías a
-- entidades de gestión y de las normas de transparencia hacia los socios.
CREATE TABLE audit_log (
    audit_id    BIGSERIAL PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT,
    payload     JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_entity_idx ON audit_log (entity, entity_id, occurred_at DESC);

-- ---------------------------------------------------------------------
-- 6. Vistas de control
-- ---------------------------------------------------------------------

-- Tasa de identificación por canal y día: primer indicador que se mira todos
-- los días. Una caída suele ser falla de captura, no falta de música.
CREATE OR REPLACE VIEW v_identification_rate AS
SELECT
    c.channel_id,
    c.name,
    d.day,
    COALESCE(SUM(p.detected_seconds), 0)            AS identified_seconds,
    COALESCE(MAX(cs.captured_seconds), 0)           AS captured_seconds,
    CASE WHEN COALESCE(MAX(cs.captured_seconds),0) > 0
         THEN COALESCE(SUM(p.detected_seconds),0) / MAX(cs.captured_seconds)
         ELSE 0 END                                 AS identification_rate,
    COUNT(p.play_id)                                AS plays
FROM channel c
CROSS JOIN LATERAL (
    SELECT generate_series(current_date - 30, current_date, '1 day')::date AS day
) d
LEFT JOIN LATERAL (
    SELECT SUM(seconds_captured) AS captured_seconds
    FROM capture_session s
    WHERE s.channel_id = c.channel_id
      AND s.started_at >= d.day AND s.started_at < d.day + 1
) cs ON true
LEFT JOIN play p
       ON p.channel_id = c.channel_id
      AND p.started_at >= d.day AND p.started_at < d.day + 1
      AND p.status = 'confirmed'
GROUP BY c.channel_id, c.name, d.day, cs.captured_seconds;

-- Cola de revisión priorizada.
CREATE OR REPLACE VIEW v_review_queue AS
SELECT r.task_id, r.reason, r.priority, r.created_at,
       p.channel_id, p.started_at, p.detected_seconds, p.min_margin, p.mean_score,
       t.isrc, t.title, t.artist
FROM review_task r
JOIN play  p ON p.play_id = r.play_id AND p.started_at = r.play_started_at
JOIN track t ON t.track_id = p.track_id
WHERE r.resolved_at IS NULL
ORDER BY r.priority, r.created_at;
