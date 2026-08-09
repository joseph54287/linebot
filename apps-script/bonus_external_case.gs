/** 外案待核准紀錄與「獎金試算／專案」歸檔端點。 */
const BONUS_LOG_SHEET = '外案申請紀錄';
const BONUS_PROJECT_SHEET = '專案';

function handleBonusPost_(payload) {
  try {
    const props = PropertiesService.getScriptProperties();
    const spreadsheet = SpreadsheetApp.openById(props.getProperty('BONUS_SPREADSHEET_ID'));
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      if (payload.action === 'submit') return bonusJson_(bonusSubmit_(spreadsheet, payload.data || {}));
      if (payload.action === 'approve') return bonusJson_(bonusResolve_(spreadsheet, payload, true));
      if (payload.action === 'reject') return bonusJson_(bonusResolve_(spreadsheet, payload, false));
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
  const headers = ['申請編號','狀態','員工','LINE User ID','日期','案名','未稅金額','案型','款項匯入','申請時間','核准人','處理時間','專案列','預計匯款日期','計價方式','稅額','含稅收款'];
  let sheet = spreadsheet.getSheetByName(BONUS_LOG_SHEET);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(BONUS_LOG_SHEET);
    sheet.setFrozenRows(1);
  }
  sheet.getRange(1,1,1,headers.length).setValues([headers]);
  return sheet;
}

function bonusSubmit_(spreadsheet, data) {
  const required = ['requestId','employeeName','employeeUserId','date','projectName','amount','caseType','destination','paymentDate'];
  if (required.some(key => data[key] === '' || data[key] === null || data[key] === undefined)) return {ok:false,error:'Missing fields'};
  const amount = Number(data.amount);
  if (!Number.isFinite(amount) || amount <= 0) return {ok:false,error:'Invalid amount'};
  if (!['公司','員工個人','尚未確認'].includes(data.destination)) return {ok:false,error:'Invalid destination'};
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('A:A').createTextFinder(String(data.requestId)).matchEntireCell(true).findNext();
  if (found) return {ok:true,duplicate:true,requestId:data.requestId};
  const taxMode = data.taxMode === '含稅' ? '含稅' : '稅外';
  const grossAmount = taxMode === '含稅' ? Number(data.enteredAmount) : Math.round(amount * 1.05);
  const taxAmount = grossAmount - amount;
  sheet.appendRow([data.requestId,'待核准',data.employeeName,data.employeeUserId,new Date(data.date),data.projectName,amount,data.caseType,data.destination,new Date(),'','','',new Date(data.paymentDate),taxMode,taxAmount,grossAmount]);
  return {ok:true,requestId:data.requestId};
}

function bonusResolve_(spreadsheet, payload, approved) {
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('A:A').createTextFinder(String(payload.requestId || '')).matchEntireCell(true).findNext();
  if (!found) return {ok:false,error:'Not found'};
  const row = found.getRow();
  const values = sheet.getRange(row,1,1,17).getValues()[0];
  const status = values[1];
  const result = {ok:true,employeeName:values[2],employeeUserId:values[3],projectName:values[5]};
  if (status === '已核准' || status === '已拒絕') return Object.assign(result,{duplicate:true,status:status});
  if (!approved) {
    sheet.getRange(row,2).setValue('已拒絕');
    sheet.getRange(row,11,1,2).setValues([[payload.approverUserId || '',new Date()]]);
    return Object.assign(result,{status:'已拒絕'});
  }
  if (values[8] === '尚未確認') return {ok:false,error:'Destination unresolved'};
  const projectRow = bonusInsertProject_(spreadsheet, {
    requestId:values[0], employeeName:values[2], date:values[4], projectName:values[5],
    amount:Number(values[6]), caseType:values[7], destination:values[8], approverUserId:payload.approverUserId || '',
    paymentDate:values[13], taxMode:values[14], taxAmount:Number(values[15]), grossAmount:Number(values[16]),
  });
  sheet.getRange(row,2).setValue('已核准');
  sheet.getRange(row,11,1,3).setValues([[payload.approverUserId || '',new Date(),projectRow]]);
  return Object.assign(result,{status:'已核准',projectRow:projectRow});
}

function bonusInsertProject_(spreadsheet, data) {
  const sheet = spreadsheet.getSheetByName(BONUS_PROJECT_SHEET);
  if (!sheet) throw new Error('Project sheet not found');
  const headers = ['案型','外案申請編號','外案狀態','LINE申請人','核准人','申請核准時間','來源','預計匯款日期','計價方式','稅額','含稅收款'];
  if (!sheet.getRange(1,13).getValue()) sheet.getRange(1,13,1,headers.length).setValues([headers]);
  const existing = sheet.getRange('N:N').createTextFinder(String(data.requestId)).matchEntireCell(true).findNext();
  if (existing) return existing.getRow();
  const month = new Date(data.date).getMonth() + 1;
  const lastRow = Math.max(sheet.getLastRow(), 2);
  const monthValues = sheet.getRange(1,1,lastRow,1).getValues().flat();
  let start = 2, end = lastRow + 1;
  for (let index=1; index<monthValues.length; index++) {
    if (Number(monthValues[index]) === month) {
      start = index + 2;
      for (let next=index+1; next<monthValues.length; next++) {
        if (monthValues[next] !== '' && monthValues[next] !== null) { end = next + 1; break; }
      }
      break;
    }
  }
  let target = start;
  while (target < end && sheet.getRange(target,2,1,11).getDisplayValues()[0].some(String)) target++;
  if (target >= end) sheet.insertRowBefore(end);
  sheet.getRange(target,2,1,3).setValues([[new Date(data.date),data.projectName,data.amount]]);
  sheet.getRange(target,9).setValue(data.employeeName);
  sheet.getRange(target,11).setValue(data.destination);
  sheet.getRange(target,13,1,7).setValues([[data.caseType,data.requestId,'已核准',data.employeeName,data.approverUserId,new Date(),'LINE Bot']]);
  sheet.getRange(target,20).setValue(data.paymentDate);
  sheet.getRange(target,21,1,3).setValues([[data.taxMode,data.taxAmount,data.grossAmount]]);
  return target;
}

function bonusJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
