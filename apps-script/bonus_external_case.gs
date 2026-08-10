/** 外案待核准與核准結果紀錄端點；測試資料只保留在外案申請紀錄。 */
const BONUS_LOG_SHEET = '外案申請紀錄';

function handleBonusPost_(payload) {
  try {
    const props = PropertiesService.getScriptProperties();
    const spreadsheet = SpreadsheetApp.openById(props.getProperty('BONUS_SPREADSHEET_ID'));
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      if (payload.action === 'submit') return bonusJson_(bonusSubmit_(spreadsheet, payload.data || {}));
      if (payload.action === 'approve') return bonusJson_(bonusResolve_(spreadsheet, payload, true));
      if (payload.action === 'discuss' || payload.action === 'reject') return bonusJson_(bonusDiscuss_(spreadsheet, payload));
    } finally {
      lock.releaseLock();
    }
    return bonusJson_({ok: false, error: 'Unknown action'});
  } catch (error) {
    console.error(error);
    return bonusJson_({ok: false, error: 'Internal error'});
  }
}

function bonusLog_(spreadsheet) {
  const headers = ['申請編號','狀態','員工','LINE User ID','日期','案名','未稅金額','案型','款項匯入','申請時間','核准人','處理時間','專案列','預計匯款日期','計價方式','稅額','含稅收款','聯繫窗口'];
  let sheet = spreadsheet.getSheetByName(BONUS_LOG_SHEET);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(BONUS_LOG_SHEET);
    sheet.setFrozenRows(1);
  }
  sheet.getRange(1,1,1,headers.length).setValues([headers]);
  return sheet;
}

function bonusSubmit_(spreadsheet, data) {
  const required = ['requestId','employeeName','employeeUserId','date','projectName','amount','caseType','destination','paymentDate','contact'];
  if (required.some(key => data[key] === '' || data[key] === null || data[key] === undefined)) return {ok:false,error:'Missing fields'};
  const amount = Number(data.amount);
  if (!Number.isFinite(amount) || amount <= 0) return {ok:false,error:'Invalid amount'};
  if (!['公司','員工個人','尚未確認'].includes(data.destination)) return {ok:false,error:'Invalid destination'};
  const sheet = bonusLog_(spreadsheet);
  const taxMode = data.taxMode === '含稅' ? '含稅' : '稅外';
  const grossAmount = taxMode === '含稅' ? Number(data.enteredAmount) : Math.round(amount * 1.05);
  const taxAmount = grossAmount - amount;
  const paymentDateValue = data.paymentDate === '尚未確認' ? '尚未確認' : new Date(data.paymentDate);
  const found = sheet.getRange('A:A').createTextFinder(String(data.requestId)).matchEntireCell(true).findNext();
  if (found) {
    const row = found.getRow();
    sheet.getRange(row,5,1,5).setValues([[new Date(data.date),data.projectName,amount,data.caseType,data.destination]]);
    sheet.getRange(row,14,1,5).setValues([[paymentDateValue,taxMode,taxAmount,grossAmount,data.contact]]);
    return {ok:true,duplicate:true,requestId:data.requestId};
  }
  sheet.appendRow([data.requestId,'待核准',data.employeeName,data.employeeUserId,new Date(data.date),data.projectName,amount,data.caseType,data.destination,new Date(),'','','',paymentDateValue,taxMode,taxAmount,grossAmount,data.contact]);
  return {ok:true,requestId:data.requestId};
}

function bonusResolve_(spreadsheet, payload, approved) {
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('A:A').createTextFinder(String(payload.requestId || '')).matchEntireCell(true).findNext();
  if (!found) return {ok:false,error:'Not found'};
  const row = found.getRow();
  const values = sheet.getRange(row,1,1,18).getValues()[0];
  const status = values[1];
  const result = {ok:true,employeeName:values[2],employeeUserId:values[3],projectName:values[5]};
  if (status === '已核准') return Object.assign(result,{duplicate:true,status:status});
  if (values[8] === '尚未確認') return {ok:false,error:'Destination unresolved'};
  sheet.getRange(row,2).setValue('已核准');
  sheet.getRange(row,11,1,3).setValues([[payload.approverUserId || '',new Date(),'']]);
  return Object.assign(result,{status:'已核准'});
}

function bonusDiscuss_(spreadsheet, payload) {
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('A:A').createTextFinder(String(payload.requestId || '')).matchEntireCell(true).findNext();
  if (!found) return {ok:false,error:'Not found'};
  const row = found.getRow();
  const values = sheet.getRange(row,1,1,18).getValues()[0];
  const result = {ok:true,employeeName:values[2],employeeUserId:values[3],projectName:values[5]};
  if (values[1] === '已核准') return {ok:false,error:'Already approved'};
  if (values[1] === '待討論') return Object.assign(result,{duplicate:true,status:'待討論'});
  sheet.getRange(row,2).setValue('待討論');
  sheet.getRange(row,11,1,2).setValues([[payload.approverUserId || '',new Date()]]);
  return Object.assign(result,{status:'待討論'});
}

function bonusJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
