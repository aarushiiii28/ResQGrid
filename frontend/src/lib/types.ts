// Shapes used by the UI (a subset of the API responses).

export type Role = 'caller' | 'dispatcher' | 'driver' | 'hospital' | 'admin'

export interface User {
  sub: string
  role: Role
  name: string
  vehicle_id?: string | null
  hospital_id?: string | null
}

export interface Prov {
  value: unknown
  source: 'reported' | 'measured' | 'ai_extracted' | 'human_confirmed' | 'simulated'
  at: string
  confidence: number
  human_confirmed: boolean
  evidence?: string | null
}

export interface GeoPoint {
  lat: number
  lon: number
  accuracy_m?: number | null
  at?: string | null
  approximate?: boolean
  source?: string
}

export interface Incident {
  id: string
  created_at: string
  created_by: string | null
  status: 'pending' | 'new' | 'dispatched' | 'active' | 'closed' | 'merged' | 'cancelled_by_caller'
  channel: string
  reported_category: Prov | null
  operator_priority: Prov | null
  patient_count: Prov | null
  location_candidates: (GeoPoint & { label?: string | null })[]
  facts: Record<string, Prov>
  unknowns: string[]
  conflicts: { field: string; existing: unknown; incoming: unknown; resolved: boolean }[]
  assigned_vehicle_id: string | null
  destination_id: string | null
  needs_review: boolean
  review_reasons: string[]
  building_id: string | null
  duplicate_suggestions: { incident_id: string; score: number; reasons: string[]; status: string }[]
}

export interface VehicleView {
  id: string
  capability_tags: string[]
  status: string
  availability: 'available' | 'not_available' | 'unknown'
  location: GeoPoint | null
  location_state: 'live' | 'stale' | 'unknown'
  location_age_s: number | null
  last_update_age_s: number | null
  assigned_incident_id: string | null
  offline: boolean
}

export interface ResourceView {
  status: 'available' | 'unavailable' | 'unknown'
  reported_status: string
  count: number | null
  stale: boolean
  age_s: number | null
  source: string
}

export interface HospitalView {
  id: string
  name: string
  location: GeoPoint
  capabilities: string[]
  resources: Record<string, ResourceView>
}

export interface Components {
  time: number
  freshness: number
  coverage_loss: number
  uncertainty: number
  switch: number
  weighted: Record<string, number>
  total: number
}

export interface Candidate {
  vehicle_id: string
  hospital_id: string
  feasible: boolean
  rank: number | null
  is_current: boolean
  eta_scene_low_s: number
  eta_scene_s: number
  eta_scene_high_s: number
  eta_hospital_s: number
  eta_total_low_s: number
  eta_total_high_s: number
  components: Components
  constraints: Record<string, boolean>
  reason_codes: string[]
}

export interface RouteEstimate {
  distance_m: number
  duration_low_s: number
  duration_s: number
  duration_high_s: number
  polyline: [number, number][]
  steps: { instruction: string; maneuver: string; distance_m: number; duration_s: number; lat: number; lon: number; name?: string | null }[]
  source: string
  confidence: number
  degraded: boolean
  notes: string[]
  feasible: boolean
}

export interface Decision {
  id: string
  incident_id: string
  policy: 'P0' | 'P1'
  policy_version: string
  created_at: string
  status: 'pending' | 'approved' | 'rejected' | 'overridden' | 'superseded' | 'kept_current'
  weights: Record<string, number>
  candidates: Candidate[]
  exclusions: { kind: string; id: string; reasons: string[] }[]
  recommendation: Candidate | null
  baseline: Candidate | null
  keep_current: boolean
  assumptions: string[]
  scene_route: RouteEstimate | null
  hospital_route: RouteEstimate | null
  approved_by: string | null
  rationale: string | null
}

export interface Assignment {
  id: string
  incident_id: string
  vehicle_id: string
  hospital_id: string | null
  state: string
  late: boolean
  sent_at: string | null
  acknowledged_at: string | null
  accepted_at: string | null
  timeout_s: number
  fallback: { message: string } | null
}

