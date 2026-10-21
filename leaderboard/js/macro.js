/* The macrobench: agent workflows timed end to end. One row per system and machine, one column per workflow, ranked
   by one workflow's completion time within one comparison group; one details dialog per row; and the workflow
   definitions drawn as branch histories. Every campaign here passed validation in the build, so a row carries no
   caveat: each workflow either completed in a time or ended with its own status. */
const MACRO = {
    campaigns: typeof macroCampaigns === 'undefined' ? [] : macroCampaigns,
    excluded: typeof macroExcluded === 'undefined' ? [] : macroExcluded,
    verbs: typeof verbs === 'undefined' ? [] : verbs,
    verbsExcluded: typeof verbsExcluded === 'undefined' ? [] : verbsExcluded,
    built: typeof macroSuite !== 'undefined',
};
const SCENARIOS = MACRO.built && macroSuite.scenarios ? macroSuite.scenarios : {rl_env: 'S1 RL environment', context_mgmt: 'S2 Context management', multi_agent: 'S3 Multi-agent', dev_agent: 'S4 Dev agent', ops_agent: 'S5 Ops agent', data_agent: 'S6 Data agent'};
// `alt` is the full text of each branch history. It is written from the scenario's source, not
// from any attempt, so it never carries timings, hashes or measured counts. The same text names
// the Canvas scene and the PNG that stands in for it without Canvas.
const WORKFLOWS = {
    rl_env: {name: 'Evaluate agent rollouts', caption: 'Try several repair paths from a faulty task state, compare their results, then discard the branches.',
        ops: ['branch from a recorded commit', 'commit checkpoints', 'read across branches', 'diff', 'delete'],
        alt: 'main keeps only its seeded commits; a task branch leaves the current main commit and commits the injected fault; repair paths A and B each branch from that same task commit in turn, a representative example since later rollouts may fork recorded history of earlier rollouts, and commit checkpoints as configured; every repair tip is checked across branches and diffed against the task commit; the task and repair branches are deleted without merging.'},
    context_mgmt: {name: 'Manage context history', caption: 'Compare context-compaction strategies, adopt one, and later replace a rejected choice with a retained alternative.',
        ops: ['branch', 'commit', 'fast-forward merge', 'revert', 'rebase', 'delete'],
        alt: 'main commits the interactions and a checkpoint; a selected candidate and a retained alternative each branch from that checkpoint in turn and commit one compaction; main fast-forwards to the selected candidate, so its ref moves without a merge commit, and later work commits follow; that compaction is later reverted on main by a new commit; main is then rebased onto the retained alternative, drawn as its later work replayed after the retained compaction although a backend may keep, replay or drop the reverted pair differently, and the adopt call that follows adds no merge commit in the Git-style path drawn here, although another backend may integrate it differently; the candidate branches are deleted after their retention window.'},
    multi_agent: {name: 'Coordinate multiple agents', caption: 'Agents revise the same plan independently and integrate their changes one at a time under a fixed conflict policy.',
        ops: ['branch', 'commit', 'log', 'merge under a fixed conflict policy', 'delete'],
        alt: 'agents A and B branch from the same shared checkpoint on main and commit their revisions independently while main keeps receiving updates that are not committed one by one; each agent branch history and main are read; the agent branches merge into main one at a time under the fixed conflict policy, A then B being a representative order for a configurable number of agents, and how many merge commits that leaves depends on the backend; the agent branches are deleted after integration.'},
    dev_agent: {name: 'Develop against live data', caption: 'Build and backfill a feature while production changes, refresh the branch by rebasing, then review and discard it.',
        ops: ['branch', 'commit', 'rebase with conflicts', 'delete without merging'],
        alt: 'production keeps committing writes and scheduled schema migrations; a feature branch leaves a production commit and commits its schema and backfill; at the end of development, and optionally midway, it is rebased onto the moving production branch, so its commits are replayed after the newest production commit and the branch no longer points at the earlier ones; a quiet review follows, with optional edits committed; the feature branch is deleted without merging.'},
    ops_agent: {name: 'Recover from a bad deployment', caption: 'Investigate earlier production checkpoints and attempt recovery by resetting to a selected checkpoint.',
        ops: ['commit', 'branch at a historical commit', 'diff', 'reset', 'delete'],
        alt: 'production commits checkpoints under load until a bad deployment corrupts the head; investigation branches open at earlier checkpoints, each runs the consistency checks, diffs against the corrupted head and commits its findings; production is reset to the selected investigated checkpoint, so its ref moves back along its own history to that original checkpoint, not to a findings commit, without a new commit; the investigation branches are deleted.'},
    data_agent: {name: 'Ingest and merge data', caption: 'Validate an ingestion batch in isolation, retry from a checkpoint when needed, then rebase and merge it into the warehouse.',
        ops: ['branch', 'commit', 'reset, or branch from the checkpoint', 'rebase', 'merge', 'delete'],
        alt: 'a batch branch leaves the warehouse branch and ingests, validates and commits step by step while the warehouse keeps taking analytical load; a step may optionally be retried by reset to an earlier commit, or by a new branch from that historical commit where reset is unsupported, and the batch is redone from there; with the analytical load paused, the batch is rebased onto the warehouse, so its commits are replayed after the newest warehouse commit, and merged into it, which moves the warehouse ref to the batch tip by fast-forward unless the backend needs a three-way merge; batches integrate one at a time and the batch branch is deleted.'},
};
const workflow = key => WORKFLOWS[key] || {name: SCENARIOS[key]};
// Status icons are drawn: a filled disc and one stroke glyph, so the label never rests on color alone. The first four
// are a workflow's outcomes; not_applicable marks a correctness check that did not apply.
const STATUS = {
    verified: {label: 'Completed', glyph: ['M4.7 8.3l2.2 2.2 4.4-4.6']}, incomplete: {label: 'Incomplete', glyph: [], half: true}, failed: {label: 'Failed', glyph: ['M5.6 5.6l4.8 4.8m0-4.8l-4.8 4.8']},
    timed_out: {label: 'Timed out', glyph: ['M8 5.2V8l1.9 1.3'], ring: true}, not_applicable: {label: 'Not applicable', glyph: ['M5.2 8h5.6']},
};
const STATUS_RULES = {
    verified: 'Every planned work unit succeeded, every applicable check passed, nothing was skipped or unsupported. Only this state has a time.',
    incomplete: 'Work was skipped or degraded.', failed: 'Setup, a worker, an operation or a correctness check failed.',
    timed_out: 'The deadline ended the attempt; its partial time is in the details only.',
};
function statusIcon(status) {
    const info = STATUS[status], svg = svgNode('svg', {class: 'status-icon', viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false'});
    svg.append(svgNode('circle', {cx: 8, cy: 8, r: 8, class: 'status-disc'}));
    if (info.half) svg.append(svgNode('circle', {cx: 8, cy: 8, r: 4.2, class: 'status-glyph'}), svgNode('path', {d: 'M8 3.8a4.2 4.2 0 0 0 0 8.4z', class: 'status-fill'}));
    if (info.ring) svg.append(svgNode('circle', {cx: 8, cy: 8, r: 4.6, class: 'status-glyph'}));
    for (const d of info.glyph) svg.append(svgNode('path', {d, class: 'status-glyph'}));
    return svg;
}
function chip(status, word = STATUS[status].label) {
    const span = node('span', `status-chip status-${status}`);
    span.append(statusIcon(status), node('span', 'status-word', word));
    return span;
}
const ENDINGS = {exited: '', killed: 'child killed by the parent at the deadline', interrupted: 'parent interrupted', launch_failed: 'child could not be started'};
const VERBS = ['branch', 'commit', 'diff', 'log', 'merge', 'rebase', 'revert', 'reset', 'delete'];
const STATEMENT_ROWS = {CONNECT: 'Connect', READ: 'Read', INSERT: 'Insert', UPDATE: 'Update', DELETE_ROWS: 'Delete rows', DDL: 'DDL', API_RETRY_WAIT: 'Retry wait'};
const statusOf = (c, key) => c.attempts[key].outcome.status;
const completion = (c, key) => statusOf(c, key) === 'verified' && typeof c.attempts[key].outcome.completion_sec === 'number' ? c.attempts[key].outcome.completion_sec : null;
// One slot per system folder, machine, execution profile, and mode. The builder lists campaigns newest first, so the newest heads its slot.
function campaignSlots() {
    const slots = new Map();
    for (const c of MACRO.campaigns) {
        const key = `${c.folder}/${c.machine}/${c.execution_profile || 'local_single_host'}${c.native_only ? '/native-only' : ''}`;
        if (!slots.has(key)) slots.set(key, []);
        slots.get(key).push(c);
    }
    return slots;
}
const coverageText = c => `${Object.keys(SCENARIOS).filter(k => statusOf(c, k) === 'verified').length} of ${Object.keys(SCENARIOS).length} workflows completed`;
// Raw evidence of a failed attempt may be malformed, so every counter the page shows is checked here; anything else
// reads as not recorded.
const counter = v => Number.isSafeInteger(v) && v >= 0;
const workCounts = e => { const w = e && e.work; return w && ['expected', 'succeeded', 'failed', 'skipped'].every(k => counter(w[k])) && w.expected > 0 && w.succeeded + w.failed + w.skipped <= w.expected ? w : null; };
const SAMPLE = 400;  // validate_macro.scrubbed keeps at most this many check results; up to it the list must match the counts exactly
const checkResults = i => Array.isArray(i.results) && i.results.every(r => r && typeof r === 'object' && typeof r.name === 'string' && [true, false, null].includes(r.passed)) ? i.results : null;
function checkCounts(e) {
    const i = e && e.invariants, results = i && checkResults(i);
    if (!(results && typeof i.enabled === 'boolean' && ['checked', 'passed', 'failed', 'not_applicable'].every(k => counter(i[k])) && i.checked === i.passed + i.failed)) return null;
    const total = i.checked + i.not_applicable, tally = {passed: 0, failed: 0, not_applicable: 0};
    for (const r of results) tally[r.passed === true ? 'passed' : r.passed === false ? 'failed' : 'not_applicable'] += 1;
    const complete = total <= SAMPLE;
    return results.length === Math.min(total, SAMPLE) && Object.entries(tally).every(([k, n]) => complete ? n === i[k] : n <= i[k]) ? i : null;
}
const unsupportedOps = e => Array.isArray(e && e.unsupported_ops) ? uniq(e.unsupported_ops.filter(s => typeof s === 'string').map(s => s.split(':')[0]).filter(Boolean)) : [];
const timeOf = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? seconds(v) : 'Not recorded';
function workSummary(a) {
    const e = a.evidence, w = workCounts(e);
    if (!w) return {value: 'Not recorded', absent: true, sub: !e ? 'no evidence' : 'work counters malformed'};
    const unfinished = w.expected - w.succeeded - w.failed - w.skipped;
    const notes = [w.failed ? `${w.failed} failed` : '', w.skipped ? `${w.skipped} skipped` : '', unfinished ? `${unfinished} not finished` : ''].filter(Boolean);
    return {value: `${w.succeeded} / ${w.expected}`, sub: notes.length ? notes.join(' · ') : 'work units', parts: [['ok', w.succeeded, 'succeeded'], ['bad', w.failed, 'failed'], ['warn', w.skipped, 'skipped'], ['none', unfinished, 'not finished']], total: w.expected};
}
function checkSummary(a) {
    const e = a.evidence, i = checkCounts(e);
    if (!i) return {value: 'Not recorded', absent: true, sub: !e ? 'no evidence' : 'check counters malformed'};
    if (!i.enabled) return {value: 'Disabled', absent: true, sub: 'checks turned off'};
    const notes = [i.failed ? `${i.failed} failed` : '', i.not_applicable ? `${i.not_applicable} N/A` : ''].filter(Boolean);
    if (i.checked === 0) return {value: 'None applicable', absent: true, sub: notes.join(' · ')};
    return {value: `${i.passed} / ${i.checked}`, sub: notes.length ? notes.join(' · ') : 'checks passed', parts: [['ok', i.passed, 'passed'], ['bad', i.failed, 'failed'], ['none', i.not_applicable, 'not applicable']], total: i.checked + i.not_applicable};
}
// A proportion bar: one segment per part, widths by count, with the counts written beside it.
function stackBar(summary, what) {
    const box = node('div', 'stack');
    const line = node('p', 'stack-line'); line.append(node('strong', summary.absent ? 'cell-absent' : 'metric-value', summary.value), node('span', 'row-meta', summary.sub)); box.append(line);
    if (summary.parts) {
        const track = node('div', 'stack-bar'); track.setAttribute('role', 'img');
        track.setAttribute('aria-label', `${what}: ${summary.parts.filter(([, n]) => n).map(([, n, word]) => `${n} ${word}`).join(', ')} of ${summary.total}`);
        for (const [kind, n] of summary.parts) if (n) { const seg = node('span', `stack-seg seg-${kind}`); seg.style.flexGrow = String(n); track.append(seg); }
        box.append(track);
    }
    return box;
}
// Comparison groups: campaigns compare only within one group, which binds the workload, the benchmark revision, the
// runtime and the machine. Every column is ranked, marked and shaded against its own fastest time in the group.
function groupsOf(list) {
    const groups = new Map();
    for (const c of list) { if (!groups.has(c.group)) groups.set(c.group, []); groups.get(c.group).push(c); }
    return [...groups.entries()].map(([group, rows]) => ({group, rows})).sort((a, b) => b.rows.length - a.rows.length || a.group.localeCompare(b.group));
}
function groupStats(g, key) {
    const timed = g.rows.map(c => ({c, t: completion(c, key)})).filter(x => x.t !== null).sort((a, b) => a.t - b.t);
    return {timed, fastest: timed.length ? timed[0].t : null, rank: ranksOf(timed, 'c', 't')};
}
// A cell is its result and nothing else: a time, marked and shaded against the fastest in its column when the column
// compares at least two times, or the workflow's status.
function macroCell(tr, c, key, stats) {
    const status = statusOf(c, key), a = c.attempts[key], t = completion(c, key), td = tr.insertCell(), value = node('span', 'wf-value');
    td.className = `wf-cell status-${status}`;
    if (t !== null) {
        const comparison = compared(stats.timed.map(x => x.t));
        if (comparison && t === stats.fastest) value.append(bestMark('Fastest in this workflow'));
        value.append(node('span', 'metric-value', seconds(t)));
        if (comparison) shade(td, stats.fastest, t);
    } else value.append(statusIcon(status), node('span', 'wf-status', STATUS[status].label));
    td.append(value);
    const unsupported = unsupportedOps(a.evidence);
    if (unsupported.length) td.append(node('span', 'wf-note', `Unsupported: ${unsupported.join(', ')}`));
    if (a.outcome.reasons.length) td.title = a.outcome.reasons.join('; ');
    else if (status === 'timed_out') td.title = 'The deadline ended the attempt; its partial elapsed time is in the details';
}
// The workflow board, a data table: a row per campaign, a column per workflow, sorted by one workflow's completion time from
// its header, and a last column with how many workflows completed beside the row's Details button.
function macroTable(g, keys, empty = null) {
    const stats = Object.fromEntries(keys.map(k => [k, groupStats(g, k)])), sortable = g.rows.length > 0;
    const {table, head, body} = dataTable('board workflow-table', `Agent workflows: completion time per system and workflow${sortable ? `, ranked by ${workflow(state.rank).name}` : ''}`, [colHead('System', 'system-col')]);
    for (const key of keys) {
        const th = sortable ? sortHeader(workflow(key).name, key === state.rank, state.rankDesc, descending => { state.rank = key; state.rankDesc = descending; writeHash(boardHash()); }, `rank-${key}-${g.group}`) : colHead(workflow(key).name);
        th.classList.add('wf-col'); head.append(th);
    }
    head.append(colHead('Completed', 'done-col'));  // with the row's Details, the table's action column
    const ranked = stats[state.rank] || groupStats(g, state.rank);
    const order = sortedRows(ranked.timed.map(x => x.c), g.rows.filter(c => !ranked.rank.has(c)).sort((a, b) => a.system.localeCompare(b.system) || a.machine.localeCompare(b.machine)), state.rankDesc);
    for (const c of order) {
        const tr = body.insertRow();
        const th = systemHeader(ranked.rank.get(c), c, c.system, [`v${c.version}`, c.started.slice(0, 10), c.native_only ? 'Native-only' : null]); tr.append(th);
        if (!ranked.rank.has(c)) th.title = `Not ranked by ${workflow(state.rank).name}: ${STATUS[statusOf(c, state.rank)].label}`;
        for (const key of keys) macroCell(tr, c, key, stats[key]);
        const cell = tr.insertCell(); cell.className = 'done-cell';
        const verified = keys.filter(k => statusOf(c, k) === 'verified').length, strip = node('span', 'status-strip');
        strip.setAttribute('role', 'img'); strip.setAttribute('aria-label', `${verified} of ${keys.length} workflows completed`);
        for (const k of keys) { const seg = node('span', `strip-seg status-${statusOf(c, k)}`); seg.title = `${workflow(k).name}: ${STATUS[statusOf(c, k)].label}`; strip.append(seg); }
        const summary = node('span', 'done-summary'), line = node('span', 'done-line'); summary.append(node('span', 'done-count', `${verified} / ${keys.length}`), strip);
        const detailId = slug('details', c.folder, c.machine,
            ...(c.execution_profile && c.execution_profile !== 'local_single_host' ? [c.execution_profile] : []),
            ...(c.native_only ? ['native-only'] : []));
        line.append(summary, detailsButton(c.system, opener => showCampaign(c, opener), detailId)); cell.append(line);
    }
    if (!g.rows.length && empty) { const tr = body.insertRow(); tr.className = 'empty-row'; const td = tr.insertCell(); td.setAttribute('colspan', String(keys.length + 2)); td.append(empty); }
    return scrolling(table, 'Scroll sideways to see every workflow.');
}
const MACRO_CHIPS = {
    macroHosted: ['macro-chip-hosted', 'macro-value-hosted', 'macro-selectors-hosted', k => HOSTING[k] || k],
    macroSystem: ['macro-chip-system', 'macro-value-system', 'macro-selectors-system', k => k],
    macroMachine: ['macro-chip-machine', 'macro-value-machine', 'macro-selectors-machine', k => k],
    macroTag: ['macro-chip-tag', 'macro-value-tag', 'macro-selectors-tag', k => k],
};
const MACRO_FILTERS = {
    hosting: c => state.macroHosted[c.hosted || 'no'] !== false,
    system: c => state.macroSystem[c.system] !== false,
    machine: c => state.macroMachine[c.machine] !== false,
    tags: c => Object.values(state.macroTag).every(Boolean) || (c.tags || []).some(t => state.macroTag[t]),
};
function resetMacroFilters() {
    for (const key in MACRO_CHIPS) for (const value in state[key]) state[key][value] = true;
    writeHash(boardHash()); render();
}
function renderMacroChips() {
    for (const [key, [chipId, valId, selId, name]] of Object.entries(MACRO_CHIPS)) {
        const map = state[key], values = Object.keys(map), on = values.filter(v => map[v]), all = on.length === values.length;
        el(chipId).hidden = key !== 'macroHosted' && values.length < 2;
        el(chipId).classList.toggle('chip-active', !all);
        el(valId).textContent = all ? 'All' : on.length === 1 ? name(on[0]) : on.length ? `${on.length} of ${values.length}` : 'None';
        el(selId).replaceChildren(filterOption(`${key}-all`, 'All', all, () => { for (const v of values) map[v] = !all; }),
            ...values.map((v, i) => filterOption(`${key}-${i}`, name(v), map[v], () => { map[v] = !map[v]; })));
    }
}
function wireMacro() {
    el('macro-reset-filters').addEventListener('click', resetMacroFilters);
    const chips = Object.values(MACRO_CHIPS).map(([chipId]) => el(chipId));
    for (const chip of chips) chip.addEventListener('toggle', () => { if (chip.open) for (const other of chips) if (other !== chip) other.open = false; });
    document.addEventListener('click', event => { for (const chip of chips) if (chip.open && !chip.contains(event.target)) chip.open = false; });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') for (const chip of chips) if (chip.open) { chip.open = false; chip.querySelector('summary').focus(); } });
}
function machineParts(c) {
    const env = c.environment || {};
    return [typeof env.cpus === 'number' ? plural(env.cpus, 'CPU') : null, gib(env.memory_bytes, 0), c.platform, c.machine];
}
function macroGroup(g, keys) {
    const box = node('section', 'board-group'), heading = node('div', 'group-heading');
    const title = g.nativeOnly ? 'Agent workflows (Native-only)' : g.profileLabel ? `Agent workflows (${g.profileLabel})` : 'Agent workflows';
    heading.append(node('h2', '', title),
        node('span', 'group-count', `${plural(g.rows.length, 'system')} · ranked by ${workflow(state.rank).name} · lower is better`));
    if (keys.some(k => groupStats(g, k).timed.length >= 2)) heading.append(heatKey('Shading: darker mint is closer to the fastest completion time in its column'));
    box.append(heading, macroTable(g, keys));
    return box;
}
// Where a campaign came from: sources, runtime and machine.
function provenance(c) {
    const env = c.environment || {};
    return [['Run ID', c.campaign_id], ['Comparison group', c.group], ['Git commit', c.commit], ['Source SHA-256', c.source_sha256],
        ['Dependency lock SHA-256', c.lock_sha256], ['Python', c.python], ['Platform', c.platform], ['CPU', env.cpu || 'unknown'],
        ['CPU count', env.cpus ?? 'unknown'], ['Memory', gib(env.memory_bytes, 1) || 'unknown'], ['Started', c.started], ['Finished', c.finished],
        ...(c.native_only ? [['Mode', 'Native-only']] : []), ...(c.execution_profile ? [['Execution profile', c.execution_profile]] : [])];
}
function verbsScalingSection(records) {
    const ordered = [...records].sort((a, b) => a.system.localeCompare(b.system) || (a.scale - b.scale));
    const pointIds = uniq(ordered.flatMap(r => (r.points || []).map(p => p.id).filter(id => id && id !== 'control')));
    const {table, body} = dataTable('heatmap', 'Verb scaling: median p50 latency per grid point', ['Grid point', ...ordered.map(r => {
        const label = `${r.system}${r.native_only ? ' (Native-only)' : ''} · W=${r.scale}`;
        const th = colHead(label); th.title = `${r.machine} · ${r.date}`;
        return th;
    })]);
    for (const pid of pointIds) {
        const tr = body.insertRow(); tr.append(rowHead(pid, 'row-label'));
        for (const r of ordered) {
            const pt = (r.points || []).find(p => p.id === pid), td = tr.insertCell(), p50 = pt?.latency?.p50;
            td.textContent = pt?.status === 'ok' && typeof p50 === 'number' ? `${(p50 * 1000).toFixed(1)} ms` : pt?.status === 'unsupported' ? 'Unsupported' : '—';
        }
    }
    return breakdownSection(null, 'Verb scaling', 'Median p50 latency across scaling axes per system and scale.', table, []);
}
let macroGroupsShown = [];
function renderMacro() {
    renderMacroChips();
    const keys = Object.keys(SCENARIOS);
    if (!keys.includes(state.rank)) state.rank = keys[0];
    const slots = campaignSlots(), campaigns = [...slots.values()].flat();
    state.campaign = new Set([...state.campaign].filter(id => campaigns.some(c => c.campaign_id === id)));  // an id from another build never lingers in the URL
    const chosen = [...slots.values()].map(list => list.find(c => state.campaign.has(c.campaign_id)) || list[0]);
    const shown = chosen.filter(c => Object.values(MACRO_FILTERS).every(f => f(c)));
    const filtered = Object.keys(MACRO_CHIPS).some(k => Object.values(state[k]).some(v => !v));
    el('macro-reset-filters').hidden = !filtered;
    el('macro-shown').textContent = `${plural(shown.length, 'system')}${filtered ? ' · filtered' : ''}`;
    const latest = chosen.map(c => c.started.slice(0, 10)).sort().at(-1);
    el('macro-updated').textContent = latest ? `Last run ${latest}` : 'No published runs yet';
    const hasExplicitProfile = shown.some(c => Boolean(c.execution_profile));
    const cohortOf = c => `${c.native_only ? '1:native' : '0:std'}:${c.execution_profile || 'local_single_host'}:${hasExplicitProfile ? c.group : ''}`;
    const cohorts = uniq(shown.map(cohortOf));
    const stdCohorts = cohorts.filter(k => k.startsWith('0:std:'));
    macroGroupsShown = cohorts.map(ck => {
        const rows = shown.filter(c => cohortOf(c) === ck), first = rows[0];
        const prof = first.execution_profile || 'local_single_host';
        return {
            group: `${first.group}${first.native_only ? '-native-only' : ''}${prof !== 'local_single_host' ? `-${prof}` : ''}`,
            rows,
            nativeOnly: Boolean(first.native_only),
            profileLabel: !first.native_only && stdCohorts.length > 1 ? prof : '',
        };
    });
    // Without any campaign the table shell stays, with one honest row: no fabricated result.
    el('macro-groups').replaceChildren(...macroGroupsShown.length ? macroGroupsShown.map(g => macroGroup(g, keys))
        : [macroTable({group: '', rows: []}, keys, node('span', 'empty-cell', chosen.length ? 'No matches. Adjust or reset the filters.' : 'No published results yet. Results appear here once a validated run is published.'))]);
    el('macro-breakdowns').replaceChildren(...macroGroupsShown.map(g => macroBreakdown(g, Boolean(g.nativeOnly || g.profileLabel || macroGroupsShown.length > 1))), ...(MACRO.verbs.length ? [verbsScalingSection(MACRO.verbs)] : []));
    const allExcluded = [...MACRO.excluded.map(x => ({label: `${x.system} / ${x.source.split('/').at(-1)}`, href: PHASE === 'prod' ? null : x.source, reason: x.reason})),
        ...MACRO.verbsExcluded.map(x => ({label: `verbs / ${x.source}`, href: PHASE === 'prod' ? null : x.source, reason: x.reason}))];
    el('macro-excluded').replaceChildren(...allExcluded.length ? [excludedPanel('macro-excluded-panel', 'Runs not on the board', allExcluded)] : []);  // production never links a file it does not serve
    renderExplainer(keys);
}
// A row's details, all in one place: its rank, each workflow against the fastest with that workflow's evidence behind
// its own disclosure, the other runs of this system on this machine, then provenance.
function showCampaign(c, opener) {
    const g = macroGroupsShown.find(x => x.rows.includes(c)) || {group: c.group, rows: [c]};
    const keys = Object.keys(SCENARIOS), rankStats = groupStats(g, state.rank);
    const body = openDetails(c.system, `v${c.version} · ${c.machine} · ${c.started.slice(0, 10)}`, c.source, opener);
    const r = rankStats.rank.get(c), t = completion(c, state.rank), reason = c.attempts[state.rank].outcome.reasons[0], alone = rankStats.timed.length === 1;
    body.append(node('p', r ? 'verdict' : 'verdict verdict-unranked', r ? `${ordinal(r)} of ${rankStats.timed.length} by ${workflow(state.rank).name}: ${seconds(t)}${alone ? ', the only completed time in this comparison' : t === rankStats.fastest ? ', the fastest' : rankStats.fastest > 0 ? `, ×${(t / rankStats.fastest).toFixed(2)} the fastest` : ''}.`
        : `Not ranked by ${workflow(state.rank).name}: ${STATUS[statusOf(c, state.rank)].label}${reason ? `, ${reason}` : ''}.`));
    detailsSection(body, 'Workflows', `${coverageText(c)}. Bars share one scale per workflow and the tick marks the fastest time in this comparison. Open a workflow for its evidence.`);
    const list = node('div', 'why-list');
    for (const key of keys) list.append(workflowDetails(c, key, groupStats(g, key)));
    body.append(list);
    const others = [...campaignSlots().values()].find(slot => slot.includes(c)) || [];
    if (others.length > 1) {
        detailsSection(body, 'Other runs on this machine', 'Choose which run the leaderboard shows for this system.');
        const choices = node('div', 'campaign-choices');
        for (const other of others) {
            const button = node('button', 'selector' + (other === c ? ' selector-active' : ''), `${other.started.slice(0, 16).replace('T', ' ')} · ${coverageText(other)}`);
            button.type = 'button'; button.id = `campaign-${other.campaign_id}`; button.setAttribute('aria-pressed', String(other === c));
            button.addEventListener('click', () => {
                for (const x of others) state.campaign.delete(x.campaign_id);
                if (other !== others[0]) state.campaign.add(other.campaign_id);
                writeHash(boardHash()); render(); showCampaign(other, opener);
            });
            choices.append(button);
        }
        body.append(choices);
    }
    body.append(disclosure('Provenance', metaGrid(provenance(c))));
    if (c.tags && c.tags.length) body.append(tagList(c.tags));
    showDetails();
}
// One workflow in the details: its time and rank against the fastest, or its status and first reason; open for the evidence.
function workflowDetails(c, key, stats) {
    const status = statusOf(c, key), time = completion(c, key);
    const item = node('details', 'why-item'), summary = node('summary', 'why-row');
    const name = node('span', 'why-name'); name.append(node('strong', '', workflow(key).name), node('span', 'row-meta', SCENARIOS[key]));
    const value = node('span', 'why-value');
    if (time !== null) {
        const slowest = Math.max(...stats.timed.map(x => x.t)), comparison = stats.timed.length >= 2;
        value.append(node('span', 'metric-value', seconds(time)), node('span', 'row-meta', comparison ? `${ordinal(stats.rank.get(c))} of ${stats.timed.length}` : 'the only completed time'));
        if (comparison && slowest) name.append(timeBar(time, slowest, stats.fastest));
    } else value.append(chip(status), node('span', 'row-meta', c.attempts[key].outcome.reasons[0] || ''));
    summary.append(name, value, icon(ICON.chevron, 'icon why-chevron'));
    item.append(summary, attemptBody(c.attempts[key]));
    return item;
}
// One attempt's evidence: outcome, work and checks as proportions, then the checks, the operation tables and the attempt
// record behind disclosures. Evidence is shown as recorded, so every list is checked before it is walked and a malformed one is named.
function attemptBody(a) {
    const e = a.evidence || {}, child = a.child || {}, box = node('div', 'why-body'), outcome = node('dl', 'outcome');
    const fact = (term, value, note) => { const pair = node('div'), dd = node('dd'); dd.append(typeof value === 'string' ? node('span', 'outcome-value', value) : value, node('span', 'row-meta', note)); pair.append(node('dt', '', term), dd); outcome.append(pair); };
    fact('Status', chip(a.outcome.status), a.outcome.status === 'verified' ? 'All required work verified' : '');  // a failure's first reason already heads this workflow's row
    fact('Completion time', a.outcome.status === 'verified' ? timeOf(a.outcome.completion_sec) : '—', a.outcome.status === 'verified' ? 'Workflow running time including the stop of background traffic; schema and seed setup and database cleanup excluded' : typeof a.outcome.elapsed_sec !== 'number' ? 'No elapsed time recorded' : `Partial elapsed ${seconds(a.outcome.elapsed_sec)}, not a completion time`);
    fact('Setup time', timeOf(a.outcome.setup_sec), 'Schema and seed data, before the clock starts');
    box.append(outcome);
    if (a.outcome.reasons.length > 1) box.append(tagList(a.outcome.reasons.slice(1), 'attempt-reasons'));  // the others, each named once
    const work = e.work && typeof e.work === 'object' ? e.work : {}, errors = Array.isArray(e.worker_errors) ? e.worker_errors : null, verbs = e.failed_verbs && typeof e.failed_verbs === 'object' ? e.failed_verbs : {};
    const tallyOf = v => v && typeof v === 'object' ? Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(', ') || 'none' : 'malformed, not a tally';
    const counts = node('div', 'stacks');
    const workBox = node('div'); workBox.append(node('h4', '', 'Work units'), stackBar(workSummary(a), 'Work units'));
    const checkBox = node('div'); checkBox.append(node('h4', '', 'Correctness checks'), stackBar(checkSummary(a), 'Correctness checks'));
    counts.append(workBox, checkBox); box.append(counts);
    if (e.work && !workCounts(e)) box.append(node('p', 'notice', 'Work counters are malformed or disagree with each other; shown as recorded.'));
    box.append(metaGrid([...workCounts(e) ? [] : [['Expected units', work.expected], ['Succeeded', work.succeeded], ['Failed', work.failed], ['Skipped', work.skipped]],
        ['Worker errors', errors ? errors.length : 'malformed, not a list'], ['Unexplained failed operations', tallyOf(verbs.unexpected)], ['Expected or recovered failures', tallyOf(verbs.expected)],
        ['Child status', e.status], ['Elapsed', typeof a.outcome.elapsed_sec === 'number' ? seconds(a.outcome.elapsed_sec) : null]]));
    for (const w of errors || []) box.append(node('p', 'notice', w && typeof w === 'object' ? `Worker ${w.worker}, item ${w.item}: ${w.error}` : `Worker error entry is not a record: ${JSON.stringify(w)}`));
    const inv = e.invariants && typeof e.invariants === 'object' ? e.invariants : null;
    if (!inv) box.append(node('p', 'fine', e.invariants === undefined || e.invariants === null ? 'No correctness-check evidence.' : 'Correctness-check evidence is not a record; shown in the raw summary only.'));
    else {
        const results = checkResults(inv), checkParts = [];
        if (!checkCounts(e)) box.append(node('p', 'notice', results ? 'Check counts are malformed or disagree with the listed results; shown as recorded.' : 'Check results are not a list of records; the counts are shown as recorded.'));
        checkParts.push(metaGrid([['Enabled', inv.enabled === true ? 'yes' : inv.enabled === false ? 'no' : JSON.stringify(inv.enabled)], ['Passed', inv.passed], ['Failed', inv.failed], ['Not applicable', inv.not_applicable]]));
        if (results && results.length) {
            const {table: checks, body: tb} = dataTable('detail-table check-table', 'Correctness checks', ['Check', 'Result', 'Detail']);
            for (const r of results) {
                const tr = tb.insertRow(); tr.append(rowHead(r.name));
                tr.insertCell().append(r.passed === true ? chip('verified', 'passed') : r.passed === false ? chip('failed', 'failed') : chip('not_applicable', 'not applicable'));
                tr.insertCell().textContent = typeof r.detail === 'string' ? r.detail : '';
            }
            checkParts.push(scrolling(checks));
        }
        box.append(disclosure(`Every check (${results ? results.length : 'unlisted'})`, ...checkParts));
    }
    const record = v => v && typeof v === 'object' ? v : {};
    const opCounts = record(e.op_status_counts), impl = record(record(e.implementation).verbs), caps = record(e.capabilities), notes = record(record(e.implementation).notes);
    const {table: ops, body: ob} = dataTable('detail-table ops-table', 'Branch verbs: declared implementation and observed outcomes', ['Verb', 'Declared', 'Observed', 'OK time']);
    const observed = n => !n ? 'not exercised' : [n.ok ? `${n.ok} ok` : '', n.failed ? `${n.failed} failed` : '', n.unsupported ? `${n.unsupported} unsupported` : ''].filter(Boolean).join(' · ') || 'not exercised';
    for (const verb of VERBS) {
        const n = record(opCounts[verb.toUpperCase()]), tr = ob.insertRow(); tr.append(rowHead(verb));
        const declared = impl[verb] || (verb in caps ? (caps[verb] ? 'supported' : 'unsupported') : 'not declared');
        tr.insertCell().textContent = notes[verb] ? `${declared} · ${notes[verb]}` : declared;
        tr.insertCell().textContent = observed(n); tr.insertCell().textContent = n.ok ? timeOf(n.ok_seconds) : '—';
    }
    const exec = record(opCounts.EXEC), opParts = [scrolling(ops), node('p', 'fine', `Agent steps (EXEC): ${observed(exec)}${exec.ok ? `, ${timeOf(exec.ok_seconds)} of OK time` : ''}. The rows below are the SQL and connect breakdown inside those steps and are not added again.`)];
    const {table: sql, body: sb} = dataTable('detail-table ops-table', 'SQL and connect breakdown inside EXEC', ['Inside EXEC', 'Observed', 'OK time']);
    for (const [op, text] of Object.entries(STATEMENT_ROWS)) { const n = opCounts[op]; if (!n) continue; const tr = sb.insertRow(); tr.append(rowHead(text)); tr.insertCell().textContent = observed(record(n)); tr.insertCell().textContent = record(n).ok ? timeOf(record(n).ok_seconds) : '—'; }
    if (sb.children.length) opParts.push(scrolling(sql));
    if (Array.isArray(e.unsupported_ops) ? e.unsupported_ops.length : e.unsupported_ops != null) opParts.push(node('p', 'notice', `Unsupported: ${Array.isArray(e.unsupported_ops) ? e.unsupported_ops.map(String).join(', ') : `${JSON.stringify(e.unsupported_ops)} (not a list; kept as recorded)`}`));
    if (e.worker_time && typeof e.worker_time === 'object') opParts.push(metaGrid([['Branch verb time', timeOf(e.worker_time.branch_sec)], ['Data step time', timeOf(e.worker_time.data_sec)], ['Total worker time', timeOf(e.worker_time.total_sec)], ['Background spine time', typeof e.worker_time.spine_sec === 'number' ? seconds(e.worker_time.spine_sec) : null]]));
    if (e.storage && typeof e.storage === 'object') { const sp = e.storage.points && typeof e.storage.points === 'object' ? e.storage.points : e.storage, gc = e.storage.gc && typeof e.storage.gc === 'object' ? e.storage.gc.status : null; opParts.push(metaGrid([['Storage scope', typeof e.storage.scope === 'string' ? e.storage.scope : null], ['Storage after setup', gib(sp.after_setup, 2)], ['Storage after workflow', gib(sp.after_workflow, 2)], ['Storage after GC', gib(sp.after_gc, 2)], ['GC status', typeof gc === 'string' ? gc : null], ['Storage before cleanup', gib(sp.before_cleanup, 2)], ['Storage after cleanup', gib(sp.after_cleanup, 2)], ['Retained delta', typeof e.storage_delta_bytes === 'number' ? gib(e.storage_delta_bytes, 2) : null]])); }
    if (Array.isArray(e.stalls) && e.stalls.length) for (const s of e.stalls) if (s && typeof s === 'object') { const dur = s.elapsed_sec ?? s.duration_sec; opParts.push(node('p', 'notice', `Stall: ${s.op || 'op'} took ${typeof dur === 'number' ? seconds(dur) : dur}${typeof e.op_stall_sec === 'number' ? ` (threshold ${seconds(e.op_stall_sec)})` : ''}`)); }
    if (Array.isArray(e.backend_observations) && e.backend_observations.length) for (const obs of e.backend_observations) if (typeof obs === 'string') opParts.push(node('p', 'fine', `Observation: ${obs}`));
    if (e.backend_observations && typeof e.backend_observations === 'object' && !Array.isArray(e.backend_observations)) for (const [k, v] of Object.entries(e.backend_observations)) opParts.push(node('p', 'fine', `Observation (${k}): ${v}`));
    if (e.metrics && typeof e.metrics === 'object') opParts.push(metaGrid(Object.entries(e.metrics).filter(([, v]) => typeof v !== 'object').map(([k, v]) => [k, typeof v === 'number' ? Number(v.toFixed(4)) : v])));
    const unsupported = unsupportedOps(e);
    box.append(node('p', 'fine', unsupported.length ? `Unsupported in this attempt: ${unsupported.join(', ')}.` : `${VERBS.filter(v => record(opCounts[v.toUpperCase()]).ok).length} of ${VERBS.length} branch verbs exercised successfully.`), disclosure('Verbs, SQL breakdown and metrics', ...opParts));
    const raw = node('a', 'text-link', 'Raw attempt JSON '); raw.href = a.source; raw.target = '_blank'; raw.rel = 'noopener';
    const arrow = node('span', '', '↗'); arrow.setAttribute('aria-hidden', 'true'); raw.append(arrow, node('span', 'sr-only', ' (opens in a new tab)'));
    const tail = node('pre', 'log-tail', child.log_tail); tail.tabIndex = 0; tail.setAttribute('aria-label', 'Last lines of the child log');  // it scrolls, so a keyboard must reach it
    const log = child.log_tail ? [node('p', 'fine', 'Last lines of the child log'), tail] : [];
    box.append(disclosure('Attempt record', metaGrid([['Attempt', a.attempt_id], ['Date', a.date], ['Workload identity SHA-256', a.config_sha256], ['Base config SHA-256', a.base_config_sha256],
        ['Child ended', a.child ? `${child.ended}${child.exit_code === null ? '' : ` (exit code ${child.exit_code})`}` : 'no record'], ['Runtime cap', a.child ? `${child.deadline_sec}s${ENDINGS[child.ended] ? ` · ${ENDINGS[child.ended]}` : ''}` : null],
        ['Launch error', child.error], ['Seed', e.seed]]), ...log, raw));
    return box;
}
function renderStatusRules() {
    for (const [status, rule] of Object.entries(STATUS_RULES)) { const pair = node('div'), term = node('dt'); term.append(chip(status)); pair.append(term, node('dd', '', rule)); el('status-rules').append(pair); }
}

// ---------------------------------------------------------------- Evidence across systems, below the card
// An attempt's per-operation counts, or null when it recorded none or any entry is malformed: such an attempt counts nowhere.
function opCounts(a) {
    const counts = a.evidence && a.evidence.op_status_counts;
    const valid = n => n && ['ok', 'failed', 'unsupported'].every(k => counter(n[k])) && typeof n.ok_seconds === 'number' && Number.isFinite(n.ok_seconds) && n.ok_seconds >= 0;
    return counts && typeof counts === 'object' && Object.values(counts).every(valid) ? counts : null;
}
// Branch operations: each system's mean time per successful call over every workflow it ran, so an operation that failed
// and ended a workflow early still shows, with its failures under its time. A row is an operation some system here called.
function verbTable(campaigns) {
    const totals = campaigns.map(c => Object.fromEntries(VERBS.map(verb => [verb, Object.values(c.attempts).map(opCounts).filter(Boolean).map(counts => counts[verb.toUpperCase()]).filter(Boolean)
        .reduce((t, n) => ({ok: t.ok + n.ok, failed: t.failed + n.failed, unsupported: t.unsupported + n.unsupported, seconds: t.seconds + n.ok_seconds}), {ok: 0, failed: 0, unsupported: 0, seconds: 0})])));
    const {table, body} = dataTable('heatmap', 'Branch operations: mean time per successful call, per system', ['Operation', ...campaigns.map(c => {
        const identity = node('span', 'system-identity'); identity.append(systemIcon(c), node('span', 'system-name', c.system));
        const th = colHead(identity); th.title = `${c.machine} · ${c.started.slice(0, 10)}`;
        return th;
    })]);
    for (const verb of VERBS) {
        const cells = totals.map(t => t[verb]), means = cells.map(n => n.ok ? n.seconds / n.ok : null), comparison = compared(means), fastest = Math.min(...means.filter(m => m !== null));
        if (!cells.some(n => n.ok + n.failed + n.unsupported)) continue;
        const tr = body.insertRow(); tr.append(rowHead(verb[0].toUpperCase() + verb.slice(1), 'row-label'));
        cells.forEach((n, i) => {
            const td = tr.insertCell(), mean = means[i], calls = n.ok + n.failed + n.unsupported, best = comparison && mean === fastest;
            td.className = mean !== null ? best ? 'cell-best' : '' : calls ? 'cell-missing' : 'cell-absent';
            td.title = calls ? `${fmt(n.ok)} of ${plural(calls, 'call')} succeeded${n.ok ? `, ${seconds(n.seconds)} in all` : ''}` : 'Not called in any workflow';
            if (mean !== null && comparison) shade(td, fastest, mean);
            if (best) td.append(bestMark('Fastest in this row'));
            td.append(node('span', 'metric-value', mean !== null ? `${(mean * 1000).toFixed(1)} ms` : !calls ? 'Not called' : n.failed ? 'Failed' : 'Unsupported'));
            const problems = [n.failed ? `${fmt(n.failed)} failed` : '', n.unsupported ? `${fmt(n.unsupported)} unsupported` : ''].filter(Boolean);
            if (problems.length) td.append(node('span', 'wf-note', `${problems.join(', ')} of ${plural(calls, 'call')}`));
        });
    }
    if (!body.children.length) { const tr = body.insertRow(); tr.className = 'empty-row'; const td = tr.insertCell(); td.setAttribute('colspan', String(campaigns.length + 1)); td.append(node('span', 'empty-cell', 'No branch operations recorded.')); }
    return table;
}
// Time in branch operations: for each completed workflow, the share of its operation time spent in the nine branch
// operations rather than in agent steps. Operation time adds up concurrent workers' calls, so only the share compares.
function shareChart(campaigns) {
    const card = node('figure', 'panel dotplot-panel'), keys = Object.keys(SCENARIOS);
    card.append(node('h3', '', 'Time in branch operations'), node('p', 'panel-note', 'Share of each completed workflow\'s operation time spent in branch operations; the rest is agent steps.'));
    const rows = keys.map(key => ({label: workflow(key).name, dots: campaigns.flatMap(c => {
        const counts = statusOf(c, key) === 'verified' && opCounts(c.attempts[key]);
        if (!counts) return [];
        const branch = VERBS.reduce((sum, verb) => sum + (counts[verb.toUpperCase()]?.ok_seconds ?? 0), 0), total = branch + (counts.EXEC?.ok_seconds ?? 0);
        return total ? [{e: c, value: branch / total, title: `${c.system} · ${workflow(key).name}: ${Math.round(branch / total * 100)}% in branch operations, ${seconds(branch)} of ${seconds(total)}`}] : [];
    })}));
    const shares = rows.flatMap(r => r.dots.map(d => d.value)), percent = v => `${Math.round(v * 100)}%`;
    if (!shares.length) { card.append(node('p', 'chart-empty', 'No completed workflow recorded its operation time.')); return card; }
    const shown = campaigns.filter(c => rows.some(r => r.dots.some(d => d.e === c))), gaps = rows.some(r => r.dots.length < campaigns.length);
    card.append(dotPlot(`Share of operation time in branch operations across ${plural(keys.length, 'workflow')}, from ${percent(Math.min(...shares))} to ${percent(Math.max(...shares))}.`, rows, campaigns,
        [0, 0.25, 0.5, 0.75, 1].map(v => ({value: v, text: percent(v)})), v => `${(v * 100).toFixed(2)}%`),
        chartLegend(shown), node('p', 'panel-note', `Operation time adds up every worker's calls, so it can exceed the completion time.${gaps ? ' A workflow that did not complete has no dot.' : ''}`));
    return card;
}
// The evidence behind a comparison group, below the card: its branch operations beside where its operation time goes,
// systems in name order.
function macroBreakdown(g, named) {
    const c = g.rows[0], campaigns = [...g.rows].sort((a, b) => a.system.localeCompare(b.system));
    return breakdownSection(named ? [g.nativeOnly ? 'Native-only mode' : g.profileLabel || null, (c.environment || {}).cpu, c.machine, `revision ${c.commit.slice(0, 7)}`].filter(Boolean).join(' · ') : null, 'Branch operations',
        'Mean time per successful call over every workflow each system ran, shaded within each row.', verbTable(campaigns), [shareChart(campaigns)]);
}

// ---------------------------------------------------------------- Workflow definitions
const DIAGRAM_HEIGHTS = {rl_env: 360, context_mgmt: 400, multi_agent: 352, dev_agent: 352, ops_agent: 376, data_agent: 400};  // CSS px of each 2x PNG
function diagramImage(key, spec) {
    const img = node('img', 'workflow-diagram');
    img.setAttribute('src', `workflow-diagrams/${key}.png`); img.setAttribute('alt', `${spec.name}, branch history: ${spec.alt}`);
    img.setAttribute('width', 900); img.setAttribute('height', DIAGRAM_HEIGHTS[key]); img.setAttribute('decoding', 'async');
    return img;
}
const SCENES = {
    rl_env: {lanes: [['main', 'main', 'database', 'main'], ['task', 'task', 'temporary', 'a'], ['ra', 'repair A', 'temporary', 'b'], ['rb', 'repair B', 'temporary', 'c']], items: [
        L('main', .04, .10, 0, .15), ...run('main', [[.10, .15], [.22, .45]]), T('main', .13, .5, 'seeded state', false), R('main', 'main', [[.45, .22]]),
        C('main', .22, 'task', .34, .7, 1.1), D('task', .34, 1.1), T('task', .34, 1.15, 'inject fault, commit', true), R('task', 'task', [[1.1, .34]], 3.9, true),
        C('task', .34, 'ra', .46, 1.3, 1.7), ...run('ra', [[.46, 1.7], [.57, 2.0], [.68, 2.3]]), R('repair A', 'ra', [[1.7, .46], [2.0, .57], [2.3, .68]], 4.0, true), T('ra', .57, 2.1, 'repair checkpoints', true),
        C('task', .34, 'rb', .46, 2.35, 2.65), ...run('rb', [[.46, 2.65], [.57, 2.9]]), R('repair B', 'rb', [[2.65, .46], [2.9, .57]], 4.0, true),
        T('rb', .96, 3.1, 'compare each repair tip with the task commit', true, 'right'), T('main', .96, 3.9, 'temporary refs released; main unchanged', true, 'right')]},
    context_mgmt: {lanes: [['ra', 'retained', 'candidate A', 'a'], ['main', 'main', 'database', 'main'], ['rb', 'selected', 'candidate B', 'b']], items: [
        L('main', .04, .10, 0, .15), ...run('main', [[.10, .15], [.18, .3], [.28, .5]]),
        R('main', 'main', [[.5, .28], [1.6, .28], [2.0, .42, 'rb'], [2.5, .52, 'rb'], [2.8, .62, 'rb'], [3.1, .72, 'rb'], [3.3, .72, 'rb'], [3.8, .66, 'ra']]),
        C('main', .28, 'ra', .42, .7, 1.1), D('ra', .42, 1.1), T('ra', .42, 1.15, 'compaction A', true), R('candidate A', 'ra', [[1.1, .42]], 4.1, true),
        C('main', .28, 'rb', .42, 1.1, 1.5), D('rb', .42, 1.5), R('candidate B', 'rb', [[1.5, .42]], 4.1, true),
        ...run('rb', [[.42, 2.0, false], [.52, 2.5], [.62, 2.8], [.72, 3.1]], 3.7), T('rb', .50, 3.15, 'later work, then a revert commit', false, 'left'),
        ...run('ra', [[.42, 3.3, false], [.54, 3.55], [.66, 3.8]]), T('ra', .50, 3.6, 'later work replayed after A', false, 'left'),
        T('main', .96, 4.0, 'candidate refs released after their retention window', false, 'right')]},
    multi_agent: {lanes: [['a', 'agent A', 'temporary', 'a'], ['main', 'main', 'database', 'main'], ['b', 'agent B', 'temporary', 'b']], items: [
        L('main', .04, .12, 0, .15), ...run('main', [[.12, .15], [.28, .4]]), T('main', .26, .45, 'shared checkpoint', false, 'right'),
        R('main', 'main', [[.4, .28], [1.8, .28], [2.2, .62], [2.9, .62], [3.3, .82]]),
        C('main', .28, 'a', .42, .6, 1.0), ...run('a', [[.42, 1.0], [.52, 1.3]]), R('agent A', 'a', [[1.0, .42], [1.3, .52]], 3.9), T('a', .47, 1.35, 'revise and commit', false),
        C('main', .28, 'b', .42, .6, 1.0), ...run('b', [[.42, 1.0], [.50, 1.3]]), R('agent B', 'b', [[1.0, .42], [1.3, .50]], 3.9, true), T('b', .46, 1.35, 'revise and commit', true),
        C('a', .52, 'main', .62, 1.8, 2.2), ...run('main', [[.28, .4, false], [.62, 2.2]]), T('main', .62, 2.25, 'integrate A, then B', false),
        C('b', .50, 'main', .82, 2.9, 3.3), ...run('main', [[.62, 2.2, false], [.82, 3.3]]),
        T('b', .96, 3.6, 'serial integration under a fixed policy', false, 'right')]},
    dev_agent: {lanes: [['f', 'feature', 'before rebase', 'a'], ['f2', 'feature', 'rebased', 'a'], ['prod', 'production', 'database', 'main']], items: [
        L('prod', .04, .12, 0, .15), ...run('prod', [[.12, .15], [.25, .4]]), T('prod', .25, .45, 'base', false),
        R('production', 'prod', [[.4, .25], [1.1, .25], [1.4, .40], [1.7, .50], [2.0, .62], [3.0, .62], [3.3, .76]]),
        C('prod', .25, 'f', .36, .5, .9, 2.9), ...run('f', [[.36, .9], [.46, 1.2], [.56, 1.5]], 2.9), T('f', .46, 1.3, 'schema and backfill', true),
        R('feature', 'f', [[.9, .36], [1.2, .46], [1.5, .56], [2.6, .56], [3.3, .88, 'f2'], [3.7, .95, 'f2']], 4.1),
        ...run('prod', [[.25, .4, false], [.40, 1.4], [.50, 1.7], [.62, 2.0]]), T('prod', .40, 1.45, 'migration', false), T('prod', .62, 2.05, 'newer base', false),
        C('prod', .62, 'f2', .70, 2.4, 2.8), ...run('f2', [[.70, 2.8], [.79, 3.05], [.88, 3.3], [.95, 3.7]]), T('f2', .79, 3.1, 'replayed after the newer base', false),
        ...run('prod', [[.62, 2.0, false], [.76, 3.3]]),
        T('f', .96, 4.0, 'feature ref released; never merged into production', false, 'right')]},
    ops_agent: {lanes: [['prod', 'production', 'database', 'main'], ['ia', 'investigation A', 'temporary', 'a'], ['ib', 'investigation B', 'temporary', 'b']], items: [
        L('prod', .04, .12, 0, .2), ...run('prod', [[.12, .2], [.22, .45], [.32, .7]]), T('prod', .22, .5, 'checkpoints under load', false),
        ...run('prod', [[.32, .7, false], [.44, .95], [.56, 1.2], [.70, 1.45]], 3.0), T('prod', .63, 1.5, 'bad deployment, corrupted head', false),
        R('production', 'prod', [[.7, .32], [.95, .44], [1.2, .56], [1.45, .70], [2.7, .70], [3.3, .32]]),
        C('prod', .32, 'ia', .44, 1.6, 2.0), D('ia', .44, 2.0), R('investigation A', 'ia', [[2.0, .44]], 3.9, true),
        C('prod', .44, 'ib', .56, 2.0, 2.4), D('ib', .56, 2.4), R('investigation B', 'ib', [[2.4, .56]], 3.9, true), T('ib', .62, 2.45, 'check, diff, commit findings', true, 'left'),
        T('prod', .96, 3.4, 'reset: the ref moves back, no new commit', true, 'right'), T('ia', .96, 4.0, 'investigation refs released', true, 'right')]},
    data_agent: {lanes: [['bx', 'batch', 'abandoned step', 'c'], ['bt', 'batch', 'before rebase', 'a'], ['bt2', 'batch', 'rebased', 'a'], ['wh', 'warehouse', 'database', 'main']], items: [
        L('wh', .04, .12, 0, .15), ...run('wh', [[.12, .15], [.25, .4]]), T('wh', .18, .45, 'base', false),
        C('wh', .25, 'bt', .36, .5, .9, 3.1), ...run('bt', [[.36, .9], [.44, 1.2]], 3.1), T('bt', .40, 1.3, 'ingest and validate', false),
        C('bt', .44, 'bx', .52, 1.2, 1.5, 2.1), D('bx', .52, 1.5, 2.1), T('bx', .56, 2.15, 'optional retry: the ref rewinds, the step is redone', true, 'left'),
        R('batch', 'bt', [[.9, .36], [1.2, .44], [1.5, .52, 'bx'], [1.8, .52, 'bx'], [2.1, .44, 'bt'], [2.3, .44], [2.6, .60], [3.0, .60], [3.5, .84, 'bt2']], 4.1, true),
        ...run('bt', [[.44, 2.1, false], [.60, 2.6]], 3.1),
        ...run('wh', [[.25, .4, false], [.40, 1.6], [.55, 2.2]]), T('wh', .40, 1.65, 'other batch', false),
        R('warehouse', 'wh', [[.4, .25], [1.3, .25], [1.6, .40], [2.2, .55], [3.6, .55], [4.0, .84, 'bt2']]),
        T('wh', .96, 2.5, 'analytics paused; rebase, then fast-forward merge', false, 'right'),
        C('wh', .55, 'bt2', .64, 2.7, 3.1), ...run('bt2', [[.64, 3.1], [.74, 3.3], [.84, 3.5]])]},
};
// The measured side of a definition: completion times in the largest comparison group.
function workflowResults(key) {
    const g = macroGroupsShown[0], box = el('workflow-results');
    if (!g) { box.replaceChildren(node('p', 'fine', 'No results yet.')); return; }
    const {timed} = groupStats(g, key);
    const missing = g.rows.filter(c => completion(c, key) === null).map(c => ({e: c, word: STATUS[statusOf(c, key)].label}));
    const list = resultBars(`${workflow(key).name}: completion time per system`, timed.map(({c, t}) => ({e: c, t})), missing);
    const note = node('p', 'fine'); note.append(parts(['Lower is faster', (g.rows[0].environment || {}).cpu, ...machineParts(g.rows[0])])); box.replaceChildren(list, note);
}
function renderExplainer(keys) {
    if (!keys.includes(state.workflow)) state.workflow = keys[0];
    el('selectors-workflow').replaceChildren(...keys.map(k => explorerOption(`workflow-${k}`, workflow(k).name, SCENARIOS[k], `${workflow(k).name} (${SCENARIOS[k]})`,
        state.workflow === k, () => { state.workflow = k; writeHash(workflowsHash()); })));
    const current = workflow(state.workflow);
    el('workflow-name').textContent = current.name; el('workflow-id').textContent = SCENARIOS[state.workflow];
    el('workflow-caption').textContent = current.caption || ''; el('workflow-caption').hidden = !current.caption;
    el('workflow-ops').replaceChildren(...(current.ops || []).map(op => node('li', '', op))); el('workflow-ops-block').hidden = !current.ops;
    el('workflow-alt').textContent = current.alt || ''; el('workflow-text').hidden = !current.alt;
    workflowResults(state.workflow);
    const host = el('workflow-scene');
    if (!current.alt || !SCENES[state.workflow]) { unmountScene(); host.replaceChildren(node('p', 'diagram-missing', 'Diagram not available. See the attempt details.')); return; }
    if (scene.key !== state.workflow) mountScene(state.workflow, SCENES[state.workflow], `${current.name}, branch history: ${current.alt}`, host, () => diagramImage(state.workflow, current));  // a re-render of the same workflow neither restarts nor rebuilds the scene
}
