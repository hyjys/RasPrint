'use strict';

const element = (id) => document.getElementById(id);
const printerSelect = element('printer-select');
const fileInput = element('document-file');
const printForm = element('upload-form');
const printButton = element('submit-print');
const adminForm = element('login-form');
const themeForm = element('theme-form');
const themeSelect = element('theme-select');
const customThemeForm = element('custom-theme-form');
const serverSettingsForm = element('server-settings-form');
const hotspotForm = element('hotspot-form');
const passwordChangeForm = element('password-change-form');
const cancelDialog = element('cancel-dialog');
const languageSelect = element('language-select');

const routes = { dashboard: 'dashboardDescription', queue: 'queueDescription', admin: 'adminDescription' };
const printerStates = {
  idle: ['printerIdle', 'success'],
  processing: ['printing', 'primary'],
  stopped: ['printerStopped', 'danger'],
  unknown: ['stateUnknown', 'muted'],
};
const jobStates = {
  pending: ['jobPending', 'muted'],
  held: ['jobHeld', 'warning'],
  processing: ['printing', 'primary'],
  stopped: ['printerStopped', 'warning'],
  canceled: ['jobCanceled', 'muted'],
  aborted: ['jobAborted', 'danger'],
  completed: ['jobCompleted', 'success'],
  unknown: ['stateUnknown', 'muted'],
};
const quietReasons = new Set(['none', 'job-incoming', 'job-data-insufficient', 'job-queued', 'job-printing', 'job-completed-successfully', 'job-canceled-by-user']);
const activeJobs = new Set(['pending', 'held', 'processing', 'stopped']);
const finishedJobs = new Set(['canceled', 'aborted', 'completed']);
const themeKeys = [
  'primary', 'primary_hover', 'primary_light', 'primary_text', 'sidebar', 'background', 'surface',
  'panel', 'border', 'text', 'muted', 'success', 'success_light', 'danger', 'danger_light',
  'warning', 'warning_light',
];
const themeColorLabels = {
  primary: 'colorPrimary', primary_hover: 'colorPrimaryHover', primary_light: 'colorPrimaryLight',
  primary_text: 'colorPrimaryText', sidebar: 'colorSidebar', background: 'colorBackground',
  surface: 'colorSurface', panel: 'colorPanel', border: 'colorBorder', text: 'colorText',
  muted: 'colorMuted', success: 'colorSuccess', success_light: 'colorSuccessLight', danger: 'colorDanger',
  danger_light: 'colorDangerLight', warning: 'colorWarning', warning_light: 'colorWarningLight',
};
const hex = /^#[\da-f]{6}$/i;
const hotspotHostname = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const svgNamespace = 'http://www.w3.org/2000/svg';
const feedbackIds = [
  'upload-feedback', 'login-feedback', 'theme-feedback', 'manage-feedback',
  'server-settings-feedback', 'hotspot-feedback', 'custom-theme-feedback', 'password-change-feedback',
];
const utf8 = new TextEncoder();

let language = initialLanguage();
let dateFormat;

let view = 'dashboard';
let dashboard = null;
let dashboardFailed = false;
let dashboardRequest = null;
let pollTimer;
let session = { authenticated: false };
let file = null;
let printBusy = false;
let activeFilter = 'all';
let cancelTarget = null;
let maxUpload = 25;
let extensions = ['pdf', 'jpg', 'jpeg', 'png', 'txt'];
let adminSettings = null;
let hotspotChange = null;
let hotspotTimer = 0;
let installedThemes = [];
let themeBusy = false;
let themeDraft = {};
let themeEditId = null;
let themePreviewLogo = '/static/logo.png';
let themeObjectUrl = null;

function initialLanguage() {
  try {
    const stored = localStorage.getItem('rasprint-language');
    if (stored && Object.hasOwn(localeTags, stored)) return stored;
  } catch {
    // Use the browser language when storage is restricted.
  }
  return (navigator.languages || [navigator.language]).map(resolveLocale).find(Boolean) || 'en';
}

function localize(pair) {
  return pair[localeIndex[language]] ?? pair[1];
}

function t(key, fields = {}) {
  return localize(messages[key]).replace(/\{(\w+)\}/g, (_, name) => String(fields[name] ?? ''));
}

