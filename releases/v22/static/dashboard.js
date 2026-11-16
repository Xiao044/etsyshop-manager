window.initDashboardCharts = () => {
document.querySelectorAll('.chart-area').forEach((area) => {
  if (area.dataset.chartInitialized) return;
  area.dataset.chartInitialized = '1';
  const readout = area.querySelector('.chart-readout');
  const dot = area.querySelector('.chart-active-dot');
  const show = (point) => {
    readout.querySelector('.readout-date').textContent = point.dataset.day;
    readout.querySelector('.readout-value').textContent = point.dataset.label ? `${point.dataset.label}：${point.dataset.value}` : point.dataset.value;
    readout.querySelector('.readout-kind').textContent = point.dataset.kind;
    dot.setAttribute('cx', point.dataset.x);
    dot.setAttribute('cy', point.dataset.y);
    dot.classList.add('visible');
  };
  const hide = () => { dot.classList.remove('visible'); };
  area.querySelectorAll('.chart-zone').forEach((point) => {
    point.addEventListener('mouseenter', () => show(point));
    point.addEventListener('focus', () => show(point));
    point.addEventListener('blur', hide);
  });
  area.addEventListener('mouseleave', hide);
});

document.querySelectorAll('#operator-filter, #shop-data-select').forEach((select) => {
  if (select.dataset.controlInitialized) return;
  select.dataset.controlInitialized = '1';
  select.addEventListener('change', () => select.form.requestSubmit());
});

document.querySelectorAll('.review-disclosure').forEach((details) => {
  if (details.dataset.disclosureInitialized) return;
  details.dataset.disclosureInitialized = '1';
  details.addEventListener('toggle', async () => {
    if (!details.open || details.dataset.loaded) return;
    const body = details.querySelector('.review-list-body');
    body.textContent = '正在读取评价…';
    try {
      const response = await fetch(details.dataset.url);
      if (!response.ok) throw new Error('读取失败');
      body.innerHTML = await response.text();
      details.dataset.loaded = '1';
    } catch {
      body.textContent = '读取失败，请重新展开。';
    }
  });
});

};
window.initDashboardCharts();
