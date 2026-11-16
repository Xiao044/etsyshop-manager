document.querySelectorAll('.chart-area').forEach((area) => {
  const tip = area.querySelector('.chart-tooltip');
  const svg = area.querySelector('svg');
  const dot = area.querySelector('.chart-active-dot');
  const show = (point) => {
    tip.textContent = `${point.dataset.day} · ${point.dataset.value} · ${point.dataset.kind}`;
    tip.hidden = false;
    const areaBox = area.getBoundingClientRect();
    const svgBox = svg.getBoundingClientRect();
    const x = Number(point.dataset.x);
    const y = Number(point.dataset.y);
    const center = svgBox.left - areaBox.left + x * svgBox.width / 740;
    tip.style.left = `${Math.max(92, Math.min(areaBox.width - 92, center))}px`;
    tip.style.top = `${Math.max(0, svgBox.top - areaBox.top + y * svgBox.height / 190 - 42)}px`;
    dot.setAttribute('cx', x);
    dot.setAttribute('cy', y);
    dot.classList.add('visible');
  };
  const hide = () => { tip.hidden = true; dot.classList.remove('visible'); };
  area.querySelectorAll('.chart-zone').forEach((point) => {
    point.addEventListener('mouseenter', () => show(point));
    point.addEventListener('focus', () => show(point));
    point.addEventListener('blur', hide);
  });
  area.addEventListener('mouseleave', hide);
});

const operatorSelect = document.querySelector('#operator-filter');
if (operatorSelect) {
  operatorSelect.addEventListener('change', () => operatorSelect.form.requestSubmit());
}

document.querySelectorAll('.review-disclosure').forEach((details) => {
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
      body.textContent = '评价读取失败，请关闭后重试。';
    }
  });
});
