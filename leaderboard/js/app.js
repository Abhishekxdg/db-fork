/* Boot, state, routing and render. The phase is build configuration written into data.js; nothing in the URL can change it. */
const PROD = (typeof site === 'undefined' ? 'prod' : site.phase) !== 'local';
const DOCS = PROD ? 'https://github.com/elaineang/db-fork/blob/main/leaderboard/README.md' : 'README.md';
const allOn = values => Object.fromEntries(values.map(v => [v, true]));
// The page opens on the agent workflows whenever their data is built. Each suite keeps its own selections.
const state = {
    suite: PROD || MACRO.built ? 'macro' : 'micro', rank: Object.keys(SCENARIOS)[0], rankDesc: false, workflow: Object.keys(SCENARIOS)[0], campaign: new Set(),
    mode: 'first', sort: 'branch', sortDesc: false, ticked: new Set(suiteRows), op: allRows[0].op, hosted: allOn(Object.keys(HOSTING)),
    system: allOn(uniq(data.map(e => e.system))), machine: allOn(uniq(data.map(e => e.machine))), tag: allOn(uniq(data.flatMap(e => e.tags))),
};
const decoded = text => { try { return decodeURIComponent(text); } catch { return null; } };  // a mangled link is treated as absent, never as a crash; a key counts only when it is the table's own (not "constructor")
function parseHash() {
    const [path, query = ''] = window.location.hash.slice(1).split('?'), params = {};
    for (const pair of query.split('&')) {
        if (!pair) continue;
        const [k, v = ''] = pair.split('='), key = decoded(k), value = decoded(v);
        if (key !== null && value !== null) params[key] = value;
    }
    return {path, params};
}
// Routes: #leaderboard (suite unchanged), #agent-workflows (?workflow= ranks by that workflow, ?order=desc lists it slowest first, ?campaign= picks older campaigns),
// #workflows?workflow= (one workflow's definition), #macro-guide, and the microbench's #microbench (?try=best, ?sort=, ?order=desc,
// ?workloads= the counted read/write workloads, and ?hosting=, ?systems=, ?machines=, ?capabilities= the values each filter keeps),
// #dataset?operation= (one operation's definition; the bare link opens the dataset details) and #methodology.
const FILTER_PARAMS = {hosted: 'hosting', system: 'systems', machine: 'machines', tag: 'capabilities'};
function applyHash() {
    const {path, params} = parseHash();
    if (path === 'workflows') { state.suite = 'macro'; if (Object.hasOwn(SCENARIOS, params.workflow)) state.workflow = params.workflow; return; }
    if (['microbench', 'dataset', 'methodology'].includes(path)) state.suite = 'micro';
    if (path === 'dataset' && allRows.some(r => r.op === params.operation)) state.op = params.operation;
    if (path === 'microbench') {
        state.mode = params.try === 'best' ? 'best' : 'first';
        state.sort = Object.hasOwn(SORTS, params.sort) ? params.sort : 'branch'; state.sortDesc = params.order === 'desc';
        const named = (params.workloads || '').split(',').map(op => VIEWS.data.rows.find(r => r.op === op)).filter(Boolean);
        state.ticked = new Set([...VIEWS.branch.rows, ...named.length ? named : VIEWS.data.rows]);
        for (const [key, param] of Object.entries(FILTER_PARAMS)) {
            const kept = param in params ? new Set(params[param].split(',')) : null;
            for (const value of Object.keys(state[key])) state[key][value] = !kept || kept.has(value);
        }
    }
    if (path === 'macro-guide') state.suite = 'macro';
    if (path !== 'agent-workflows') return;
    state.suite = 'macro';
    state.rank = Object.hasOwn(SCENARIOS, params.workflow) ? params.workflow : Object.keys(SCENARIOS)[0]; state.rankDesc = params.order === 'desc';
    state.campaign = new Set((params.campaign || '').split(',').filter(Boolean));
}
// A board's link carries only what differs from its default view, so the plain link stays plain.
const query = parts => parts.filter(Boolean).map((p, i) => (i ? '&' : '?') + p).join('');
const kept = map => Object.keys(map).filter(v => map[v]);
const boardHash = () => state.suite === 'macro'
    ? '#agent-workflows' + query([state.rank !== Object.keys(SCENARIOS)[0] && `workflow=${state.rank}`, state.rankDesc && 'order=desc', state.campaign.size && `campaign=${[...state.campaign].join(',')}`])
    : '#microbench' + query([state.mode === 'best' && 'try=best', state.sort !== 'branch' && `sort=${state.sort}`, state.sortDesc && 'order=desc',
        countedData().length < VIEWS.data.rows.length && `workloads=${countedData().map(r => r.op).join(',')}`,
        ...Object.entries(FILTER_PARAMS).map(([key, param]) => kept(state[key]).length < Object.keys(state[key]).length && `${param}=${kept(state[key]).map(encodeURIComponent).join(',')}`)]);
