/** AURTOR 代墊寫入擴充；與既有客戶群組 API 共用同一個 Web App。 */
const EXPENSE_SHEET_ID_ = '1cwpPt50hlC3OH2tOKQGA7DVv4-qwuMgrLir6AoyU7CM';
const EXPENSE_SHEET_NAME_ = '表單回應 1';
const COMPANY_TAX_ID_ = '90531465';

function doGet(e) {
  if (!authorized_(e)) return json_({ ok: false, error: 'unauthorized' });
  const action = String((e && e.parameter && e.parameter.action) || '');
  if (action === 'expense_stats') return expenseStats_(String(e.parameter.payer || '').trim(), String(e.parameter.userId || '').trim());
  if (action === 'supplements') return supplementList_(String(e.parameter.payer || '').trim(), String(e.parameter.userId || '').trim());
  return doGetGroup_(e);
}

function doPost(e) {
  if (!authorized_(e)) return json_({ ok: false, error: 'unauthorized' });
  const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  if (body.action === 'expense') return saveExpense_(body.expense || {});
  if (body.action === 'supplement') return updateSupplement_(body.supplement || {});
  return doPostGroup_(e);
}

function saveExpense_(expense) {
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  if (!sheet) return json_({ ok: false, error: 'expense_sheet_not_found' });
  ensureExpenseMetadataHeaders_(sheet);

  const transactionId = String(expense.transactionId || '').trim();
  const invoiceNumber = String(expense.invoiceNumber || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase();
  const duplicateRow = findDuplicateExpense_(sheet, expense, transactionId, invoiceNumber);
  if (duplicateRow) {
    const original = sheet.getRange(duplicateRow, 1, 1, Math.max(24, sheet.getLastColumn())).getValues()[0];
    return json_({ ok: true, duplicate: true, row: duplicateRow, recordUrl: expenseRecordUrl_(sheet, duplicateRow), original: { date: normalizeExpenseDate_(original[2]), project: String(original[12] || ''), amount: Number(original[5] || 0), registrantName: String(original[14] || original[6] || '') } });
  }

  // 必須先完成重複檢查，才保存圖片，避免重複附件。
  const receiptUrl = saveExpenseReceipt_(expense);
  const now = new Date();
  const missingReasons = expenseMissingReasons_(expense);
  const supplementStatus = missingReasons.length ? '待補件' : '資料完整';
  sheet.appendRow([
    now, expense.month || '', expense.date || '', expense.category || '', expense.item || '',
    expense.amount || '', expense.payer || '', expense.payment || '', expense.reimbursed || '',
    receiptUrl, expense.invoice || '', expense.note || '', expense.project || '', expense.month || '',
    expense.registrantName || '', transactionId, invoiceNumber,
    expense.companyTaxIdValid === true ? '正確' : '未填公司統編', expense.registrantUserId || '',
    expense.receiptHash || '', supplementStatus, missingReasons.join('、'), '', supplementStatus === '資料完整' ? now : '',
  ]);
  const row = sheet.getLastRow();
  return json_({ ok: true, duplicate: false, row: row, receiptUrl: receiptUrl, recordUrl: expenseRecordUrl_(sheet, row) });
}

function expenseMissingReasons_(expense) {
  const reasons = [];
  if (expense.companyTaxIdValid !== true) reasons.push('缺少統編');
  if (!expense.receiptBase64) reasons.push('缺少收據');
  if (Number(expense.receiptConfidence || 1) < 0.75) reasons.push('圖片不清楚');
  if (!String(expense.project || '').trim() || String(expense.project || '').trim() === '專案無') reasons.push('專案待確認');
  if (!Number(expense.amount || 0) || Number(expense.receiptConfidence || 1) < 0.65) reasons.push('金額需要確認');
  return reasons;
}

function supplementList_(payer, userId) {
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  if (!sheet) return json_({ ok: false, error: 'expense_sheet_not_found' });
  ensureExpenseMetadataHeaders_(sheet);
  const rows = sheet.getLastRow() < 2 ? [] : sheet.getRange(2, 1, sheet.getLastRow() - 1, Math.max(24, sheet.getLastColumn())).getValues();
  const items = [];
  for (let index = rows.length - 1; index >= 0 && items.length < 10; index--) {
    const row = rows[index];
    const belongs = userId && String(row[18] || '').trim() ? String(row[18] || '').trim() === userId : String(row[6] || '').trim() === payer;
    if (!belongs || String(row[20] || '').trim() !== '待補件') continue;
    items.push({ row: index + 2, date: normalizeExpenseDate_(row[2]), project: String(row[12] || ''), amount: Number(row[5] || 0), reasons: String(row[21] || '').split('、').filter(String), recordUrl: expenseRecordUrl_(sheet, index + 2) });
  }
  return json_({ ok: true, items: items });
}

function updateSupplement_(input) {
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  const rowNumber = Number(input.row || 0);
  if (!sheet || rowNumber < 2 || rowNumber > sheet.getLastRow()) return json_({ ok: false, error: 'record_not_found' });
  ensureExpenseMetadataHeaders_(sheet);
  const row = sheet.getRange(rowNumber, 1, 1, Math.max(24, sheet.getLastColumn())).getValues()[0];
  const belongs = String(row[18] || '').trim() ? String(row[18] || '').trim() === String(input.userId || '').trim() : String(row[6] || '').trim() === String(input.payer || '').trim();
  if (!belongs) return json_({ ok: false, error: 'forbidden' });
  let reasons = String(row[21] || '').split('、').filter(String);
  if (input.acceptNoTax === true) reasons = reasons.filter(function(reason) { return reason !== '缺少統編'; });
  if (input.project) { sheet.getRange(rowNumber, 13).setValue(input.project); reasons = reasons.filter(function(reason) { return reason !== '專案待確認'; }); }
  if (Number(input.amount || 0) > 0) { sheet.getRange(rowNumber, 6).setValue(Number(input.amount)); reasons = reasons.filter(function(reason) { return reason !== '金額需要確認'; }); }
  if (input.receiptBase64) {
    const receiptUrl = saveExpenseReceipt_(input);
    sheet.getRange(rowNumber, 10).setValue(receiptUrl);
    sheet.getRange(rowNumber, 20).setValue(input.receiptHash || '');
    reasons = reasons.filter(function(reason) { return reason !== '缺少收據' && reason !== '圖片不清楚'; });
    if (input.companyTaxIdValid === true) { sheet.getRange(rowNumber, 18).setValue('正確'); reasons = reasons.filter(function(reason) { return reason !== '缺少統編'; }); }
  }
  const now = new Date();
  sheet.getRange(rowNumber, 21).setValue(reasons.length ? '待補件' : '資料完整');
  sheet.getRange(rowNumber, 22).setValue(reasons.join('、'));
  sheet.getRange(rowNumber, 23).setValue(now);
  if (!reasons.length) sheet.getRange(rowNumber, 24).setValue(now);
  return json_({ ok: true, complete: !reasons.length, reasons: reasons, recordUrl: expenseRecordUrl_(sheet, rowNumber) });
}

function expenseStats_(payer, userId) {
  if (!payer) return json_({ ok: false, error: 'payer_required' });
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  if (!sheet) return json_({ ok: false, error: 'expense_sheet_not_found' });
  const now = new Date();
  const timezone = Session.getScriptTimeZone() || 'Asia/Taipei';
  const period = Utilities.formatDate(now, timezone, 'yyyy-MM');
  // 使用顯示值解析舊版表單時間，避免試算表地區格式讓 Date 轉換失敗。
  const rows = sheet.getLastRow() < 2 ? [] : sheet.getRange(2, 1, sheet.getLastRow() - 1, Math.max(24, sheet.getLastColumn())).getDisplayValues();
  let count = 0, total = 0, pendingCount = 0, pendingTotal = 0, paidCount = 0, paidTotal = 0;
  rows.forEach(function(row) {
    // 新資料優先比對 LINE User ID；舊資料則兼容「支出人」與「登記人」欄位。
    const lineUserId = String(row[18] || '').trim();
    const legacyRegistrant = String(row[14] || '').trim();
    const belongsToUser = lineUserId
      ? lineUserId === userId
      : String(row[6] || '').trim() === payer || legacyRegistrant.indexOf(userId) >= 0 || legacyRegistrant.indexOf(payer) >= 0;
    if (!belongsToUser || normalizeExpenseDate_(row[0]).slice(0, 7) !== period) return;
    const amount = Number(String(row[5] || 0).replace(/,/g, '')) || 0;
    count += 1; total += amount;
    if (String(row[8] || '').trim() === '是') { paidCount += 1; paidTotal += amount; }
    else { pendingCount += 1; pendingTotal += amount; }
  });
  return json_({ ok: true, period: period, count: count, total: total, pendingCount: pendingCount, pendingTotal: pendingTotal, paidCount: paidCount, paidTotal: paidTotal });
}

function ensureExpenseMetadataHeaders_(sheet) {
  const headers = ['交易識別碼', '發票號碼', '統編狀態', 'LINE User ID', '圖片指紋', '補件狀態', '缺漏原因', '最後補件時間', '補件完成時間'];
  const range = sheet.getRange(1, 16, 1, headers.length);
  const current = range.getValues()[0];
  range.setValues([headers.map(function(header, index) { return current[index] || header; })]);
  backfillSupplementStatus_(sheet);
}

function backfillSupplementStatus_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  const rows = sheet.getRange(2, 1, lastRow - 1, Math.max(24, sheet.getLastColumn())).getValues();
  const output = rows.map(function(row) {
    if (String(row[20] || '').trim()) return [row[20], row[21]];
    const reasons = [];
    if (String(row[17] || '').trim() !== '正確') reasons.push('缺少統編');
    if (!String(row[9] || '').trim()) reasons.push('缺少收據');
    if (!String(row[12] || '').trim() || String(row[12] || '').trim() === '專案無') reasons.push('專案待確認');
    if (!Number(row[5] || 0)) reasons.push('金額需要確認');
    return [reasons.length ? '待補件' : '資料完整', reasons.join('、')];
  });
  sheet.getRange(2, 21, output.length, 2).setValues(output);
}

