import {feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function countFields(spec, basis, current) {
  const config = spec.staffing || {};
  return el('fieldset', {class:'count-grid'}, el('legend', {}, '新批次各去向件数'),
    p(config.counts_help || '录入新批次各去向件数。', 'subtle'),
    basis.destinations.map(name => {
      const id = `count-${name}`;
      return el('label', {class:'field', for:id}, el('span', {}, name),
        el('input', {id, name:`count:${name}`, type:'number', min:'0', step:'1', required:true,
          value:current?.[name] ?? '', inputmode:'numeric'}));
    }));
}

function collect(form, spec, basis) {
  const data = new FormData(form);
  const counts = {};
  for (const name of basis.destinations) {
    const raw = data.get(`count:${name}`);
    if (raw === null || raw === '' || !/^\d+$/.test(raw.trim())) {
      throw new Error(`去向「${name}」的件数须为非负整数`);
    }
    counts[name] = Number(raw);
  }
  return {counts, tolerance:Number(data.get('tolerance'))};
}

export async function staffingPage(route, refresh) {
  const {spec} = workspace;
  const basisList = await workspace.staffingBasis();
  const selectedId = route.id || basisList[0]?.id;
  const basis = basisList.find(item => item.id === selectedId);
  const error = feedback();
  const title = spec.pages[6] || '排班推算';
  if (!basisList.length) {
    return panel(title, empty('还没有可用于沿用的验收。先完成一次分流验收。', '#run', '前往分流验收'));
  }
  const chooser = el('label', {class:'field', for:'basis-select'}, el('span', {}, '验收依据'),
    el('select', {id:'basis-select', onChange:event => { location.hash = `#staffing/${event.target.value}`; }},
      basisList.map(item => el('option', {value:item.id, selected:item.id === basis.id},
        `${item.name} · 留出 ${Object.values(item.totals).reduce((a, b) => a + b, 0)} 件`))));

  if (!session.canWrite) {
    return el('div', {class:'two-col'},
      panel(title, chooser, p('只读角色可以查看验收依据与已有推算，不能发起新的推算。', 'subtle'),
        basisPreview(spec, basis)),
      el('aside', {class:'sidebar-panel'}, panel('口径', el('ul', {class:'method-list'},
        spec.methodology.slice(0, 2).map(text => el('li', {}, text))), methodology(spec))));
  }

  const submit = button('发起推算', {type:'submit', class:'primary', 'data-testid':'start-staffing'});
  const form = el('form', {class:'fields', 'data-testid':'staffing-form', onSubmit:handler(submit, error, async event => {
    const payload = collect(event.target, spec, basis);
    const item = await workspace.startStaffing({
      basis_run_id:basis.id, name:event.target.elements.name.value.trim() || `接件量推算 ${basis.name}`,
      counts:payload.counts, params:{tolerance:payload.tolerance},
    });
    location.hash = `#staffing/${basis.id}/${item.id}`;
  })},
    chooser,
    field({key:'name', label:'推算名称', type:'string', max_length:120}, `接件量推算 ${basis.name}`),
    countFields(spec, basis),
    field({...spec.staffing.tolerance, type:'number'}, spec.staffing.tolerance.default),
    el('div', {class:'actions'}, submit), error);

  const runId = route.offset ? Number(route.offset) : null;
  const result = runId ? await renderRun(runId, error, refresh) : null;
  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'}, panel(title,
      p(spec.staffing.basis_desc || '选定一份已完成验收，沿用其分流去向比例。', 'subtle'), form, error), result),
    el('aside', {class:'sidebar-panel'}, panel('验收依据流向', basisPreview(spec, basis)),
      panel('推算口径', methodology(spec))));
}

function methodology(spec) {
  return el('div', {}, el('ul', {class:'method-list'}, [
    '各队件数取非负整数，合计等于新批次来信总数。',
    '每个去向的推算件数与录入件数之差不超过统一容差。',
    '人工分诊作为独立去向参与约束，不并入业务组。',
    '上下界为全部可行整数分配中该队的最小与最大接件量。',
  ].map(text => el('li', {}, text))),
  p(spec.staffing.counts_help || '', 'subtle'));
}

function basisPreview(spec, basis) {
  const total = Object.values(basis.totals).reduce((a, b) => a + b, 0);
  const rows = basis.destinations.map(name => {
    const hits = basis.origins.reduce((sum, origin) => sum + (basis.counts[origin][name] || 0), 0);
    return {destination:name, hits, ratio:`${(hits / total * 100).toFixed(1)}%`};
  });
  const teamRows = basis.origins.map(origin => ({
    team:origin, total:basis.totals[origin],
    share:`${(basis.totals[origin] / total * 100).toFixed(1)}%`,
  }));
  return el('div', {},
    table([{key:'destination', label:'去向'}, {key:'hits', label:'留出件数'}, {key:'ratio', label:'占比'}],
      rows, {caption:'验收依据去向', pageSize:12}),
    table([{key:'team', label:'归口队'}, {key:'total', label:'留出件数'}, {key:'share', label:'占比'}],
      teamRows, {caption:'验收依据归口', pageSize:12}));
}

async function renderRun(id, message, refresh) {
  const job = await workspace.getRun(id);
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p', {role:'status'}, '后台正在按检查点计算，完成后会展示各队上下界。');
    poll(() => workspace.getRun(id), finished, () => location.hash === original)
      .then(found => {
        if (found && status.isConnected && location.hash === original) refresh();
        else if (status.isConnected) status.textContent = '仍在计算，可稍后刷新。';
      })
      .catch(err => { if (status.isConnected) status.textContent = err.message; });
    return panel(job.name, pill(STATUS[job.status], job.status), status,
      button('刷新', {class:'quiet', onClick:refresh}));
  }
  if (job.status === 'failed' || !job.result) {
    return panel(job.name, pill(STATUS[job.status], job.status), p(job.error, 'feedback'),
      link('返回调整输入', `#staffing/${job.basis_run_id}`, {class:'quiet'}));
  }
  const result = job.result;
  return el('section', {class:'analysis', 'data-testid':'staffing-complete'},
    el('div', {class:'result-head'}, p(job.name, 'eyeline'), h(2, result.headline),
      p(result.note, 'intro-copy'), el('div', {class:'actions'}, pill(STATUS[job.status], job.status))),
    message,
    metrics(result.metrics),
    (result.sections || []).map(item => panel(item.title, p(item.text))),
    result.tables.map(item => panel(item.title, table(item.columns, item.rows,
      {caption:item.title, empty:'无数据'}))));
}