function renderLanguage() {
  document.documentElement.lang = language;
  languageSelect.value = language;
  dateFormat = new Intl.DateTimeFormat(localeTags[language], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  for (const item of document.querySelectorAll('[data-i18n]')) item.textContent = t(item.dataset.i18n);
  for (const item of document.querySelectorAll('[data-i18n-label]')) item.setAttribute('aria-label', t(item.dataset.i18nLabel));
  updateThemeButtons();
  renderThemePreview();
  renderPageHeading();
  renderFile();
  renderInfo();
  printButton.querySelector('span').textContent = t(printBusy ? 'sending' : 'print');
  if (dashboard) {
    renderPrinters();
    if (dashboardFailed) optionalText('connection-error', networkNotice());
  } else if (dashboardFailed) {
    selectOptions(printerSelect, [['', t('connectionError')]], '');
    renderSelectedPrinter();
    optionalText('connection-error', networkNotice());
  } else {
    selectOptions(printerSelect, [['', t('checkingPrinter')]], '');
    text('printer-name', t('checking'));
    text('printer-state', t('checkingConnection'));
    text('printer-meta', t('checkingPrinterServer'));
    element('ink-levels').replaceChildren(node('p', 'small muted', t('checkingInk')));
  }
  renderJobs();
}

async function changeLanguage() {
  language = languageSelect.value;
  try { localStorage.setItem('rasprint-language', language); } catch {
    // The current page still uses the selected language.
  }
  for (const id of feedbackIds) feedback(id);
  renderLanguage();
  if (dashboardRequest) await dashboardRequest;
  refreshDashboard();
  if (session.authenticated && view === 'admin') {
    loadAdmin();
  }
}

async function request(path, options = {}) {
  const { method = 'GET', body, admin = false } = options;
  const headers = new Headers({ Accept: 'application/json', 'Accept-Language': language, 'X-RasPrint-Language': language });
  if (admin && method !== 'GET' && session.csrf_token) headers.set('X-CSRF-Token', session.csrf_token);
  if (body && !(body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    body: body && !(body instanceof FormData) ? JSON.stringify(body) : body,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (admin && response.status === 401) showSession({ authenticated: false });
    const error = new Error(typeof result.detail === 'string' ? result.detail : t('serverError'));
    error.status = response.status;
    throw error;
  }
  return result;
}

function node(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined) item.textContent = value;
  return item;
}

function icon(name) {
  const svg = document.createElementNS(svgNamespace, 'svg');
  const use = document.createElementNS(svgNamespace, 'use');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function text(id, value) {
  const target = element(id);
  const label = String(value ?? '');
  if (target.textContent !== label) target.textContent = label;
}

function optionalText(id, value) {
  text(id, value);
  element(id).hidden = !value;
}

function feedback(id, message = '', kind = '') {
  const target = element(id);
  text(id, message);
  target.dataset.kind = kind;
  if (id === 'manage-feedback') target.hidden = !message;
}

function errorText(error) {
  return error.status ? error.message : t('networkError');
}

function badge(label, tone) {
  return node('span', `badge tone-${tone}`, label);
}

function reasonList(values = []) {
  return [...new Set(values.filter(Boolean).map((value) => value.replace(/-(error|warning|report)$/, '')))]
    .filter((value) => !quietReasons.has(value))
    .map((value) => Object.hasOwn(reasonMessages, value) ? localize(reasonMessages[value]) : value);
}

function percentage(value) {
  return Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value) : null;
}

function meter(value, label) {
  const progress = node('progress', 'progress', `${value}%`);
  progress.max = 100;
  progress.value = value;
  progress.setAttribute('aria-label', label);
  return progress;
}

function tinyState(title, description, symbol) {
  const box = node('div', 'empty-state');
  const glyph = node('span', 'empty-icon');
  glyph.append(icon(symbol));
  box.append(glyph, node('strong', '', title));
  if (description) box.append(node('p', '', description));
  return box;
}

const mobile = matchMedia('(max-width: 760px)');

function openMenu() {
  if (!mobile.matches) return;
  element('sidebar').classList.add('open');
  element('overlay').hidden = false;
  element('open-menu').setAttribute('aria-expanded', 'true');
  document.body.classList.add('drawer-open');
  document.querySelector('main').inert = true;
  document.querySelector('.mobile-header').inert = true;
  element('close-menu').focus();
}

function closeMenu(returnFocus = false) {
  if (!element('sidebar').classList.contains('open')) return;
  element('sidebar').classList.remove('open');
  element('overlay').hidden = true;
  element('open-menu').setAttribute('aria-expanded', 'false');
  document.body.classList.remove('drawer-open');
  document.querySelector('main').inert = false;
  document.querySelector('.mobile-header').inert = false;
  if (returnFocus) element('open-menu').focus();
}

function showView(focus = true) {
  const id = location.hash.slice(1);
  if (id === 'main') return;
  view = Object.hasOwn(routes, id) ? id : 'dashboard';
  closeMenu();
  for (const key of Object.keys(routes)) element(key).hidden = key !== view;
  for (const link of document.querySelectorAll('.nav-link')) {
    if (link.hash === `#${view}`) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  renderPageHeading();
  if (focus) {
    window.scrollTo(0, 0);
    element('page-title').focus({ preventScroll: true });
  }
  renderJobs();
  if (view === 'admin') loadSession();
}

function renderPageHeading() {
  text('page-title', t(view));
  text('page-subtitle', t(routes[view]));
}

function setFile(nextFile, error = '') {
  file = nextFile;
  if (!file) fileInput.value = '';
  renderFile();
  feedback('upload-feedback', error, error ? 'error' : '');
  updateSubmit();
}

function renderFile() {
  const title = element('file-label-title');
  text('file-label-title', file ? file.name : t('chooseFile'));
  if (file) title.title = file.name;
  else title.removeAttribute('title');
  text('file-label-subtitle', file ? t('changeFile', { size: (file.size / (1024 * 1024)).toFixed(2) }) : t('dropFile'));
  element('drop-icon-use').setAttribute('href', file ? '#i-file' : '#i-up');
  element('clear-file').hidden = !file;
}

function chooseFile(nextFile) {
  if (!nextFile) return;
  const suffix = nextFile.name.split('.').pop().toLowerCase();
  if (!nextFile.name.includes('.') || !extensions.includes(suffix)) {
    setFile(null, t('invalidFileType'));
  } else if (!nextFile.size || nextFile.size > maxUpload * 1024 * 1024) {
    setFile(null, t('invalidFileSize', { size: maxUpload }));
  } else {
    setFile(nextFile);
  }
}

function selectedPrinter() {
  return dashboard?.printers.find((printer) => printer.id === printerSelect.value);
}

function updateSubmit() {
  printButton.disabled = printBusy || !file || !selectedPrinter()?.accepting || dashboardFailed || Boolean(dashboard?.printer_error);
}

async function printDocument(event) {
  event.preventDefault();
  if (printButton.disabled) return;
  if (!element('copies').validity.valid) {
    feedback('upload-feedback', t('invalidCopies'), 'error');
    element('copies').focus();
    return;
  }
  const form = new FormData(printForm);
  form.set('file', file, file.name);
  for (const key of ['printer', 'media', 'sides', 'print_color_mode']) {
    if (!form.get(key)) form.delete(key);
  }

  printBusy = true;
  updateSubmit();
  element('upload-fields').disabled = true;
  printButton.querySelector('span').textContent = t('sending');
  feedback('upload-feedback', t('sendingFile'));
  try {
    const result = await request('/api/jobs', { method: 'POST', body: form });
    setFile(null);
    feedback('upload-feedback', t('jobAdded', { id: result.id }), 'success');
    await refreshDashboard();
  } catch (error) {
    feedback('upload-feedback', errorText(error), 'error');
  } finally {
    printBusy = false;
    element('upload-fields').disabled = false;
    printButton.querySelector('span').textContent = t('print');
    updateSubmit();
  }
}

function selectOptions(target, options, preferred) {
  const current = Array.from(target.options, (option) => [option.value, option.label]);
  if (JSON.stringify(current) !== JSON.stringify(options)) {
    target.replaceChildren(...options.map(([id, name]) => new Option(name, id)));
  }
  if (options.some(([id]) => id === preferred)) target.value = preferred;
}

function renderPrinters() {
  if (!dashboard) return;
  const choices = dashboard.printers.map((printer) => [printer.id, printer.name || printer.id]);
  const preserved = choices.some(([id]) => id === printerSelect.value) ? printerSelect.value : dashboard.default_printer;
  selectOptions(printerSelect, choices.length ? choices : [['', t('noPrinters')]], preserved || choices[0]?.[0]);
  printerSelect.disabled = choices.length === 0;
  renderSelectedPrinter();
}

function renderSelectedPrinter() {
  const printer = selectedPrinter();
  const [label, tone] = printer
    ? printerStates[printer.state] || printerStates.unknown
    : [dashboardFailed || dashboard?.printer_error ? 'connectionError' : 'noPrinterRegistered', 'muted'];
  const status = element('printer-state');
  status.className = `badge tone-${tone}`;
  status.textContent = t(label);
  text('printer-name', printer?.name || printer?.id || t('noPrinterSelected'));
  optionalText('printer-model', printer?.model && printer.model !== printer.name ? printer.model : '');
  const meta = printer
    ? [printer.id === dashboard.default_printer ? t('defaultPrinter') : '', printer.accepting ? t('acceptingJobs') : t('rejectingJobs')].filter(Boolean).join(' · ')
    : t('addCupsPrinter');
  text('printer-meta', meta);
  const translated = reasonList(printer?.reasons);
  const reasonItems = translated.map((reason) => {
    const item = node('li', 'tone-warning');
    item.append(icon('alert'), node('span', '', reason));
    return item;
  });
  element('printer-reasons').replaceChildren(...reasonItems);
  element('printer-reasons').hidden = !reasonItems.length;
  optionalText('printer-message', printer?.message && !translated.includes(printer.message) ? printer.message : '');
  renderInk(printer?.ink || []);
  updateSubmit();
}

function renderInk(inks) {
  const target = element('ink-levels');
  if (!inks.length) {
    target.replaceChildren(node('p', 'small muted', t('noInkInfo')));
    return;
  }
  const items = inks.map((ink, index) => {
    const value = percentage(ink.level);
    const label = ink.name || t('inkNumber', { number: index + 1 });
    const row = node('div', 'ink-row');
    const head = node('div', 'ink-label');
    const name = node('span', 'ink-name');
    name.append(node('span', 'ink-dot'), node('span', '', label));
    head.append(name, node('span', 'ink-level', value === null ? t('unavailable') : `${value}%`));
    row.append(head, value === null ? node('div', 'unknown-meter') : meter(value, t('inkMarker', { label })));
    if (hex.test(ink.color)) row.style.setProperty('--ink', ink.color);
    return row;
  });
  target.replaceChildren(...items);
}

function jobRow(job, manage) {
  const row = node('tr');
  const title = job.title || t('jobNumber', { id: job.id });
  const fileCell = node('td');
  const fileGroup = node('div', 'job-file');
  const glyph = node('span', 'job-icon');
  const fileCopy = node('div');
  const printerName = dashboard.printers.find((printer) => printer.id === job.printer)?.name || job.printer;
  glyph.append(icon('file'));
  fileCopy.append(node('strong', 'job-name', title), node('span', 'job-meta', [printerName, `#${job.id}`].filter(Boolean).join(' · ')));
  fileGroup.append(glyph, fileCopy);
  fileCell.append(fileGroup);
  const statusCell = node('td');
  const [label, tone] = jobStates[job.state] || jobStates.unknown;
  statusCell.append(badge(t(label), tone));
  const detail = [...new Set([job.message, ...reasonList(job.reasons)].filter(Boolean))].join(' · ');
  if (detail) statusCell.append(node('span', 'job-status-detail', detail));
  const progressCell = node('td');
  const progress = percentage(job.progress);
  if (progress !== null) {
    const progressGroup = node('div', 'job-progress');
    progressGroup.append(meter(progress, t('progressFor', { title })), node('span', '', `${progress}%`));
    progressCell.append(progressGroup);
  } else {
    progressCell.append(node('span', 'muted', t('unavailable')));
  }
  const timeCell = node('td', 'job-time');
  const timestamp = job.created_at ? new Date(job.created_at) : null;
  if (timestamp && !Number.isNaN(timestamp.getTime())) {
    const time = node('time', '', dateFormat.format(timestamp));
    time.dateTime = job.created_at;
    timeCell.append(time);
    const mobileTime = time.cloneNode(true);
    mobileTime.className = 'job-meta mobile-time';
    fileCopy.append(mobileTime);
  } else timeCell.textContent = '—';
  row.append(fileCell, statusCell, progressCell, timeCell);
  if (manage) {
    const actionCell = node('td');
    const cancel = node('button', 'button job-cancel', t('cancel'));
    cancel.type = 'button';
    cancel.dataset.job = job.id;
    cancel.setAttribute('aria-label', t('cancelJobNamed', { title }));
    cancel.addEventListener('click', () => {
      cancelTarget = job.id;
      cancelDialog.returnValue = 'close';
      text('cancel-filename', title);
      cancelDialog.showModal();
    });
    actionCell.append(cancel);
    row.append(actionCell);
  }
  return row;
}

function jobTable(target, jobs, manage = false) {
  if (!jobs.length) {
    const title = dashboardFailed || dashboard?.printer_error ? t('queueUnavailable') : dashboard ? t('noJobs') : t('checkingQueue');
    const subtitle = dashboard && !manage && view === 'dashboard' && !dashboard.printer_error ? t('jobsAppear') : '';
    target.replaceChildren(tinyState(title, subtitle, 'list'));
    return;
  }
  const focusing = document.activeElement?.dataset.job;
  const scroller = node('div', 'table-scroll');
  const table = node('table', 'job-table');
  const head = table.createTHead().insertRow();
  for (const [index, title] of [t('fileColumn'), t('stateColumn'), t('progressColumn'), t('timeColumn'), ...(manage ? [t('manageColumn')] : [])].entries()) {
    const field = node('th', index === 3 ? 'time-column' : '', title);
    field.scope = 'col';
    head.append(field);
  }
  table.createTBody().append(...jobs.map((job) => jobRow(job, manage)));
  scroller.append(table);
  target.replaceChildren(scroller);
  if (focusing) Array.from(target.querySelectorAll('[data-job]')).find((button) => button.dataset.job === focusing)?.focus({ preventScroll: true });
}

function renderJobs() {
  const jobs = [...(dashboard?.jobs || [])].sort((first, second) => second.id - first.id);
  const active = jobs.filter((job) => activeJobs.has(job.state));
  const count = element('nav-job-count');
  count.hidden = !active.length;
  count.textContent = active.length;

  if (view === 'dashboard') {
    text('recent-count', dashboard ? t('jobCount', { active: active.length, total: jobs.length }) : t('checkingQueue'));
    jobTable(element('recent-jobs'), jobs.slice(0, 5));
  } else if (view === 'queue') {
    text('queue-count', dashboard ? t('jobCount', { active: active.length, total: jobs.length }) : t('checkingQueue'));
    const states = activeFilter === 'active' ? activeJobs : finishedJobs;
    const shown = activeFilter === 'all' ? jobs : jobs.filter((job) => states.has(job.state));
    jobTable(element('all-jobs'), shown);
  } else if (session.authenticated) {
    text('manage-count', dashboard ? t('manageCount', { count: active.length }) : t('checkingQueue'));
    jobTable(element('manage-jobs'), active, true);
  }
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (document.hidden) return;
  const interval = dashboard?.jobs.some((job) => activeJobs.has(job.state)) ? 5000 : 12000;
  pollTimer = window.setTimeout(refreshDashboard, interval);
}

function networkNotice(message = t('networkError')) {
  return dashboard?.printers.length ? `${message} ${t('stale')}` : message;
}

async function refreshDashboard() {
  if (dashboardRequest) return dashboardRequest;
  const reload = element('refresh');
  reload.disabled = true;
  const requestedLanguage = language;
  dashboardRequest = (async () => {
    try {
      const next = await request('/api/dashboard');
      if (requestedLanguage !== language) return;
      const showingPrevious = Boolean(next.printer_error && dashboard?.printers.length);
      dashboard = showingPrevious ? { ...dashboard, printer_error: next.printer_error, demo: next.demo } : next;
      dashboardFailed = false;
      element('demo-indicator').hidden = !dashboard.demo;
      if (adminSettings) renderHotspotAddresses();
      optionalText('connection-error', showingPrevious ? `${next.printer_error} ${t('stale')}` : next.printer_error);
      renderPrinters();
    } catch (error) {
      dashboardFailed = true;
      optionalText('connection-error', networkNotice(errorText(error)));
      if (!dashboard) {
        printerSelect.disabled = true;
        selectOptions(printerSelect, [['', t('connectionError')]], '');
        renderSelectedPrinter();
      }
    } finally {
      dashboardRequest = null;
      reload.disabled = false;
      renderJobs();
      updateSubmit();
      schedulePoll();
    }
  })();
  return dashboardRequest;
}

async function loadInfo() {
  try {
    const info = await request('/api/info');
    if (Number.isFinite(info.max_upload_mb) && info.max_upload_mb > 0) maxUpload = info.max_upload_mb;
    if (info.allowed_extensions?.length) extensions = info.allowed_extensions.map((extension) => extension.replace(/^\./, '').toLowerCase());
  } finally {
    renderInfo();
  }
}

function renderInfo() {
  const formats = extensions.map((extension) => extension.toUpperCase()).join(', ');
  text('file-format', t('fileFormats', { formats, size: maxUpload }));
  fileInput.accept = extensions.map((extension) => `.${extension}`).join(',');
}

async function loadTheme() {
  const theme = await request('/api/theme');
  const brand = theme.brand?.name || 'RasPrint';
  const logo = theme.brand?.logo || '/static/logo.png';
  document.title = brand;
  for (const name of document.querySelectorAll('.brand-name')) name.textContent = brand;
  for (const image of document.querySelectorAll('.brand-logo')) image.src = logo;
  element('favicon').href = logo;
  for (const key of themeKeys) {
    const property = `--${key.replaceAll('_', '-')}`;
    if (hex.test(theme.colors?.[key])) document.documentElement.style.setProperty(property, theme.colors[key]);
    else document.documentElement.style.removeProperty(property);
  }
}

function showSession(nextSession) {
  const signedOut = session.authenticated && !nextSession.authenticated;
  session = nextSession;
  element('login-card').hidden = session.authenticated;
  element('admin-content').hidden = !session.authenticated;
  if (signedOut) {
    adminSettings = null;
    hotspotChange = null;
    window.clearTimeout(hotspotTimer);
    installedThemes = [];
    serverSettingsForm.reset();
    hotspotForm.reset();
    resetThemeEditor();
    passwordChangeForm.reset();
    serverSettingsForm.querySelector('fieldset').disabled = true;
    hotspotForm.querySelector('fieldset').disabled = true;
    renderHotspotAddresses();
    updateThemeButtons();
  }
  renderJobs();
}

function updateThemeButtons() {
  themeSelect.disabled = themeBusy || !installedThemes.length;
  element('apply-theme').disabled = themeSelect.disabled || !themeSelect.value;
  const selected = installedThemes.find((theme) => theme.id === themeSelect.value);
  const canEdit = selected && selected.editable !== false && selected.id !== 'raspberry';
  element('remove-theme').disabled = themeSelect.disabled || !selected || selected.removable === false || selected.id === 'raspberry';
  element('edit-theme').disabled = themeSelect.disabled || !selected;
  text('edit-theme', t(canEdit ? 'themeEdit' : 'themeCopy'));
  const save = element('save-custom-theme');
  save.disabled = themeBusy;
  element('theme-editor-fields').disabled = themeBusy;
  element('reset-theme-editor').disabled = themeBusy;
  text(save.id, t(themeEditId ? 'themeSave' : 'addTheme'));
}

function renderAdminThemes(result) {
  installedThemes = result.themes;
  selectOptions(themeSelect, result.themes.map((theme) => [theme.id, theme.name]), result.active);
  updateThemeButtons();
}

async function loadAdminThemes() {
  const requestedLanguage = language;
  const result = await request('/api/admin/themes', { admin: true });
  if (requestedLanguage === language && session.authenticated) renderAdminThemes(result);
}

async function loadAdmin() {
  if (!session.authenticated) return;
  await Promise.all([
    loadAdminThemes().catch((error) => adminError('theme-feedback', error)),
    loadAdminSettings().catch((error) => {
      adminError('server-settings-feedback', error);
      if (!adminSettings && session.authenticated) adminError('hotspot-feedback', error);
    }),
  ]);
}

function adminError(id, error) {
  feedback(session.authenticated ? id : 'login-feedback', errorText(error), 'error');
}

function toggleHotspotPassword() {
  const field = element('hotspot-password-field');
  const enabled = hotspotForm.elements.secured.checked;
  field.hidden = !enabled;
  hotspotForm.elements.password.disabled = !enabled;
  if (!enabled) hotspotForm.elements.password.value = '';
}

function renderServerSettings(settings, preserveFields) {
  const controls = serverSettingsForm.elements;
  const selected = preserveFields ? controls.printer.value : (settings.printer ?? '');
  const queues = settings.queues || [];
  const options = [['', t('automaticQueue')], ...queues.map((queue) => [queue.id, queue.name || queue.id])];
  if (selected && !queues.some((queue) => queue.id === selected)) options.push([selected, selected]);
  selectOptions(controls.printer, options, selected);
  if (!preserveFields) controls.max_upload_mb.value = settings.max_upload_mb;
  optionalText('cups-settings-error', settings.printer_error);
  serverSettingsForm.querySelector('fieldset').disabled = false;
}

function renderHotspotSettings(ap, preserveFields) {
  if (ap?.error) hotspotChange = null;
  hotspotForm.querySelector('fieldset').disabled = !ap?.available || !!ap.pending;
  if (!preserveFields) {
    hotspotForm.elements.ssid.value = ap?.ssid ?? '';
    hotspotForm.elements.hostname.value = ap?.hostname ?? '';
    hotspotForm.elements.port.value = ap?.port ?? '';
    hotspotForm.elements.secured.checked = ap?.secured ?? false;
    hotspotForm.elements.password.value = '';
  }
  toggleHotspotPassword();
  renderHotspotAddresses();
  if (hotspotChange) renderHotspotFeedback();
  else if (ap?.pending) feedback('hotspot-feedback', t('hotspotPending'));
  else feedback('hotspot-feedback', ap?.error || (ap?.available ? '' : t('hotspotUnavailable')), ap?.error ? 'error' : '');
}

async function loadAdminSettings() {
  const requestedLanguage = language;
  if (!adminSettings) {
    feedback('server-settings-feedback', t('checkingSettings'));
    feedback('hotspot-feedback', t('checkingSettings'));
  }
  const settings = await request('/api/admin/settings', { admin: true });
  if (requestedLanguage !== language || !session.authenticated) return;
  const preserveFields = !!adminSettings;
  adminSettings = settings;
  renderServerSettings(settings, preserveFields);
  renderHotspotSettings(settings.ap, preserveFields && !hotspotChange);
  feedback('server-settings-feedback');
}

async function loadSession() {
  try {
    showSession(await request('/api/admin/session'));
    await loadAdmin();
  } catch (error) {
    feedback(session.authenticated ? 'theme-feedback' : 'login-feedback', errorText(error), 'error');
  }
}

async function login(event) {
  event.preventDefault();
  const password = adminForm.elements.password;
  if (!password.value) {
    feedback('login-feedback', t('passwordRequired'), 'error');
    password.focus();
    return;
  }
  const button = adminForm.querySelector('button[type="submit"]');
  button.disabled = true;
  feedback('login-feedback');
  try {
    const result = await request('/api/admin/login', { method: 'POST', body: { password: password.value } });
    adminForm.reset();
    showSession(result);
    await loadAdmin();
    if (view === 'admin' && session.authenticated) serverSettingsForm.elements.printer.focus();
  } catch (error) {
    feedback(session.authenticated ? 'theme-feedback' : 'login-feedback', errorText(error), 'error');
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  const button = element('logout');
  button.disabled = true;
  try {
    await request('/api/admin/logout', { method: 'POST', admin: true });
    showSession({ authenticated: false });
    feedback('login-feedback', t('loggedOut'), 'success');
    adminForm.elements.password.focus();
  } catch (error) {
    feedback(session.authenticated ? 'theme-feedback' : 'login-feedback', errorText(error), 'error');
  } finally {
    button.disabled = false;
  }
}

async function saveTheme(event) {
  event.preventDefault();
  if (themeBusy || !themeSelect.value) return;
  themeBusy = true;
  updateThemeButtons();
  feedback('theme-feedback');
  try {
    await request('/api/admin/theme', { method: 'PUT', body: { id: themeSelect.value }, admin: true });
    await loadTheme();
    feedback('theme-feedback', t('themeApplied'), 'success');
  } catch (error) {
    adminError('theme-feedback', error);
  } finally {
    themeBusy = false;
    updateThemeButtons();
  }
}

function colorProperty(key) {
  return `--${key.replaceAll('_', '-')}`;
}

const initialColors = {};
function createColorFields() {
  const target = element('theme-colors');
  const pageStyle = getComputedStyle(document.documentElement);
  for (const key of themeKeys) {
    const label = node('label', 'theme-color');
    const control = node('input');
    const caption = node('span');
    control.type = 'color';
    control.name = `color_${key}`;
    initialColors[key] = pageStyle.getPropertyValue(colorProperty(key)).trim();
    caption.dataset.i18n = themeColorLabels[key];
    label.append(control, caption);
    target.append(label);
  }
}

function setThemeFields(config) {
  const controls = customThemeForm.elements;
  controls.theme_name.value = typeof config.name === 'string' ? config.name : '';
  controls.theme_name_en.value = typeof config.name_en === 'string' ? config.name_en : '';
  controls.brand_name.value = typeof config.brand?.name === 'string' ? config.brand.name : 'RasPrint';
  for (const key of themeKeys) {
    controls[`color_${key}`].value = hex.test(config.colors?.[key]) ? config.colors[key] : initialColors[key];
  }
  renderThemePreview();
}

function resetThemeEditor() {
  themeEditId = null;
  themeDraft = {};
  themePreviewLogo = '/static/logo.png';
  customThemeForm.reset();
  customThemeForm.elements.namedItem('id').readOnly = false;
  element('theme-remove-logo-field').hidden = true;
  element('reset-theme-editor').hidden = true;
  feedback('custom-theme-feedback');
  setThemeFields(themeDraft);
  updateThemeButtons();
}

function renderThemePreview() {
  const controls = customThemeForm.elements;
  const preview = element('theme-preview');
  text('theme-preview-name', controls.brand_name.value.trim() || 'RasPrint');
  const logo = controls.logo.files[0];
  if (themeObjectUrl) {
    URL.revokeObjectURL(themeObjectUrl);
    themeObjectUrl = null;
  }
  if (logo && ['image/png', 'image/jpeg', 'image/webp'].includes(logo.type)) themeObjectUrl = URL.createObjectURL(logo);
  element('theme-preview-logo').src = themeObjectUrl || (controls.remove_logo.checked ? '/static/logo.png' : themePreviewLogo);
  for (const key of themeKeys) preview.style.setProperty(colorProperty(key), controls[`color_${key}`].value);
}

async function importThemeJson() {
  const controls = customThemeForm.elements;
  const id = controls.namedItem('id');
  const configFile = controls.config.files[0];
  if (!configFile) return;
  try {
    if (configFile.size > 32768) throw new Error();
    const config = JSON.parse(await configFile.text());
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error();
    themeDraft = config;
    if (!id.value) {
      id.value = configFile.name.replace(/\.json$/i, '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 48);
    }
    setThemeFields(config);
    feedback('custom-theme-feedback');
  } catch {
    controls.config.value = '';
    feedback('custom-theme-feedback', t('invalidThemeJson'), 'error');
  }
}

async function editTheme() {
  if (themeBusy) return;
  const id = themeSelect.value;
  const selected = installedThemes.find((theme) => theme.id === id);
  if (!selected) return;
  themeBusy = true;
  updateThemeButtons();
  try {
    const config = await request(`/api/admin/themes/${encodeURIComponent(id)}`, { admin: true });
    resetThemeEditor();
    const editable = id !== 'raspberry' && selected.editable !== false;
    themeEditId = editable ? id : null;
    themeDraft = editable ? config : { ...config, name_en: '' };
    themePreviewLogo = config.brand?.logo || '/static/logo.png';
    const controls = customThemeForm.elements;
    const idControl = controls.namedItem('id');
    idControl.value = editable ? id : '';
    idControl.readOnly = editable;
    element('theme-remove-logo-field').hidden = !editable || themePreviewLogo === '/static/logo.png';
    element('reset-theme-editor').hidden = false;
    setThemeFields(themeDraft);
    const focusTarget = editable ? controls.theme_name : idControl;
    window.requestAnimationFrame(() => focusTarget.focus());
  } catch (error) {
    adminError('theme-feedback', error);
  } finally {
    themeBusy = false;
    updateThemeButtons();
  }
}

async function addTheme(event) {
  event.preventDefault();
  if (themeBusy) return;
  const controls = customThemeForm.elements;
  const idControl = controls.namedItem('id');
  const id = idControl.value;
  if (!/^[a-z0-9][a-z0-9_-]{0,47}$/.test(id)) {
    feedback('custom-theme-feedback', t('themeIdHelp'), 'error');
    idControl.focus();
    return;
  }
  const name = controls.theme_name.value.trim();
  const brandName = controls.brand_name.value.trim();
  if (!name || !brandName) {
    feedback('custom-theme-feedback', t('themeLabelRequired'), 'error');
    (name ? controls.brand_name : controls.theme_name).focus();
    return;
  }
  themeBusy = true;
  updateThemeButtons();
  feedback('custom-theme-feedback');
  try {
    const editing = !!themeEditId;
    const { id: ignored, ...original } = themeDraft;
    const config = {
      ...original,
      name,
      name_en: controls.theme_name_en.value.trim(),
      brand: { ...themeDraft.brand, name: brandName },
      colors: Object.fromEntries(themeKeys.map((key) => [key, controls[`color_${key}`].value])),
    };
    if (typeof config.brand.logo === 'string' && config.brand.logo.startsWith('/')) delete config.brand.logo;
    if (controls.remove_logo.checked) config.brand.logo = null;
    const body = new FormData();
    body.set('id', id);
    body.set('config', JSON.stringify(config));
    const logo = controls.logo.files[0];
    if (logo) body.set('logo', logo);
    const path = editing ? `/api/admin/themes/${encodeURIComponent(themeEditId)}` : '/api/admin/themes';
    await request(path, { method: editing ? 'PUT' : 'POST', body, admin: true });
    await Promise.all([loadAdminThemes(), loadTheme()]);
    resetThemeEditor();
    themeSelect.value = id;
    feedback('custom-theme-feedback', t(editing ? 'themeSaved' : 'themeAdded'), 'success');
  } catch (error) {
    adminError('custom-theme-feedback', error);
  } finally {
    themeBusy = false;
    updateThemeButtons();
  }
}

async function removeTheme() {
  if (element('remove-theme').disabled || !window.confirm(t('confirmRemoveTheme'))) return;
  themeBusy = true;
  updateThemeButtons();
  feedback('theme-feedback');
  try {
    const id = themeSelect.value;
    await request(`/api/admin/themes/${encodeURIComponent(id)}`, { method: 'DELETE', admin: true });
    await Promise.all([loadAdminThemes(), loadTheme()]);
    if (id === themeEditId) resetThemeEditor();
    feedback('theme-feedback', t('themeRemoved'), 'success');
  } catch (error) {
    adminError('theme-feedback', error);
  } finally {
    themeBusy = false;
    updateThemeButtons();
  }
}

async function saveServerSettings(event) {
  event.preventDefault();
  const controls = serverSettingsForm.elements;
  const limit = controls.max_upload_mb.valueAsNumber;
  const fieldset = serverSettingsForm.querySelector('fieldset');
  if (fieldset.disabled) return;
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    feedback('server-settings-feedback', t('invalidServerSettings'), 'error');
    controls.max_upload_mb.focus();
    return;
  }
  const body = { max_upload_mb: limit, printer: controls.printer.value || null };
  fieldset.disabled = true;
  feedback('server-settings-feedback');
  try {
    adminSettings = await request('/api/admin/settings', { method: 'PUT', body, admin: true });
    renderServerSettings(adminSettings, false);
    maxUpload = adminSettings.max_upload_mb;
    renderInfo();
    feedback('server-settings-feedback', t('settingsSaved'), 'success');
    await refreshDashboard();
  } catch (error) {
    adminError('server-settings-feedback', error);
  } finally {
    fieldset.disabled = !session.authenticated;
  }
}

function renderHotspotAddress(id, value) {
  const address = element(id);
  address.removeAttribute('href');
  address.parentElement.hidden = true;
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:') return false;
    address.textContent = url.href;
    address.parentElement.hidden = false;
    if (dashboard?.demo === false) address.href = url.href;
    return true;
  } catch {
    return false;
  }
}

function renderHotspotAddresses() {
  const ap = adminSettings?.ap;
  const current = hotspotChange ?? (ap?.available ? ap : null);
  const direct = renderHotspotAddress('hotspot-address', current?.url);
  const local = renderHotspotAddress('hotspot-local-address', current?.local_url);
  element('hotspot-addresses').hidden = !direct && !local;
}

function renderHotspotFeedback() {
  if (dashboard?.demo) feedback('hotspot-feedback', t('demoAddresses'), 'success');
  else if (hotspotChange.reconnect) feedback('hotspot-feedback', t('hotspotSaved', { ssid: hotspotChange.ssid }), 'success');
  else feedback('hotspot-feedback', t('hotspotOpenAddress'), 'success');
}

function followHotspotChange(attempt = 0) {
  window.clearTimeout(hotspotTimer);
  if (!session.authenticated || !hotspotChange || attempt >= 5) return;
  hotspotTimer = window.setTimeout(async () => {
    try {
      await loadAdminSettings();
      if (adminSettings?.ap?.pending) followHotspotChange(attempt + 1);
    } catch {
      // The server can become temporarily unreachable during a restart or while the user reconnects.
      followHotspotChange(attempt + 1);
    }
  }, 3000);
}

async function saveHotspot(event) {
  event.preventDefault();
  const { ssid, hostname, port, secured, password } = hotspotForm.elements;
  const fieldset = hotspotForm.querySelector('fieldset');
  if (fieldset.disabled) return;
  if (!hotspotHostname.test(hostname.value)) {
    feedback('hotspot-feedback', t('localHostnameHelp'), 'error');
    hostname.focus();
    return;
  }
  const body = { ssid: ssid.value, port: port.valueAsNumber, secured: secured.checked, hostname: hostname.value.toLowerCase() };
  const ap = adminSettings?.ap;
  const passwordRequired = !ap?.secured || ap.ssid !== body.ssid;
  if (body.port === 80) {
    feedback('hotspot-feedback', t('directPortHelp'), 'error');
    port.focus();
    return;
  }
  if (!body.ssid.trim() || utf8.encode(body.ssid).length > 32 || !Number.isInteger(body.port) || body.port < 1 || body.port > 65535 ||
      (body.secured && (password.value || passwordRequired) && !/^[ -~]{8,63}$/.test(password.value))) {
    feedback('hotspot-feedback', t('invalidHotspot'), 'error');
    return;
  }
  if (body.secured && password.value) body.password = password.value;
  const reconnect = ap?.ssid !== body.ssid || ap?.secured !== body.secured || Boolean(body.password);
  if ((reconnect || ap?.port !== body.port) && !window.confirm(t('confirmHotspot'))) return;
  fieldset.disabled = true;
  feedback('hotspot-feedback');
  try {
    const change = await request('/api/admin/ap', { method: 'PUT', body, admin: true });
    hotspotChange = { ...change, reconnect };
    hostname.value = change.hostname ?? body.hostname;
    password.value = '';
    renderHotspotAddresses();
    renderHotspotFeedback();
    followHotspotChange();
  } catch (error) {
    adminError('hotspot-feedback', error);
  } finally {
    fieldset.disabled = !session.authenticated || !!hotspotChange;
  }
}

async function changePassword(event) {
  event.preventDefault();
  const controls = passwordChangeForm.elements;
  const current = controls.current_password.value;
  const next = controls.new_password.value;
  const byteCount = utf8.encode(next).length;
  if (!current) {
    feedback('password-change-feedback', t('passwordRequired'), 'error');
    controls.current_password.focus();
    return;
  }
  if (byteCount < 8 || byteCount > 1024) {
    feedback('password-change-feedback', t('passwordLength'), 'error');
    controls.new_password.focus();
    return;
  }
  if (next !== controls.confirm_password.value) {
    feedback('password-change-feedback', t('passwordMismatch'), 'error');
    controls.confirm_password.focus();
    return;
  }
  const button = passwordChangeForm.querySelector('button[type="submit"]');
  button.disabled = true;
  feedback('password-change-feedback');
  try {
    const result = await request('/api/admin/password', {
      method: 'PUT',
      body: { current_password: current, new_password: next },
      admin: true,
    });
    showSession(result);
    feedback('login-feedback', t('passwordChanged'), 'success');
    adminForm.elements.password.focus();
  } catch (error) {
    adminError('password-change-feedback', error);
  } finally {
    button.disabled = false;
  }
}

async function cancelJob() {
  if (cancelDialog.returnValue !== 'cancel-job' || cancelTarget === null) return;
  const id = cancelTarget;
  cancelTarget = null;
  feedback('manage-feedback', t('cancelingJob', { id }));
  try {
    await request(`/api/admin/jobs/${id}/cancel`, { method: 'POST', admin: true });
    feedback('manage-feedback', t('canceledJob', { id }), 'success');
    await refreshDashboard();
  } catch (error) {
    feedback(session.authenticated ? 'manage-feedback' : 'login-feedback', errorText(error), 'error');
  }
}

const dropzone = element('dropzone');
dropzone.addEventListener('dragover', (event) => {
  event.preventDefault();
  if (printBusy) return;
  event.dataTransfer.dropEffect = 'copy';
  dropzone.classList.add('is-dragging');
});
dropzone.addEventListener('dragleave', (event) => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove('is-dragging');
});
dropzone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropzone.classList.remove('is-dragging');
  if (!printBusy) chooseFile(event.dataTransfer.files[0]);
});

