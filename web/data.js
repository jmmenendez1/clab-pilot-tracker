/* Data layer. Two adapters with the same interface:
 *   - SupabaseAdapter: production (PostgreSQL + Auth on Supabase)
 *   - LocalAdapter:    in-browser store (localStorage) for development and demos; ?local=1
 * All methods return plain objects shaped like the database rows.
 */

const STAGES = ["ideation", "design", "implementation", "evaluation"];

function uuid() {
  return crypto.randomUUID ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
      });
}

/* ------------------------------------------------------------------ */
class LocalAdapter {
  constructor() {
    this.key = "clab_local_db";
    this.db = JSON.parse(localStorage.getItem(this.key) || "null") ||
      { pilots: [], hypotheses: [], responses: [], completions: [], seq: 1 };
    this.user = { id: "00000000-0000-4000-8000-000000000001", email: "dev@local", full_name: "Local developer", role: "admin" };
    this.mode = "local";
  }
  _save() { localStorage.setItem(this.key, JSON.stringify(this.db)); }

  async init() { return this.user; }
  async signIn() { return { ok: true }; }
  async signOut() {}
  currentUser() { return this.user; }
  onAuthChange() {}

  async listPilots() {
    return this.db.pilots.filter(p => !p.archived).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  }
  async getPilot(id) { return this.db.pilots.find(p => p.id === id) || null; }
  async createPilot(fields) {
    const now = new Date().toISOString();
    const p = { id: uuid(), current_stage: "ideation", archived: false, created_by: this.user.id, created_at: now, updated_at: now, ...fields };
    this.db.pilots.push(p); this._save(); return p;
  }
  async updatePilot(id, fields) {
    const p = await this.getPilot(id); Object.assign(p, fields, { updated_at: new Date().toISOString(), updated_by: this.user.id });
    this._save(); return p;
  }

  async listHypotheses(pilotId) { return this.db.hypotheses.filter(h => h.pilot_id === pilotId && !h.archived).sort((a, b) => a.ordinal - b.ordinal); }
  async addHypothesis(pilotId) {
    const all = this.db.hypotheses.filter(h => h.pilot_id === pilotId);
    const h = { id: uuid(), pilot_id: pilotId, ordinal: all.length + 1, archived: false, created_by: this.user.id, created_at: new Date().toISOString() };
    this.db.hypotheses.push(h); this._save(); return h;
  }
  async archiveHypothesis(id) { const h = this.db.hypotheses.find(x => x.id === id); h.archived = true; this._save(); }

  async currentResponses(pilotId) {
    const rows = this.db.responses.filter(r => r.pilot_id === pilotId);
    const latest = new Map();
    for (const r of rows) {
      const k = `${r.dimension_code}|${r.field_key}|${r.hypothesis_id || ""}`;
      const prev = latest.get(k);
      if (!prev || r.created_at > prev.created_at || (r.created_at === prev.created_at && r.id > prev.id)) latest.set(k, r);
    }
    return [...latest.values()].map(r => ({ ...r, author: this.user }));
  }
  async saveResponse(row) {
    const r = { id: this.db.seq++, author_id: this.user.id, created_at: new Date().toISOString(), ...row };
    this.db.responses.push(r); this._save(); return { ...r, author: this.user };
  }
  async history(pilotId, dimension, field, hypothesisId) {
    return this.db.responses.filter(r => r.pilot_id === pilotId && r.dimension_code === dimension && r.field_key === field && (r.hypothesis_id || null) === (hypothesisId || null))
      .map(r => ({ ...r, author: this.user })).sort((a, b) => b.created_at.localeCompare(a.created_at));
  }

  async listCompletions(pilotId) { return this.db.completions.filter(c => c.pilot_id === pilotId).map(c => ({ ...c, completer: this.user })); }
  async completeStage(pilotId, stage, snapshot) {
    const c = { id: this.db.seq++, pilot_id: pilotId, stage, snapshot, completed_by: this.user.id, completed_at: new Date().toISOString() };
    this.db.completions.push(c); this._save(); return c;
  }
}

/* ------------------------------------------------------------------ */
class SupabaseAdapter {
  constructor(cfg) {
    this.sb = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
    this.user = null;
    this.mode = "supabase";
  }
  async init() {
    const { data } = await this.sb.auth.getSession();
    if (data.session) await this._loadProfile(data.session.user);
    return this.user;
  }
  onAuthChange(cb) {
    this.sb.auth.onAuthStateChange(async (_evt, session) => {
      if (session) await this._loadProfile(session.user); else this.user = null;
      cb(this.user);
    });
  }
  async _loadProfile(authUser) {
    const { data, error } = await this.sb.from("users").select("*").eq("id", authUser.id).maybeSingle();
    if (error || !data) { this.user = { id: authUser.id, email: authUser.email, full_name: authUser.email, role: null, unauthorised: true }; return; }
    this.user = data;
  }
  async signIn(email) {
    const { error } = await this.sb.auth.signInWithOtp({ email, options: { emailRedirectTo: window.location.origin + window.location.pathname } });
    if (error) return { ok: false, message: error.message };
    return { ok: true };
  }
  async signOut() { await this.sb.auth.signOut(); this.user = null; }
  currentUser() { return this.user; }

