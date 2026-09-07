<script lang="ts">
  type Job = { id: string; campaign_name: string; kind: string; status: string; message?: string; created_at: number };
  type Campaign = { id: string; name: string; version: number; updated_at: number };
  let jobs: Job[] = $state([]);
  let campaigns: Campaign[] = $state([]);
  let selected: Campaign | null = $state(null);
  let detail: any = $state(null);
  let notice = $state('');
  let newName = $state('');
  let campaignYaml = $state('');
  let scenarioYaml = $state('');
  let busy = $state(false);
  let result: any = $state(null);
  let runtimeCap = $state('');

  function cookie(name: string) { return document.cookie.split('; ').find((item) => item.startsWith(name + '='))?.split('=')[1] || ''; }

  async function api(path: string, options: RequestInit = {}) {
    const headers = new Headers(options.headers); headers.set('content-type', 'application/json');
    if (options.method && options.method !== 'GET') headers.set('x-csrf-token', decodeURIComponent(cookie('elengtis_csrf')));
    const response = await fetch('/api/v1' + path, { ...options, credentials: 'same-origin', headers });
    const json = response.headers.get('content-type')?.includes('application/json');
    if (!response.ok || !json) {
      const body = await response.text();
      const detail = json ? JSON.parse(body).detail : 'Workbench API is not running; this is a frontend-only preview.';
      throw new Error(detail || response.statusText);
    }
    return response.json();
  }
  async function refresh() {
    try { const data = await api('/activity'); campaigns = data.campaigns; }
    catch (error) { notice = String(error); }
  }
  async function openCampaign(campaign: Campaign) { selected = campaign; detail = await api(`/campaigns/${campaign.id}`); campaignYaml = detail.campaign_yaml; }
  async function openResult(job: Job) { result = await api(`/jobs/${job.id}/result`); }
  async function queue(kind: string) {
    if (!selected) return;
    const runtime_cap_usd = kind === 'run' ? Number(runtimeCap) : undefined;
    if (kind === 'run' && (!runtime_cap_usd || runtime_cap_usd <= 0)) throw new Error('Enter a positive runtime cap before queuing a live run');
    await api(`/campaigns/${selected.id}/jobs`, { method: 'POST', body: JSON.stringify({ kind, runtime_cap_usd }) });
    notice = `${kind} queued`; await refresh();
  }
  async function createCampaign() {
    busy = true;
    try {
      const created = await api('/campaigns', { method: 'POST', body: JSON.stringify({ name: newName, campaign_yaml: campaignYaml, scenarios: { 'scenario.yaml': scenarioYaml } }) });
      notice = 'Campaign created'; newName = ''; await refresh(); await openCampaign(created);
    } catch (error) { notice = String(error); } finally { busy = false; }
  }
  function formatTime(value: number) { return new Date(value * 1000).toLocaleString(); }
  refresh();
</script>

<svelte:head><title>Elengtis Workbench</title></svelte:head>

<div class="shell">
  <aside class="sidebar">
    <div class="brand"><span class="mark">E</span><span>elengtis</span></div>
    <nav aria-label="Primary navigation">
      <a class="active" href="#activity">Activity</a><a href="#campaigns">Campaigns</a><a href="#scenarios">Scenario library</a><a href="#analyses">Analyses</a>
    </nav>
    <div class="sidebar-note"><strong>Evidence-led</strong><br />A benchmark result is evidence about a configured scenario, not a security guarantee.</div>
  </aside>
  <main>
    <header class="topbar"><div><p class="eyebrow">Security lab workbench</p><h1>Activity</h1></div><button class="quiet" onclick={refresh}>Refresh</button></header>
    {#if notice}<div class="notice" role="status">{notice}</div>{/if}
    <section class="overview" id="activity">
      <div><span class="label">Queued or running</span><strong>{jobs.filter((job) => ['queued', 'running'].includes(job.status)).length}</strong></div>
      <div><span class="label">Campaigns</span><strong>{campaigns.length}</strong></div>
      <div><span class="label">Latest state</span><strong>{jobs[0]?.status || 'quiet'}</strong></div>
    </section>
    <section class="work-grid">
      <div class="panel" id="campaigns"><div class="panel-heading"><div><p class="eyebrow">Library</p><h2>Campaigns</h2></div></div>
        {#if campaigns.length === 0}<p class="empty">No campaigns yet. Create one below or import an existing definition.</p>{/if}
        {#each campaigns as campaign}<button class:selected={selected?.id === campaign.id} class="campaign-row" onclick={() => openCampaign(campaign)}><span><strong>{campaign.name}</strong><small>Revision {campaign.version}</small></span><time>{formatTime(campaign.updated_at)}</time></button>{/each}
      </div>
      <div class="panel"><div class="panel-heading"><div><p class="eyebrow">Queue</p><h2>Recent jobs</h2></div></div>
        {#if jobs.length === 0}<p class="empty">Jobs will appear here after validation or a run.</p>{/if}
        {#each jobs as job}<button class="job-row" onclick={() => openResult(job)}><span class="status-dot {job.status}"></span><span><strong>{job.kind} · {job.campaign_name}</strong><small>{job.status} · {formatTime(job.created_at)}</small></span></button>{/each}
      </div>
    </section>
    <section class="panel editor" id="new-campaign"><div class="panel-heading"><div><p class="eyebrow">Author</p><h2>New campaign</h2></div></div>
      <div class="form-grid"><label>Name<input bind:value={newName} placeholder="Printer support benchmark" /></label><label>Campaign YAML<textarea bind:value={campaignYaml} rows="12" placeholder="schema_version: 1"></textarea></label><label>Scenario YAML<textarea bind:value={scenarioYaml} rows="12" placeholder="schema_version: 1"></textarea></label></div>
      <button class="primary" disabled={busy || !newName || !campaignYaml || !scenarioYaml} onclick={createCampaign}>Save campaign</button>
    </section>
    {#if selected && detail}<section class="panel detail"><div class="panel-heading"><div><p class="eyebrow">Selected campaign</p><h2>{selected.name}</h2></div><div class="actions"><button class="quiet" onclick={() => queue('preflight')}>Preflight</button><label>Runtime cap (USD)<input bind:value={runtimeCap} inputmode="decimal" placeholder="Required for live runs" /></label><button class="primary" onclick={() => queue('run')}>Queue run</button></div></div><pre>{detail.campaign_yaml}</pre></section>{/if}
    {#if result}<section class="panel detail"><div class="panel-heading"><div><p class="eyebrow">Evidence</p><h2>{result.rows.length} recorded attempts</h2></div></div><div class="table-wrap"><table><thead><tr><th>Trial</th><th>Proposal</th><th>Verified</th><th>Termination</th></tr></thead><tbody>{#each result.rows as row}<tr><td>{row.trial_id}</td><td>{String(row.proposed ?? 'unknown')}</td><td>{String(row.completed ?? 'unknown')}</td><td>{row.termination}</td></tr>{/each}</tbody></table></div>{#each result.evidence as item}<details><summary>{item.trial_id} · raw evidence</summary><pre>{JSON.stringify(item.document, null, 2)}</pre></details>{/each}</section>{/if}
  </main>
</div>
