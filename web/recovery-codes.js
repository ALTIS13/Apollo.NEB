"use strict";

(() => {
  const format = /^[0-9a-f]{8}(?:-[0-9a-f]{8}){3}$/i;

  window.downloadRecoveryCodes = (list, owner = false) => {
    const codes = [...list.children].map(row => row.textContent.trim());
    if (!codes.length || codes.length > 20 || codes.some(code => !format.test(code))) return;

    const now = new Date();
    const time = new Intl.DateTimeFormat("ru-RU", {dateStyle: "long", timeStyle: "short"}).format(now);
    const panel = owner ? "Управление DevFee" : "Панель майнинга";
    const codeRows = codes.map(code => "- [ ] `" + code + "`");
    const markdown = [
      "# Apollo.NEB · Резервные коды 2FA", "",
      "**Панель** · " + panel, "",
      "**Сохранено** · " + time, "",
      "> Каждый код подходит для одного входа. Храните файл в личном хранилище и отмечайте использованные коды.", "",
      "## Коды", "", ...codeRows, "",
    ].join("\n");
    const url = URL.createObjectURL(new Blob([markdown], {type: "text/markdown;charset=utf-8"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "apollo-neb" + (owner ? "-devfee" : "") + "-2fa-" + now.toISOString().slice(0, 10) + ".md";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  };
})();