  _throw(error) { if (error) throw new Error(error.message || String(error)); }

  async listPilots() {
    const { data, error } = await this.sb.from("pilots").select("*").eq("archived", false).order("updated_at", { ascending: false });
    this._throw(error); return data;
  }
  async getPilot(id) { const { data, error } = await this.sb.from("pilots").select("*").eq("id", id).maybeSingle(); this._throw(error); return data; }
  async createPilot(fields) { const { data, error } = await this.sb.from("pilots").insert(fields).select().single(); this._throw(error); return data; }
  async updatePilot(id, fields) { const { data, error } = await this.sb.from("pilots").update(fields).eq("id", id).select().single(); this._throw(error); return data; }

  async listHypotheses(pilotId) {
    const { data, error } = await this.sb.from("hypotheses").select("*").eq("pilot_id", pilotId).eq("archived", false).order("ordinal"); this._throw(error); return data;
  }
  async addHypothesis(pilotId) {
    const { data: all, error: e1 } = await this.sb.from("hypotheses").select("ordinal").eq("pilot_id", pilotId); this._throw(e1);
    const ordinal = all.length ? Math.max(...all.map(h => h.ordinal)) + 1 : 1;
    const { data, error } = await this.sb.from("hypotheses").insert({ pilot_id: pilotId, ordinal }).select().single(); this._throw(error); return data;
  }
  async archiveHypothesis(id) { const { error } = await this.sb.from("hypotheses").update({ archived: true }).eq("id", id); this._throw(error); }

  async currentResponses(pilotId) {
    const { data, error } = await this.sb.from("responses_current").select("*, author:users!responses_author_id_fkey(id,full_name,email)").eq("pilot_id", pilotId);
    if (error) { // the view may not expose the FK for embedding; fall back to a second query
      const { data: rows, error: e2 } = await this.sb.from("responses_current").select("*").eq("pilot_id", pilotId); this._throw(e2);
      return this._attachAuthors(rows);
    }
    return data;
  }
  async _attachAuthors(rows) {
    const ids = [...new Set(rows.map(r => r.author_id))];
    if (!ids.length) return rows;
    const { data: users } = await this.sb.from("users").select("id,full_name,email").in("id", ids);
    const map = new Map((users || []).map(u => [u.id, u]));
    return rows.map(r => ({ ...r, author: map.get(r.author_id) }));
  }
  async saveResponse(row) {
    const { data, error } = await this.sb.from("responses").insert({ ...row, author_id: this.user.id }).select().single(); this._throw(error);
    return { ...data, author: this.user };
  }
  async history(pilotId, dimension, field, hypothesisId) {
    let q = this.sb.from("responses").select("*").eq("pilot_id", pilotId).eq("dimension_code", dimension).eq("field_key", field).order("created_at", { ascending: false });
    q = hypothesisId ? q.eq("hypothesis_id", hypothesisId) : q.is("hypothesis_id", null);
    const { data, error } = await q; this._throw(error); return this._attachAuthors(data);
  }

  async listCompletions(pilotId) {
    const { data, error } = await this.sb.from("stage_completions").select("*").eq("pilot_id", pilotId).order("completed_at", { ascending: false }); this._throw(error);
    const ids = [...new Set(data.map(c => c.completed_by))];
    const { data: users } = ids.length ? await this.sb.from("users").select("id,full_name,email").in("id", ids) : { data: [] };
    const map = new Map((users || []).map(u => [u.id, u]));
    return data.map(c => ({ ...c, completer: map.get(c.completed_by) }));
  }
  async completeStage(pilotId, stage, snapshot) {
    const { data, error } = await this.sb.from("stage_completions").insert({ pilot_id: pilotId, stage, snapshot, completed_by: this.user.id }).select().single(); this._throw(error); return data;
  }
}

/* ------------------------------------------------------------------ */
function createDataLayer() {
  const params = new URLSearchParams(window.location.search);
  const forceLocal = params.get("local") === "1" || localStorage.getItem("clab_mode") === "local";
  if (!forceLocal && window.CLAB_CONFIG && window.supabase) return new SupabaseAdapter(window.CLAB_CONFIG);
  return new LocalAdapter();
}

window.CLAB = Object.assign(window.CLAB || {}, { createDataLayer, STAGES, uuid });