export interface AlertItem {
  id: string
  kind: string
  severity: 'red' | 'amber'
  text: string
  entity: string
  incident_id: string | null
  pulse: boolean
}

export interface AuditEvent {
  seq: number
  event_id: string
  correlation_id: string | null
  entity_type: string
  entity_id: string
  event_type: string
  occurred_at: string
  actor: string
  source: string | null
  reason: string | null
  old_value: unknown
  new_value: unknown
  payload: Record<string, unknown>
  ledger?: LedgerInfo | null
}

/** Where an event sits in the tamper-evident ledger. ANCHORED > SIGNED (by the actor's device) > PENDING (server-attested, waiting for an anchor). */
export interface LedgerInfo {
  seq: number | null
  entry_hash: string | null
  signer_key_id: string | null
  client_signed: boolean
  anchor_id: string | null
  ledger_status: 'ANCHORED' | 'SIGNED' | 'PENDING'
}

export interface Disruption {
  id: string
  kind: string
  lat: number | null
  lon: number | null
  radius_m: number | null
  multiplier: number
  note?: string | null
}

export interface PreAlert {
  id: string
  incident_id: string
  hospital_id: string
  vehicle_id: string
  created_at: string
  reported_reason: string
  reported_category: string | null
  confirmed_observations: { key: string; value: unknown; source: string; at: string }[]
  reported_observations: { key: string; value: unknown; source: string; at: string }[]
  unknowns: string[]
  eta_low_s: number | null
  eta_high_s: number | null
  status: string
  acknowledged_at: string | null
  arrived_at: string | null
  arrival_source: string | null
  handover_started_at: string | null
  handover_completed_at: string | null
  handover_duration_s: number | null
  checklist: { key: string; label: string; status: string; note: string | null }[]
  superseded_reason: string | null
}

export interface Tracker {
  dispatch_stopped?: boolean
  incident_id: string
  status: string
  headline: string
  stages: { key: string; label: string; state: 'done' | 'active' | 'pending'; at: string | null }[]
  current_stage: string | null
  category: string
  needs_review: boolean
  location: { lat: number; lon: number; accuracy_m: number | null; approximate: boolean; source: string | null } | null
  vehicle: { id: string; lat: number; lon: number; location_age_s: number | null; source: string; status: string; state: string; simulated: boolean } | null
  nearby?: { id: string; lat: number; lon: number; distance_m: number; tags: string[]; simulated: boolean }[]
  route: { polyline: [number, number][]; phase: string; degraded: boolean; source: string } | null
  eta: { low_s: number; high_s: number; text: string; phase: string } | null
  hospital: { id: string; name: string; lat: number; lon: number } | null
  assignment: { id: string; state: string; late: boolean } | null
  pipeline: { step: string; status: string; detail: string; at: string }[]
  autopilot: { enabled: boolean; label: string }
}

export interface NavPlan {
  vehicle_id: string
  incident_id: string
  phase: 'scene' | 'hospital'
  vehicle_status: string
  target: { kind: string; name: string; lat: number; lon: number }
  route: RouteEstimate
  along_m: number
  remaining_m: number
  remaining_low_s: number
  remaining_s: number
  remaining_high_s: number
  next_step: RouteEstimate['steps'][number] | null
  distance_to_next_m: number | null
  last_100m: boolean
  entrance: {
    chosen: { entrance: { name: string; landmark: string | null; accessible: boolean; gate_restricted: boolean; kind: string }; reasons: string[] } | null
    needs_confirmation: boolean
    confirmation_request: string | null
    alternatives: { entrance: { name: string } }[]
  } | null
  reroute: {
    id: string
    reason: string
    message: string
    delta_s: number
    old_remaining_s: number
    new_remaining_s: number
    status: string
    route: RouteEstimate
  } | null
  green_corridor: { simulation: boolean; label: string; signals: { id: string; lat: number; lon: number; along_m: number }[] }
  simulated_movement: boolean
  route_lock?: { locked: boolean; version: number; reroutes: number; policy_version: string; committed_at: string } | null
  route_notice?: RouteNotice | null
  off_route?: { state: 'uncertain' | 'suspect' | 'off_route'; deviation_m: number; message: string; rejoin_in_m: number | null; via: string | null } | null
  backups?: { id: string; polyline: [number, number][]; duration_s: number; feasible: boolean; label: string }[]
}

