-- Migration: Add uploaded_files column to cases table
-- Date: 2025-01-09
-- Description: Adds uploaded_files JSONB column to track raw file metadata separately from evidence
--
-- Background:
-- - Evidence is investigation-linked data derived from files, ONLY exists in INVESTIGATING phase
-- - Uploaded files can exist in ANY case phase (CONSULTING or INVESTIGATING)
-- - This separation allows accurate uploaded_files_count in UI regardless of investigation status

-- Add uploaded_files column (JSONB array)
ALTER TABLE cases
ADD COLUMN IF NOT EXISTS uploaded_files JSONB DEFAULT '[]'::jsonb NOT NULL;

-- Add index for JSONB queries (optional, for performance)
CREATE INDEX IF NOT EXISTS idx_cases_uploaded_files_gin
ON cases USING gin (uploaded_files);

-- Backfill: Migrate existing evidence to uploaded_files for backward compatibility
-- This creates uploaded_files entries from existing evidence records
UPDATE cases
SET uploaded_files = (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'file_id', e->>'evidence_id',
                'filename', COALESCE(e->>'summary', 'unknown'),
                'size_bytes', COALESCE((e->>'content_size_bytes')::int, 0),
                'data_type', COALESCE(e->>'source_type', 'unknown'),
                'uploaded_at_turn', COALESCE((e->>'collected_at_turn')::int, 0),
                'uploaded_at', COALESCE(e->>'collected_at', now()::text),
                'source_type', COALESCE(e->>'source_type', 'file_upload'),
                'preprocessing_summary', e->>'summary',
                'content_ref', COALESCE(e->>'content_ref', 'unknown')
            )
        ),
        '[]'::jsonb
    )
    FROM jsonb_array_elements(cases.evidence) AS e
)
WHERE jsonb_array_length(uploaded_files) = 0
  AND jsonb_array_length(evidence) > 0;

-- Verification query (run after migration to verify)
-- SELECT
--     case_id,
--     status,
--     jsonb_array_length(uploaded_files) as uploaded_files_count,
--     jsonb_array_length(evidence) as evidence_count
-- FROM cases
-- WHERE jsonb_array_length(uploaded_files) > 0
-- LIMIT 10;
