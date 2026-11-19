/* 錢包頁「儲值」分頁的示意幣包資料（wallet-hub 3.1；design D9）。
 *
 * ★ 示意，非正式價格；上線給真實玩家前須換掉或拿掉（屬後續的 wallet-topup，需先探索金流、價格、退款與付款紀錄保存期限）。
 * 這個檔案是**示意幣包數字的唯一位置**：樣板、其他 JS、後端、設定與資料庫都不得出現幣數、贈送數與價格
 * （tests/js/wallet-demo-packages.test.js 掃描整個 app/ 與 migrations/，找不到就通過）。畫面的文字由 wallet-hub-state.js 的 `topupViewOf` 依這份資料組出來。
 *
 * - `IS_DEMO`：一定要是 true 才會顯示卡片；畫面在 true 時一定出現「示意，非正式價格」的標示（頁面頂端與每張卡片）。
 *   把它改成 false 不是「上線」：儲值分頁只會回到「儲值功能尚未開放」，不會顯示任何價格。
 * - `CURRENCY`：價格前的幣別記號。
 * - `PACKAGES`：每張卡片 { id, coins（付費遊戲幣）, bonus（另外贈送的遊戲幣，沒有就是 0）, price（整數）}；id 在陣列內不可重複。
 * 這些資料不寫入資料庫、設定表或 API 回應，也不會送到伺服器；按鈕一律停用，沒有任何增加餘額的路徑。
 * 載入順序：無相依（wallet-hub.js 之前即可）。 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.WalletDemo = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var IS_DEMO = true;
  var CURRENCY = 'NT$';
  var PACKAGES = [
    { id: 'demo-small', coins: 300, bonus: 0, price: 30 },
    { id: 'demo-medium', coins: 1000, bonus: 100, price: 90 },
    { id: 'demo-large', coins: 3000, bonus: 450, price: 250 },
    { id: 'demo-extra', coins: 6500, bonus: 1300, price: 500 }
  ];

  return { IS_DEMO: IS_DEMO, CURRENCY: CURRENCY, PACKAGES: PACKAGES };
});
