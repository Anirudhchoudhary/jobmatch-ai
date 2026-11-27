// Types mirroring the backend's public JSON schemas (backend/app/schemas, guardrails/base.py).

export type Importance = "required" | "nice_to_have";
export type MatchStatus = "matched" | "partial" | "missing";
export type DocType = "resume" | "jd";

// --- Error envelope ---

export interface ApiErrorBody {
  error: { code: string; message: string; request_id: string };
}

// --- Guardrails ---

export type GuardrailStage = "upload" | "chat_input" | "output";
export type GuardrailAction = "allow" | "redact" | "flag" | "block";
export type GuardrailMode = "enforce" | "monitor";

export interface GuardrailOutcome {
  guardrail: string;
  provider: string;
  stage: GuardrailStage;
  action: GuardrailAction | "error";
  mode: GuardrailMode;
  reason: string;
  latency_ms: number;
  items: number[];
}

// --- Uploads and documents ---

export interface UploadTicket {
  key: string;
  url: string;
  method: "PUT";
  headers: Record<string, string>;
  max_bytes: number;
}

export interface DocumentReady {
  document_id: string;
  doc_type: DocType;
  chunk_count: number;
  reused: boolean;
  quarantined_count: number;
  guardrails: GuardrailOutcome[];
}

export interface DocumentInfo {
  doc_id: string;
  doc_type: string;
  source: string;
  filename: string | null;
  status: string;
  chunk_count: number;
  quarantined_count: number;
  created_at: string;
}

export interface DocumentList {
  documents: DocumentInfo[];
}

export interface ChunkText {
  chunk_id: string;
  doc_type: string;
  section: string;
  text: string;
}

// --- Match analysis ---

export interface Evidence {
  chunk_id: string;
  quote: string;
}

export interface Requirement {
  skill: string;
  text: string;
  importance: Importance;
  jd_chunk_id: string;
}

export interface SkillMatch {
  requirement: Requirement;
  status: MatchStatus;
  evidence: Evidence[];
}

export interface MatchAnalysis {
  analysis_id: string;
  score: number;
  required_coverage: number;
  nice_to_have_coverage: number | null;
  skills: SkillMatch[];
  downgraded_count: number;
  analyst: string;
  cached: boolean;
  request_id: string;
}

export interface BulletSuggestion {
  original_chunk_id: string;
  original: string;
  rewritten: string;
  rationale: string;
  target_skills: string[];
}

export interface BulletSuggestions {
  analysis_id: string;
  suggestions: BulletSuggestion[];
  rejected_count: number;
  analyst: string;
  request_id: string;
}

// --- Chat ---

export interface Citation {
  label: string;
  chunk_id: string;
  doc_type: string;
  section: string;
  text: string;
}

export interface Verdict {
  grounded: boolean;
  action: "allow" | "flag";
  unsupported_claims: string[];
  mode: GuardrailMode | null;
}

export interface ChatDone {
  answer: string;
  citations: Citation[];
  invalid_citations: string[];
  refused: boolean;
  prompt_version: string;
  request_id: string;
  guardrails: GuardrailOutcome[];
  guardrail_checks: GuardrailOutcome[];
  blocked: boolean;
  cached: boolean;
}