function findDuplicateExpense_(sheet, expense, transactionId, invoiceNumber) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return 0;
  const values = sheet.getRange(2, 1, lastRow - 1, Math.max(24, sheet.getLastColumn())).getValues();
  const targetDate = normalizeExpenseDate_(expense.date);
  const targetAmount = Number(expense.amount || 0);
  const targetHash = String(expense.receiptHash || '').trim();
  for (let index = values.length - 1; index >= 0; index--) {
    const row = values[index];
    if (transactionId && String(row[15] || '') === transactionId) return index + 2;
    if (targetHash && String(row[19] || '').trim() === targetHash) return index + 2;
    if (!invoiceNumber || String(row[16] || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase() !== invoiceNumber) continue;
    if (normalizeExpenseDate_(row[2]) === targetDate && Number(String(row[5]).replace(/,/g, '')) === targetAmount) {
      return index + 2;
    }
  }
  return 0;
}

function saveExpenseReceipt_(expense) {
  if (!expense.receiptBase64) return '';
  const properties = PropertiesService.getScriptProperties();
  const configuredFolderId = properties.getProperty('EXPENSE_RECEIPT_FOLDER_ID');
  const folder = configuredFolderId ? DriveApp.getFolderById(configuredFolderId) : getOrCreateExpenseFolder_();
  const bytes = Utilities.base64Decode(expense.receiptBase64);
  const blob = Utilities.newBlob(bytes, expense.receiptMimeType || 'image/jpeg', expense.receiptFileName || 'receipt.jpg');
  return folder.createFile(blob).getUrl();
}

