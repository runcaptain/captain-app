// Team metadata is separate from the selected bucket workspace. Credentials stay in the daemon.
import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme } from "herdr";
import { api } from "./kit/cst.js";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, plainError } from "./kit/ui.js";

export default class CaptainTeam extends View {
  init(_props, cx) {
    this.connection = null; this.authRejected = false; this.records = []; this.drafts = []; this.busy = false;
    this.error = null; this.message = null; this.login = null; this.selected = null;
    this.editConnection = false;
    this.editorRecordId = null; this.editorRevision = 0;
    this.editorHistory = null;
    this.serverInput = InputState.new({ placeholder: 'https://team.example.com' });
    this.workspaceInput = InputState.new({ placeholder: 'Workspace name' });
    this.tokenInput = InputState.new({ placeholder: 'Team access token' });
    this.tokenInput.set_masked(true);
    this.id = InputState.new({ placeholder: "Record ID" });
    this.body = InputState.new({ placeholder: 'Record JSON, for example {"title":"Search review"}' });
    this.body.set_value('{}'); this.visibility = 'private'; this.kind = 'investigation';
    this.refresh(cx);
    cx.timer.every(1000, (cx) => {
      if (!this.connection && !this.busy) { this.refresh(cx); return; }
      if (!this.login || this.busy || Date.now() < this.nextPoll) return;
      if (Date.now() >= this.loginExpires) { this.login = null; this.error = 'Sign-in expired. Start again.'; cx.notify(); return; }
      this.perform(cx, async () => {
        const r = await api('/api/team/login/poll', { method: 'POST', scoped: false });
        if (!r.ok) { this.login = null; return r; }
        if (r.data.state === 'authenticated') {
          this.login = null; this.editConnection = false; this.authRejected = false; this.message = 'Signed in. Pull to load team records.';
          await this.load();
        } else this.nextPoll = Date.now() + Math.max(1, r.data.interval ?? this.login.interval ?? 5) * 1000;
        return r;
      });
    });
  }
  async load() {
    const c = await api('/api/team/connection', { scoped: false });
    if (!c.ok) return c;
    if (!this.serverInput.value() && c.data.server) this.serverInput.set_value(c.data.server);
    if (!this.workspaceInput.value() && c.data.workspace) this.workspaceInput.set_value(c.data.workspace);
    this.connection = { ...c.data, authenticated: c.data.authenticated && !this.authRejected };
    if (!this.connection.authenticated) { this.records = []; this.drafts = []; return c; }
    return this.loadRecords();
  }
  async loadRecords() {
    const r = await api('/api/team/records', { scoped: false });
    if (r.ok) {
      if (this.historyId !== undefined && this.historyId !== r.data.history_id) {
        this.selected = null; this.editorRecordId = null; this.editorRevision = 0;
        this.id.set_value(''); this.body.set_value('{}');
        this.message = 'Server history changed. The open editor was cleared. Select the current record and review retained drafts.';
        r.historyReset = true;
      }
      this.historyId = r.data.history_id;
      if (r.historyReset || this.editorHistory == null) this.editorHistory = this.historyId;
      this.records = r.data.records; this.drafts = r.data.drafts;
    }
    return r;
  }
  perform(cx, work) {
    if (this.busy) return;
    this.busy = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await work();
        if (!r?.ok && !r?.starting) {
          const rejection = r?.error?.code ? r.error : r?.data?.error ?? r?.data?.detail;
          const writeDenied = r?.status === 403 && rejection?.code === 'write_denied';
          this.error = writeDenied ? rejection.message : plainError(r?.error, 'Team request failed. Retry when connected.');
          if (!writeDenied && (r?.status === 401 || r?.status === 403)) {
            this.authRejected = true;
            if (this.connection) this.connection.authenticated = false;
            this.records = []; this.drafts = []; this.selected = null; this.body.set_value('{}');
            this.editorRecordId = null; this.editorRevision = 0;
            this.message = r.status === 401
              ? 'Sign in again. Your local drafts are preserved until access is verified.'
              : 'Access was revoked. Cached records and local drafts were cleared.';
          }
        }
      } catch { this.error = 'Team request failed. Retry when connected.'; }
      finally { this.busy = false; cx.notify(); }
    });
  }
  refresh(cx) { this.perform(cx, () => this.load()); }
  sync(cx, action) {
    this.perform(cx, async () => {
      const r = await api(`/api/team/${action}`, { method: 'POST', scoped: false });
      if (!r.ok) return r;
      const loaded = await this.loadRecords();
      if (!loaded.historyReset) this.message = action === 'push' ? 'Push finished. Conflicting drafts still need review.' : 'Team records refreshed.';
      return loaded;
    });
  }
  connect(cx) {
    if (this.busy) { this.tokenInput.set_value(''); return; }
    const server = this.serverInput.value().trim();
    const workspace = this.workspaceInput.value().trim();
    let token = this.tokenInput.value().trim();
    this.tokenInput.set_value('');
    if (!server || !workspace || !token) {
      token = ''; this.error = 'Enter your server URL, workspace and token.'; cx.notify(); return;
    }
    this.perform(cx, async () => {
      try {
        const r = await api('/api/team/connect', { method: 'POST', body: { server, workspace, token }, scoped: false });
        if (!r.ok) return { ok: false, error: 'Could not connect. Check your team details, token and connection, then try again.' };
        this.authRejected = false; this.editConnection = false; this.login = null;
        this.selected = null; this.id.set_value(''); this.body.set_value('{}');
        this.editorRecordId = null; this.editorRevision = 0;
        this.historyId = undefined; this.editorHistory = null;
        this.records = []; this.drafts = [];
        this.message = 'Connected to your team.';
        return await this.load();
      } catch { return { ok: false, error: 'Could not connect. Check your team details, token and connection, then try again.' }; }
      finally { token = ''; this.tokenInput.set_value(''); }
    });
  }
  signOut(cx) {
    this.tokenInput.set_value('');
    if (this.busy) return;
    this.records = []; this.drafts = []; this.selected = null;
    this.editorRecordId = null; this.editorRevision = 0;
    this.id.set_value(''); this.body.set_value('{}'); this.login = null;
    this.authRejected = true; this.logoutPending = true;
    if (this.connection) this.connection.authenticated = false;
    this.perform(cx, async () => {
      try {
        const r = await api('/api/team/logout', { method: 'POST', scoped: false });
        if (!r.ok) return { ok: false, error: 'Signed out of this view. Credential cleanup failed. Retry sign out.' };
        this.logoutPending = false; this.authRejected = false;
        this.message = r.data?.state === 'environment_configured'
          ? 'The operator-configured credential remains active. Ask your operator to remove it to disconnect.' : 'Signed out.';
        return await this.load();
      } catch { return { ok: false, error: 'Signed out of this view. Credential cleanup failed. Retry sign out.' }; }
    });
  }
  signIn(cx) {
    this.tokenInput.set_value('');
    this.perform(cx, async () => {
      const r = await api('/api/team/login/start', { method: 'POST', scoped: false });
      if (r.ok) {
        this.login = r.data;
        this.nextPoll = Date.now() + Math.max(1, r.data.interval ?? 5) * 1000;
        this.loginExpires = Date.now() + r.data.expires_in * 1000;
      }
      return r;
    });
  }
  select(record, draft = null) {
    this.selected = draft; this.id.set_value(record.id ?? record.record_id);
    this.editorRecordId = record.id ?? record.record_id;
    this.editorHistory = this.historyId;
    // Capture the version being reviewed. Refreshing the cache must not rebase an open editor.
    const current = this.records.find(r => r.id === this.editorRecordId);
    this.editorRevision = draft?.state === 'conflict'
      ? current?.revision ?? record.expected_revision ?? 0
      : record.revision ?? record.expected_revision ?? 0;
    this.kind = record.kind; this.visibility = record.visibility;
    this.body.set_value(JSON.stringify(record.body)); this.error = null; this.message = null;
  }
  save(cx) {
    if (this.drafts.some(d => d.state === 'pending' && d.mutation.record_id === this.id.value().trim())) { this.error = 'Push or discard this pending draft before saving another edit.'; cx.notify(); return; }
    let body;
    try { body = JSON.parse(this.body.value()); if (!body || Array.isArray(body) || typeof body !== 'object') throw Error(); }
    catch { this.error = 'Enter a JSON object.'; cx.notify(); return; }
    const id = this.id.value().trim();
    if (!id) { this.error = 'Give this record an ID.'; cx.notify(); return; }
    if (this.selected?.state === 'conflict' && id !== this.selected.mutation.record_id) {
      this.error = 'Resolve this draft with its original record ID. Use New record to create a different record.';
      cx.notify(); return;
    }
    const expectedRevision = id === this.editorRecordId ? this.editorRevision : 0;
    const expectedHistory = this.editorHistory;
    this.perform(cx, async () => {
      const conflict = this.selected?.state === 'conflict';
      const path = conflict ? `/api/team/drafts/${encodeURIComponent(this.selected.mutation.mutation_id)}/resolve` : `/api/team/records/${encodeURIComponent(id)}/draft`;
      const r = await api(path, { method: conflict ? 'POST' : 'PUT', body: { body, kind: this.kind, visibility: this.visibility, expected_revision: expectedRevision, expected_history: expectedHistory }, scoped: false });
      if (!r.ok) return r;
      this.selected = null; this.message = 'Draft saved locally. Push when ready to share.';
      return this.loadRecords();
    });
  }
  discard(cx) {
    if (!this.selected) return;
    const id = this.selected.mutation.mutation_id;
    this.perform(cx, async () => {
      const r = await api(`/api/team/drafts/${encodeURIComponent(id)}`, { method: 'DELETE', scoped: false });
      if (!r.ok) return r;
      this.selected = null;
      this.message = 'Local draft discarded. Published records are unchanged. You can save a new draft.';
      return this.loadRecords();
    });
  }
  input(c, id, state) {
    return div().id(id).h(36).px(10).flex().items_center().border_1().border_color(c.grey300).bg(c.surface)
      .child(Input.new(state).w_full().text_size(13).text_color(c.navy));
  }
  render() {
    const c = palette(isDark(theme()));
    const line = (s) => div().text_size(12.5).text_color(c.grey500).child(s);
    const button = (id, label, fn) => Button(c, id, label, fn, { disabled: this.busy });
    return v_flex().id('team').size_full().p(20).gap(12).bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().gap(10).items_center().child(div().flex_1().text_size(20).font_semibold().child('Team collaboration'))
        .child(button('team-refresh', 'Refresh', (_e, cx) => this.refresh(cx))))
      .child(line(this.connection?.server ? `${this.connection.workspace} · ${this.connection.server}` : this.connection ? 'Connect to your team to begin.' : 'Connecting to Captain services...' ))
      .child(line('This team workspace is independent of your selected bucket workspace.'))
      .when(!!this.error, (e) => e.child(div().text_size(13).text_color(c.fail).child(this.error)))
      .when(!!this.message, (e) => e.child(line(this.message)))
      .when(!!this.connection && (!this.connection.authenticated || this.editConnection), (e) => e
        .child(v_flex().max_w(560).gap(8)
          .child(div().text_size(16).font_semibold().child('Connect to your team'))
          .child(line('Use the server URL, workspace and token from your team operator.'))
          .when(!!this.logoutPending, (e) => e.child(button('team-logout-retry', 'Retry sign out', (_e, cx) => this.signOut(cx))))
          .child(line('Server URL')).child(this.input(c, 'team-connect-server', this.serverInput))
          .child(line('Workspace')).child(this.input(c, 'team-connect-workspace', this.workspaceInput))
          .child(line('Access token')).child(this.input(c, 'team-connect-token', this.tokenInput))
          .child(button('team-connect', 'Connect', (_e, cx) => this.connect(cx)))
          .when(!!this.connection.authenticated, (e) => e.child(button('team-connect-cancel', 'Cancel', (_e, cx) => { this.tokenInput.set_value(''); this.editConnection = false; cx.notify(); })))
          .when(!!this.connection.login_available && !this.login, (e) => e.child(button('team-login', 'Sign in with company SSO', (_e, cx) => this.signIn(cx))))
          .when(!!this.login, (e) => e.child(v_flex().gap(8)
            .child(line(`Enter code: ${this.login.user_code}`)).child(line(this.login.verification_uri))
            .child(button('team-login-open', 'Open sign-in page', (_e, cx) => cx.open_url(this.login.verification_uri)))
            .child(line('Waiting for sign-in in your browser.'))))))
      .when(!!this.connection?.authenticated && !this.editConnection, (e) => e
        .child(h_flex().gap(8)
          .child(button('team-pull', 'Pull latest', (_e, cx) => this.sync(cx, 'pull')))
          .child(button('team-push', 'Push drafts', (_e, cx) => this.sync(cx, 'push')))
          .child(button('team-connection-settings', 'Connection settings', (_e, cx) => { this.editConnection = true; this.tokenInput.set_value(''); cx.notify(); }))
          .child(button('team-logout', 'Sign out', (_e, cx) => this.signOut(cx))))
        .child(h_flex().flex_1().min_h(0).gap(16).items_start()
          .child(v_flex().w(250).gap(6).id('team-list').overflow_y_scroll()
            .child(line(`${this.records.length} cached records · ${this.drafts.length} local drafts`))
            .children(this.records.slice(0, 200).map((r, i) => button(`team-record-${i}`, div().min_w(0).truncate().child(`${r.body?.title ?? r.id} (${r.visibility})`), (_e, cx) => { this.select(r); cx.notify(); })))
            .children(this.drafts.slice(0, 200).map((d, i) => button(`team-draft-${i}`, div().min_w(0).truncate().child(`${d.mutation.record_id}: ${d.state}`), (_e, cx) => { this.select(d.mutation, d); cx.notify(); })))
            .when(this.records.length > 200 || this.drafts.length > 200, (e) => e.child(line('Showing the first 200 of each. Use the CLI for the full list.'))))
          .child(v_flex().flex_1().min_w(0).gap(8)
            .child(button('team-new', 'New record', (_e, cx) => { this.selected = null; this.editorRecordId = null; this.editorRevision = 0; this.editorHistory = this.historyId; this.id.set_value(''); this.body.set_value('{}'); this.kind = 'investigation'; this.visibility = 'private'; cx.notify(); }))
            .child(this.input(c, 'team-record-id', this.id))
            .child(line(`Kind: ${this.kind}`))
            .child(this.input(c, 'team-record-body', this.body))
            .when(!this.selected, (e) => e.child(h_flex().gap(8).child(button('team-private', this.visibility === 'private' ? 'Private (selected)' : 'Private', (_e, cx) => { this.visibility = 'private'; cx.notify(); }))
              .child(button('team-shared', this.visibility === 'workspace' ? 'Team (selected)' : 'Team', (_e, cx) => { this.visibility = 'workspace'; cx.notify(); }))))
            .when(this.selected?.state === 'conflict', (e) => e.child(line(
              this.selected.error === 'server_history_changed' && !this.records.some(r => r.id === this.selected.mutation.record_id)
                ? 'Server history changed and this record is absent. Copy your proposal, discard this draft, then use New record to recreate it.'
                : 'Conflict: pull latest, compare the current record, then review this proposal before resolving.')))
            .when(this.selected?.state === 'pending', (e) => e.child(line('Pending draft: push it, or discard it locally before saving another edit.')))
            .child(Button(c, 'team-save', this.selected?.state === 'conflict' ? 'Save reviewed resolution' : 'Save local draft', (_e, cx) => this.save(cx), { disabled: this.busy || this.selected?.state === 'pending' }))
            .when(!!this.selected, (e) => e
              .child(button('team-discard', 'Discard local draft', (_e, cx) => this.discard(cx)))
              .child(line('Discard removes only this local draft. It cannot undo published changes.')))
            .child(line('Saving keeps a local draft. Only Push sends it to the team server.')))));
  }
}
