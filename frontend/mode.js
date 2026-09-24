/* 设备模式（桌面 / 手机）的唯一判定源。
   必须在 <head> 里同步加载、且排在 style.css 之前：样式表要在首帧就知道该套
   哪一套规则，否则会出现"先按桌面画一帧、再跳成手机"的闪动。

   判定：视口宽 ≤720px（沿用原断点），或设备本身是触摸设备。后一条是为了
   iPad 横屏（1024px 宽度够，但没有鼠标，桌面那套 hover 唤出/拖拽宽度都不适用）。

   产出两样东西：
     1. html[data-mode="mobile" | "desktop"] —— CSS 的唯一开关，mobile 规则全部
        以 [data-mode="mobile"] 开头，桌面端不匹配任何新选择器（零影响）。
     2. window.CodingAgentMode —— JS 侧判定，并在模式变化时于 document 上派发
        "modechange" 事件（旋转屏幕 / 缩放窗口即时切换，不做 resize 轮询）。
*/
(function () {
  var MOBILE_MAX_W = 720;
  var mqWidth = window.matchMedia("(max-width: " + MOBILE_MAX_W + "px)");
  var mqTouch = window.matchMedia("(hover: none) and (pointer: coarse)");

  function isMobile() { return mqWidth.matches || mqTouch.matches; }

  function apply() {
    var mode = isMobile() ? "mobile" : "desktop";
    var root = document.documentElement;
    if (root.dataset.mode === mode) return mode;
    root.dataset.mode = mode;
    // 模式切换只改这一个属性 + 派发事件，不重建任何 DOM：切横竖屏不丢输入草稿
    document.dispatchEvent(new CustomEvent("modechange", { detail: { mode: mode } }));
    return mode;
  }

  apply();

  var onChange = function () { apply(); };
  if (mqWidth.addEventListener) {
    mqWidth.addEventListener("change", onChange);
    mqTouch.addEventListener("change", onChange);
  } else {                       // 老 Safari 只有 addListener
    mqWidth.addListener(onChange);
    mqTouch.addListener(onChange);
  }

  window.CodingAgentMode = {
    isMobile: isMobile,
    apply: apply,
    MOBILE_MAX_W: MOBILE_MAX_W,
  };
})();
