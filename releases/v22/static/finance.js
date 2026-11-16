document.querySelectorAll('.finance-chart').forEach((el) => {
  const rows = JSON.parse(el.dataset.rows);
  if (!rows.length) { el.textContent = '暂无数据'; return; }
  const money = value => value === null ? '—' : `$${value.toFixed(2)}`;
  const ratio = value => value === null ? '—' : `${value.toFixed(1)}%`;
  const description = r => `${r.day} ｜ Sales ${money(r.sales_usd)} ｜ Net Profit ${money(r.net_profit_usd)} ｜ Marketing/Sales（月累计）${ratio(r.marketing_ratio)}`;
  const last = rows.at(-1);
  const average = key => {
    const valid = rows.map(r => r[key]).filter(value => Number.isFinite(value));
    return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null;
  };
  const averageSales = money(average('sales_usd'));
  const averageProfit = money(average('net_profit_usd'));

  const amounts = rows.flatMap(r => [r.sales_usd, r.net_profit_usd]).filter(v => v !== null);
  const lo = Math.min(0, ...amounts) * 1.12, hi = Math.max(1, ...amounts) * 1.12;
  const ratios = rows.map(r => r.marketing_ratio).filter(v => v !== null);
  const rlo = Math.min(0, ...ratios) * 1.12, rhi = Math.max(30, ...ratios) * 1.12;
  const x = i => 65 + i * 600 / Math.max(rows.length - 1, 1);
  const y = value => 220 - (value - lo) / (hi - lo) * 180;
  const ry = value => 220 - (value - rlo) / (rhi - rlo) * 180;
  const series = [
    {key: 'sales_usd', color: '#dc6a3c', scale: y},
    {key: 'net_profit_usd', color: '#359779', scale: y},
    {key: 'marketing_ratio', color: '#9369bb', scale: ry, dash: '6 4'}
  ];
  const path = s => {
    let penDown = false;
    return rows.map((r, i) => {
      if (r[s.key] === null) { penDown = false; return ''; }
      const command = penDown ? 'L' : 'M'; penDown = true;
      return `${command}${x(i)},${s.scale(r[s.key])}`;
    }).join(' ');
  };
  el.innerHTML = `<svg viewBox="0 0 740 265" width="100%" role="img" aria-label="每日 Sales、Net Profit 和 Marketing/Sales 走势"><g font-size="12" fill="#8994a1">${[0,1,2,3,4].map(i => `<line x1="65" x2="665" y1="${220-i*45}" y2="${220-i*45}" stroke="#edf0f3"/><text x="0" y="${224-i*45}">$${Math.round(lo+(hi-lo)*i/4)}</text><text x="680" y="${224-i*45}">${(rlo+(rhi-rlo)*i/4).toFixed(0)}%</text>`).join('')}<text x="65" y="250">${rows[0].day}</text><text x="575" y="250">${last.day}</text></g>${series.map(s => `<path d="${path(s)}" fill="none" stroke="${s.color}" stroke-width="3" stroke-dasharray="${s.dash || ''}"/>`).join('')}${rows.map((r,i) => {
    const left = i === 0 ? 55 : (x(i-1)+x(i))/2;
    const right = i === rows.length-1 ? 675 : (x(i)+x(i+1))/2;
    return `<rect x="${left}" y="25" width="${right-left}" height="200" fill="transparent" data-i="${i}" tabindex="0" aria-label="${description(r)}"></rect>`;
  }).join('')}${series.map((s,i) => `<circle data-dot="${i}" r="10" fill="${s.color}" stroke="white" stroke-width="2" visibility="hidden" pointer-events="none"/>`).join('')}</svg><div class="finance-tip" aria-live="polite"><div>日期：—</div><div class="finance-daily-row"><span class="finance-daily-value">Sales：—</span><span class="finance-average">日均 ${averageSales}</span></div><div class="finance-daily-row"><span class="finance-daily-value">Net Profit：—</span><span class="finance-average">日均 ${averageProfit}</span></div><div>Marketing/Sales（月累计）：—</div></div>`;
  const tip = el.querySelector('.finance-tip');
  el.querySelectorAll('[data-i]').forEach(rect => {
    const show = () => {
      const index = Number(rect.dataset.i), r = rows[index];
      const values = [`日期：${r.day}`, `Sales：${money(r.sales_usd)}`, `Net Profit：${money(r.net_profit_usd)}`, `Marketing/Sales（月累计）：${ratio(r.marketing_ratio)}`];
      [...tip.children].forEach((line, i) => {
        const value = line.querySelector('.finance-daily-value');
        (value || line).textContent = values[i];
      });
      series.forEach((s,i) => {
        const dot = el.querySelector(`[data-dot="${i}"]`);
        dot.setAttribute('visibility', r[s.key] === null ? 'hidden' : 'visible');
        if (r[s.key] !== null) { dot.setAttribute('cx', x(index)); dot.setAttribute('cy', s.scale(r[s.key])); }
      });
    };
    rect.addEventListener('mouseenter',show); rect.addEventListener('focus',show);
  });
  el.addEventListener('mouseleave', () => el.querySelectorAll('[data-dot]').forEach(dot => dot.setAttribute('visibility','hidden')));
});
