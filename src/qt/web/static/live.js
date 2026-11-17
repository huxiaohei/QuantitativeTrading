// 持仓表在交易时段内定时更新：带 data-live-url 的那一块每隔 data-live-seconds 秒向本站重新取一次，
// 页面在后台时不取。服务器在交易时段之外返回的那一块不带 data-live-url，更新随之停止。
(function () {
  "use strict";
  var MARK = "data-live-holdings";
  var timer = null;
  var busy = false;
  var stopped = false;

  function block() {
    return document.querySelector("[data-live-url]");
  }

  function refresh() {
    var el = block();
    if (el === null || busy || stopped || document.hidden) {
      return;
    }
    busy = true;
    fetch(el.getAttribute("data-live-url"), { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        // 登录过期时会被转到登录页：不替换，也不再继续取（刷新页面后会要求重新登录）
        if (response.redirected) {
          stopped = true;
          return null;
        }
        return response.ok ? response.text() : null;
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
    if (el === null || stopped) {
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
