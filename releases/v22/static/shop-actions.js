(() => {
  document.addEventListener('submit', event => {
    const form = event.target.closest('[data-delete-shop-form]');
    if (!form) return;
    const name = form.dataset.shopName || '这家店铺';
    if (!window.confirm(`确定永久删除店铺 ${name}？\n将级联删除数据库中的店铺、指标、评价、财务数据和关联刷新记录，无法撤销；重新添加同一链接不会恢复历史。\n采集正在执行时暂不允许删除；其他店铺及其数据不受影响。`)) {
      event.preventDefault();
      return;
    }
    const confirmation = form.querySelector('input[name="confirm_permanent"]');
    if (!confirmation) {
      event.preventDefault();
      window.alert('此页面仍是旧的删除表单，请刷新页面后重新确认永久删除。');
      return;
    }
    confirmation.value = '1';
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '删除中…'; }
  });
})();