/** A same-destination route change the stable-route policy applied by itself. The driver acknowledges; nothing asks for approval. */
export interface RouteNotice {
  id: string
  kind: 'reroute' | 'rejoin'
  reason: 'blocked' | 'wrong_turn' | 'congestion'
  message: string
  saved_s: number
  old_remaining_s: number
  new_remaining_s: number
  reason_codes: string[]
  at: string
  acknowledged: boolean
}

export interface Aed {
  id: string
  label: string
  lat: number
  lon: number
  distance_m: number
  walk_low_s: number
  walk_s: number
  walk_high_s: number
}

export interface VolunteerAlert {
  id: string
  volunteer_id: string
  volunteer_name: string
  distance_m: number
  walk_s: number
  aed_label: string | null
  status: string
  simulated: boolean
}

export interface TranscriptLine {
  speaker: 'caller' | 'operator' | 'system'
  text: string
  at: string
  mode: string
}

export interface WsEvent {
  event_id: string
  event_type: string
  occurred_at: string
  entity_id: string
  correlation_id: string | null
  source: string
  payload: Record<string, unknown>
  seq: number
}

// ---- Phase 5 / 6 ----
export interface RecoveryOption {
  id: string
  kind: 'keep_current_plan' | 'request_confirmation' | 'reroute' | 'alternate_destination' | 'propose_eligible_unit' | 'escalate'
  label: string
  description: string
  effects: {
    time_delta_s: number | null
    suitability: 'unchanged' | 'improved' | 'reduced' | 'unverified'
    suitability_note: string
    coverage_delta: number | null
    other_incidents: { incident_id: string; impact: string }[]
    uncertainty: string
  }
  trade_offs: string[]
  benefit: number | null
  changes_plan: boolean
  necessary: boolean
  below_threshold: boolean
  recommended: boolean
  payload: Record<string, unknown>
}

export interface Risk {
  id: string
  incident_id: string
  type: string
  severity: 'low' | 'medium' | 'high' | 'critical'
  entity: string
  evidence: { key: string; value: unknown; at: string | null }[]
  affected_assumption: string
  detected_at: string
  last_seen_at: string
  status: 'open' | 'awaiting' | 'resolved' | 'cleared'
  options: RecoveryOption[]
  confirmations: { id: string; target_kind: string; target_id: string; resource: string | null; message: string; requested_at: string }[]
  escalated: boolean
  resolution: { option?: string; by?: string; reason?: string; at?: string; message?: string } | null
  seen_count: number
}

export interface Patient {
  id: string
  triage: 'red' | 'amber' | 'green' | 'black' | 'unassigned'
  confirmed: boolean
  suggested: string | null
  suggested_evidence: string | null
  set_by: string | null
  note: string | null
}

export interface McLoad {
  id: string
  patient_ids: string[]
  triage_mix: Record<string, number>
  hospital_id: string
  vehicle_needs: string[]
  vehicle_id: string | null
  eta_scene_s: number | null
  eta_hospital_s: number | null
  status: string
}

export interface McHospitalLoad {
  hospital_id: string
  name: string
  capacity: number
  red_capacity: number
  planned: number
  planned_red: number
  en_route: number
  fraction: number
  unverified_resources: string[]
  note: string | null
}

