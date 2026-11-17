// 持仓表在交易时段内定时更新：带 data-live-url 的那一块每隔 data-live-seconds 秒向本站重新取一次，
// 页面在后台时不取。服务器在交易时段之外返回的那一块不带 data-live-url，更新随之停止。
(function () {
  "use strict";
  var MARK = "data-live-holdings";
  var timer = null;
  var busy = false;

  function block() {
    return document.querySelector("[data-live-url]");
  }

  function refresh() {
    var el = block();
    if (el === null || busy || document.hidden) {
      return;
    }
    busy = true;
    fetch(el.getAttribute("data-live-url"), { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        // 登录过期时会被转到登录页：不是这一块的内容一律不替换
        if (!response.ok || response.redirected) {
          return null;
        }
        return response.text();
      })
      .then(function (html) {
        var current = block();
        if (html === null || current === null || html.indexOf(MARK) === -1) {
          return;
        }
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.querySelector("[" + MARK + "]");
        if (fresh !== null) {
          current.replaceWith(document.importNode(fresh, true));
        }
      })
      .catch(function () {
        // 网络出错：保持原来的表格，下一次再试
      })
      .then(function () {
        busy = false;
        schedule();
      });
  }

  function schedule() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    var el = block();
    if (el === null) {
      return;
    }
    var seconds = parseInt(el.getAttribute("data-live-seconds"), 10);
    if (!(seconds >= 5)) {
      seconds = 15;
    }
    timer = setTimeout(refresh, seconds * 1000);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      refresh();
    }
  });
  schedule();
})();