const workflowsHash = () => `#workflows?workflow=${state.workflow}`;
const operationHash = () => `#dataset?operation=${state.op}`;
// The page records its own state in the URL without adding history entries, so Back leaves the page instead of replaying
// every sort and tab switch. A link the viewer follows adds its entry as usual.
function writeHash(hash) { if (window.location.hash !== hash) window.history.replaceState(null, '', hash); }
// A link to a section opens it, brings it into view unless it is already on screen, and, when the viewer followed it,
// moves keyboard focus there: to the chosen definition when the link names one, otherwise to the section's heading.
const TARGETS = {leaderboard: 'leaderboard', 'agent-workflows': 'leaderboard', microbench: 'leaderboard', workflows: 'workflows', dataset: 'dataset', methodology: 'methodology', 'macro-guide': 'macro-guide'};
const HEADINGS = {leaderboard: 'leaderboard-title', workflows: 'workflows-title', dataset: 'dataset-title', methodology: 'methodology-summary', 'macro-guide': 'macro-guide-summary'};
function reveal(focus = false) {
    const {path, params} = parseHash(), id = TARGETS[path]; if (!id) return;
    if (id === 'dataset') { if (!params.operation) el('dataset-details').open = true; } else if (id === 'methodology' || id === 'macro-guide') el(id).open = true;
    const box = el(id).getBoundingClientRect();
    if (box.top < 0 || box.top > window.innerHeight * .6) el(id).scrollIntoView({block: 'start'});
    if (focus) el(id === 'dataset' && params.operation ? `operation-${state.op}` : id === 'workflows' && params.workflow ? `workflow-${state.workflow}` : HEADINGS[id]).focus({preventScroll: true});
}
// The suites are tabs on the board card: arrow keys, Home and End move between them, and the active tab alone is in the tab order.
const SUITES = {macro: ['Macrobench', () => plural(Object.keys(SCENARIOS).length, 'agent workflow')], micro: ['Microbench', () => plural(suiteRows.length, 'operation')]};
function selectSuite(k) { state.suite = k; writeHash(boardHash()); render(); el(`tab-${k}`).focus(); }
function renderTabs() {
    el('selectors-suite').replaceChildren(...Object.entries(SUITES).map(([k, [name, sub]]) => {
        const tab = node('button', 'suite-tab'); tab.type = 'button'; tab.id = `tab-${k}`; tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', String(state.suite === k)); tab.setAttribute('aria-controls', `${k}-view`); tab.tabIndex = state.suite === k ? 0 : -1;
        tab.append(node('span', 'suite-name', name), node('span', 'suite-desc', sub()));
        tab.addEventListener('click', () => selectSuite(k));
        tab.addEventListener('keydown', event => { const to = {ArrowLeft: 'macro', ArrowRight: 'micro', Home: 'macro', End: 'micro'}[event.key]; if (to) { event.preventDefault(); selectSuite(to); } });
        return tab;
    }));
}
function render() {
    scrollers = [];
    el('nav-board').href = boardHash(); el('nav-workflows').href = workflowsHash(); el('nav-operations').href = operationHash();
    if (!PROD) el('nav-run').href = state.suite === 'macro' ? 'run.html#agent-workflows' : 'run.html';  // the wizard opens on the suite in view
    renderTabs();
    for (const k of Object.keys(SUITES)) el(`${k}-view`).hidden = el(`${k}-more`).hidden = state.suite !== k;
    if (state.suite === 'macro') renderMacro(); else renderMicro();
    syncScrollHints();
}

for (const id of ['doc-nav', 'doc-scoring', 'doc-macro', 'doc-footer']) el(id).href = el(id).getAttribute('href').replace(/^README\.md/, DOCS);
if (PROD) el('nav-run').remove();  // production has no run wizard
wireDetails(); wireMicro();
// A header link to the section already in the URL fires no hashchange, so it reveals and focuses the section itself.
for (const id of ['nav-board', 'nav-workflows', 'nav-operations']) el(id).addEventListener('click', event => {
    const href = el(id).href; if (href.slice(href.indexOf('#')) !== window.location.hash) return;
    event.preventDefault(); reveal(true);
});
window.addEventListener('resize', syncScrollHints);
renderDataset(); renderStatusRules();
const routeKey = () => `${boardHash()}/${state.workflow}/${state.op}`;
window.addEventListener('hashchange', () => {  // only a followed link or Back and Forward change the hash now
    const before = routeKey();
    applyHash(); if (before !== routeKey()) render(); reveal(true);
});
applyHash();
render();
reveal();