export interface Mci {
  id: string
  incident_id: string
  patients: Patient[]
  status: 'triage' | 'planned' | 'approved'
  approved_by: string | null
  plan: {
    patient_hospital: Record<string, string>
    overflow: string[]
    loads: McLoad[]
    hospital_loads: McHospitalLoad[]
    unconfirmed: string[]
    warnings: string[]
    rules: string[]
    split: { ortools: { cost: number; assigned: number; overflow: number; status: string }; greedy: { cost: number; assigned: number; overflow: number } }
    assignment: { ortools: { cost: number; assigned: number; unassigned: string[]; status: string }; greedy: { cost: number; assigned: number; unassigned: string[] } }
  } | null
}

export interface Forecast {
  label: string
  approximation: string
  bucket: string
  day_type: string
  history_count: number
  expected_per_hour_total: number
  heat: [number, number, number][]
  standby: { id: string; lat: number; lon: number; covers_share: number }[]
  moves: { standby: string; vehicle_id: string; lat: number; lon: number; distance_km: number; covers_share: number }[]
  coverage_before: number
  coverage_after: number
  radius_m: number
  units_considered: number
  explain: { formula: string; top_cells: { lat: number; lon: number; expected_per_hour: number }[]; hotspots: string[] }
}

export interface SimMetrics {
  [k: string]: number | null
}
export interface SimResult {
  policy: string
  policy_version: string
  description: string
  metrics: SimMetrics
  incidents: { incident: string; category: string; priority: number; t_call: number; served: boolean; response_s: number; dest_s: number; meets_requirements: boolean }[]
  failures: { kind: string; assignment: string; incident: string; onset_s: number; detected_s: number | null; decided_s: number | null }[]
  timeline: { t: number; kind: string; incident: string | null; detail: string; vehicle?: string }[]
  tracks: Record<string, [number, number][]>
  end_s: number
}
export interface SimRun {
  id: string
  label: string
  seed: number
  faults: string[]
  config: Record<string, unknown>
  policy_versions: Record<string, string>
  world: {
    hospitals: { id: string; lat: number; lon: number; caps: string[] }[]
    vehicles: { id: string; lat: number; lon: number; tags: string[] }[]
    incidents: { id: string; t_call: number; category: string; priority: number; lat: number; lon: number; duplicate_of: string | null; burst: boolean }[]
    closures: { id: string; lat: number; lon: number; radius_m: number; t0: number; t1: number }[]
  }
  results: Record<string, SimResult>
}
export interface Dist {
  n: number
  mean?: number
  median?: number
  p10?: number
  p90?: number
  min?: number
  max?: number
  values?: number[]
}
export interface Benchmark {
  label: string
  seeds: number[]
  faults: string[]
  policies: string[]
  distributions: Record<string, Record<string, Dist>>
  paired: Record<string, Record<string, { better: number; worse: number; tied: number; mean_delta: number; lower_is_better: boolean }>>
  where_p2_is_worse_than_p0: { metric: string; label: string; seeds_worse: number; of: number; mean_delta: number; lower_is_better: boolean }[]
  labels: Record<string, string>
}

// ---- M18 false-alarm assessment (dispatcher view) ----
export type Lane = 'HIGH' | 'UNCERTAIN' | 'LOW'
export interface FaContribution {
  feature: string
  evidence: number
  weight: number
  contribution: number
  status: string
  note: string | null
}
export interface FaView {
  incident_id: string
  status: string
  created_at: string
  live_at: string | null
  lane: Lane
  probability_genuine: number | null
  base_lane: Lane | null
  model_version: string | null
  config_hash: string | null
  bias: number | null
  logit: number | null
  contributions: FaContribution[]
  top: FaContribution[]
  missing: string[]
  overrides: string[]
  place_type: string | null
  corroborating: string[]
  mic_consent: boolean
  checkin: { state: string; reason: string; prompt_n: number; volume: number; last_prompt_at: string | null; next_in_s: number | null; response: string | null }
  hold_remaining_s: number | null
  dispatch_started: boolean
  recalled?: boolean
  awaiting_review: boolean
  followup_alert: boolean
  tag: { outcome: string; reason: string; by: string; at: string } | null
  close_reason: string | null
}