function getOrCreateExpenseFolder_() {
  const properties = PropertiesService.getScriptProperties();
  const savedId = properties.getProperty('EXPENSE_RECEIPT_FOLDER_ID');
  if (savedId) return DriveApp.getFolderById(savedId);
  const folders = DriveApp.getFoldersByName('AURTOR 代墊單據');
  const folder = folders.hasNext() ? folders.next() : DriveApp.createFolder('AURTOR 代墊單據');
  properties.setProperty('EXPENSE_RECEIPT_FOLDER_ID', folder.getId());
  return folder;
}

function expenseRecordUrl_(sheet, row) {
  return SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getUrl() + '#gid=' + sheet.getSheetId() + '&range=A' + row;
}

function normalizeDigits_(value) {
  return String(value || '').replace(/\D/g, '');
}

function normalizeExpenseDate_(value) {
  if (Object.prototype.toString.call(value) === '[object Date]' && !isNaN(value)) {
    return Utilities.formatDate(value, Session.getScriptTimeZone() || 'Asia/Taipei', 'yyyy-MM-dd');
  }
  const match = String(value || '').match(/(20\d{2})\D+(\d{1,2})\D+(\d{1,2})/);
  return match ? match[1] + '-' + ('0' + match[2]).slice(-2) + '-' + ('0' + match[3]).slice(-2) : '';
}
