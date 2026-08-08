/** AURTOR 代墊寫入擴充；與既有客戶群組 API 共用同一個 Web App。 */
const EXPENSE_SHEET_ID_ = '1cwpPt50hlC3OH2tOKQGA7DVv4-qwuMgrLir6AoyU7CM';
const EXPENSE_SHEET_NAME_ = '表單回應 1';
const COMPANY_TAX_ID_ = '90531465';

function doGet(e) {
  if (!authorized_(e)) return json_({ ok: false, error: 'unauthorized' });
  if (String((e && e.parameter && e.parameter.action) || '') !== 'expense_stats') return doGetGroup_(e);
  return expenseStats_(String(e.parameter.payer || '').trim());
}

function doPost(e) {
  if (!authorized_(e)) return json_({ ok: false, error: 'unauthorized' });
  const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  if (body.action !== 'expense') return doPostGroup_(e);
  return saveExpense_(body.expense || {});
}

function saveExpense_(expense) {
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  if (!sheet) return json_({ ok: false, error: 'expense_sheet_not_found' });

  const transactionId = String(expense.transactionId || '').trim();
  const invoiceNumber = String(expense.invoiceNumber || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase();
  const duplicateRow = findDuplicateExpense_(sheet, expense, transactionId, invoiceNumber);
  if (duplicateRow) {
    return json_({ ok: true, duplicate: true, row: duplicateRow, recordUrl: expenseRecordUrl_(sheet, duplicateRow) });
  }

  // 必須先完成重複檢查，才保存圖片，避免重複附件。
  const receiptUrl = saveExpenseReceipt_(expense);
  const now = new Date();
  sheet.appendRow([
    now, expense.month || '', expense.date || '', expense.category || '', expense.item || '',
    expense.amount || '', expense.payer || '', expense.payment || '', expense.reimbursed || '',
    receiptUrl, expense.invoice || '', expense.note || '', expense.project || '', expense.month || '',
    expense.registrantName || '', transactionId, invoiceNumber, expense.companyTaxIdValid === true,
  ]);
  const row = sheet.getLastRow();
  return json_({ ok: true, duplicate: false, row: row, receiptUrl: receiptUrl, recordUrl: expenseRecordUrl_(sheet, row) });
}

function expenseStats_(payer) {
  if (!payer) return json_({ ok: false, error: 'payer_required' });
  const sheet = SpreadsheetApp.openById(EXPENSE_SHEET_ID_).getSheetByName(EXPENSE_SHEET_NAME_);
  if (!sheet) return json_({ ok: false, error: 'expense_sheet_not_found' });
  const now = new Date();
  const timezone = Session.getScriptTimeZone() || 'Asia/Taipei';
  const period = Utilities.formatDate(now, timezone, 'yyyy-MM');
  const rows = sheet.getLastRow() < 2 ? [] : sheet.getRange(2, 1, sheet.getLastRow() - 1, Math.max(18, sheet.getLastColumn())).getValues();
  let count = 0, total = 0, pendingCount = 0, pendingTotal = 0, paidCount = 0, paidTotal = 0;
  rows.forEach(function(row) {
    if (String(row[6] || '').trim() !== payer || normalizeExpenseDate_(row[2]).slice(0, 7) !== period) return;
    const amount = Number(String(row[5] || 0).replace(/,/g, '')) || 0;
    count += 1; total += amount;
    if (String(row[8] || '').trim() === '是') { paidCount += 1; paidTotal += amount; }
    else { pendingCount += 1; pendingTotal += amount; }
  });
  return json_({ ok: true, period: period, count: count, total: total, pendingCount: pendingCount, pendingTotal: pendingTotal, paidCount: paidCount, paidTotal: paidTotal });
}

function findDuplicateExpense_(sheet, expense, transactionId, invoiceNumber) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return 0;
  const values = sheet.getRange(2, 1, lastRow - 1, Math.max(18, sheet.getLastColumn())).getValues();
  const targetDate = normalizeExpenseDate_(expense.date);
  const targetAmount = Number(expense.amount || 0);
  const targetPayer = String(expense.payer || '').trim();
  for (let index = values.length - 1; index >= 0; index--) {
    const row = values[index];
    if (transactionId && String(row[15] || '') === transactionId) return index + 2;
    if (!invoiceNumber || String(row[16] || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase() !== invoiceNumber) continue;
    if (normalizeExpenseDate_(row[2]) === targetDate && Number(String(row[5]).replace(/,/g, '')) === targetAmount && String(row[6]).trim() === targetPayer) {
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