window.addEventListener('hashchange', () => showView());
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshDashboard();
  else window.clearTimeout(pollTimer);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMenu(true);
});
for (const link of document.querySelectorAll('.sidebar a')) {
  link.addEventListener('click', () => {
    if (location.hash === link.hash && element('sidebar').classList.contains('open')) {
      closeMenu();
      element('page-title').focus();
    }
  });
}
mobile.addEventListener('change', () => closeMenu());
element('open-menu').addEventListener('click', openMenu);
element('close-menu').addEventListener('click', () => closeMenu(true));
element('overlay').addEventListener('click', () => closeMenu(true));
element('refresh').addEventListener('click', () => {
  refreshDashboard();
  loadTheme().catch(() => {});
  if (view === 'admin') loadAdmin();
});
element('job-filters').addEventListener('click', (event) => {
  if (!event.target.dataset.filter) return;
  activeFilter = event.target.dataset.filter;
  for (const filter of element('job-filters').children) filter.setAttribute('aria-pressed', String(filter === event.target));
  renderJobs();
});
fileInput.addEventListener('change', () => chooseFile(fileInput.files[0]));
element('clear-file').addEventListener('click', () => {
  setFile(null);
  fileInput.focus();
});
printerSelect.addEventListener('change', renderSelectedPrinter);
printForm.addEventListener('submit', printDocument);
adminForm.addEventListener('submit', login);
themeForm.addEventListener('submit', saveTheme);
themeSelect.addEventListener('change', updateThemeButtons);
customThemeForm.elements.config.addEventListener('change', importThemeJson);
customThemeForm.addEventListener('input', (event) => {
  if (event.target.name !== 'config') renderThemePreview();
});
customThemeForm.addEventListener('submit', addTheme);
element('edit-theme').addEventListener('click', editTheme);
element('remove-theme').addEventListener('click', removeTheme);
element('reset-theme-editor').addEventListener('click', resetThemeEditor);
serverSettingsForm.addEventListener('submit', saveServerSettings);
hotspotForm.elements.secured.addEventListener('change', toggleHotspotPassword);
hotspotForm.addEventListener('submit', saveHotspot);
passwordChangeForm.addEventListener('submit', changePassword);
element('logout').addEventListener('click', logout);
cancelDialog.addEventListener('close', cancelJob);
languageSelect.addEventListener('change', changeLanguage);
text('network-address', location.host);
createColorFields();
resetThemeEditor();
renderLanguage();
showView(false);
loadInfo().catch(() => {});
loadTheme().catch(() => {});
refreshDashboard();
