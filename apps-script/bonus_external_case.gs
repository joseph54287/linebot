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
      if (payload.action === 'attendance_config_get') return bonusJson_(attendanceConfigGet_());
      if (payload.action === 'attendance_config_set') return bonusJson_(attendanceConfigSet_(payload.config || {}));
      if (payload.action === 'attendance_record') return bonusJson_(attendanceRecord_(spreadsheet, payload.attendance || {}));
    } finally {
      lock.releaseLock();
    }
    return bonusJson_({ok: false, error: 'Unknown action'});
  } catch (error) {
    console.error(error);
    return bonusJson_({ok: false, error: 'Internal error'});
  }
}

const ATTENDANCE_SHEET_NAME_ = '打卡紀錄';

function attendanceConfigGet_() {
  const raw = PropertiesService.getScriptProperties().getProperty('attendance_config');
  if (!raw) return {ok:true,config:{configured:false}};
  const config = JSON.parse(raw);
  config.configured = true;
  return {ok:true,config:config};
}

function attendanceConfigSet_(config) {
  const latitude = Number(config.latitude), longitude = Number(config.longitude);
  const radiusMeters = Math.round(Number(config.radiusMeters || 200));
  if (!isFinite(latitude) || !isFinite(longitude) || radiusMeters < 10 || radiusMeters > 5000) return {ok:false,error:'Invalid attendance config'};
  const stored = {latitude:latitude,longitude:longitude,address:String(config.address || '').slice(0,300),radiusMeters:radiusMeters,updatedBy:String(config.updatedBy || '').slice(0,80),updatedAt:new Date().toISOString()};
  PropertiesService.getScriptProperties().setProperty('attendance_config', JSON.stringify(stored));
  stored.configured = true;
  return {ok:true,config:stored};
}

function attendanceSheet_(spreadsheet) {
  const headers = ['寫入時間','打卡時間','姓名','LINE User ID','類型','結果','距離（公尺）','允許半徑（公尺）','打卡地址','緯度','經度','中心地址','中心緯度','中心經度','交易識別碼','GPS 精準度（公尺）','來源'];
  let sheet = spreadsheet.getSheetByName(ATTENDANCE_SHEET_NAME_);
  if (!sheet) sheet = spreadsheet.insertSheet(ATTENDANCE_SHEET_NAME_);
  if (sheet.getMaxColumns() < headers.length) sheet.insertColumnsAfter(sheet.getMaxColumns(), headers.length - sheet.getMaxColumns());
  const current = sheet.getLastRow() === 0 ? [] : sheet.getRange(1,1,1,headers.length).getValues()[0];
  sheet.getRange(1,1,1,headers.length).setValues([headers.map(function(header,index){return current[index] || header;})]);
  sheet.setFrozenRows(1);
  return sheet;
}

function attendanceRecord_(spreadsheet, attendance) {
  const transactionId = String(attendance.transactionId || '').trim();
  if (!transactionId || !attendance.userId || !attendance.recordedAt) return {ok:false,error:'Missing attendance fields'};
  const sheet = attendanceSheet_(spreadsheet);
  if (sheet.getLastRow() >= 2) {
    const found = sheet.getRange(2,15,sheet.getLastRow()-1,1).createTextFinder(transactionId).matchEntireCell(true).findNext();
    if (found) return {ok:true,duplicate:true,row:found.getRow()};
  }
  sheet.appendRow([new Date(),String(attendance.recordedAt),String(attendance.employeeName || ''),String(attendance.userId),String(attendance.type || ''),attendance.withinRange === true ? '成功' : '範圍外',Number(attendance.distanceMeters || 0),Number(attendance.radiusMeters || 0),String(attendance.address || ''),Number(attendance.latitude),Number(attendance.longitude),String(attendance.centerAddress || ''),Number(attendance.centerLatitude),Number(attendance.centerLongitude),transactionId,Number(attendance.accuracyMeters || 0),String(attendance.source || 'LINE位置訊息')]);
  return {ok:true,duplicate:false,row:sheet.getLastRow()};
}

function bonusLog_(spreadsheet) {
  const headers = ['狀態','申請人','案件日期','案名','案型','款項進入','未稅金額','計價方式','稅額','含稅收款','預計匯款日','聯繫窗口','申請時間','核准人','處理時間','申請編號（系統）','申請人 LINE ID（系統）','核准人 LINE ID（系統）'];
  let sheet = spreadsheet.getSheetByName(BONUS_LOG_SHEET);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(BONUS_LOG_SHEET);
    sheet.setFrozenRows(1);
  }
  sheet.getRange(1,1,1,headers.length).setValues([headers]);
  return sheet;
}

function bonusApproverName_(userId) {
  const names = {
    'Ub983deb79584603885e5b28e9fdf2d5d': '高爾賢',
    'U6c6441cb38102499d1f80d4ea79a53ab': '周暐',
    'U9478b00702c716685d9d8b021d62d538': '阿筌',
  };
  return names[String(userId || '')] || '';
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
  const found = sheet.getRange('P:P').createTextFinder(String(data.requestId)).matchEntireCell(true).findNext();
  if (found) {
    const row = found.getRow();
    sheet.getRange(row,2,1,11).setValues([[data.employeeName,new Date(data.date),data.projectName,data.caseType,data.destination,amount,taxMode,taxAmount,grossAmount,paymentDateValue,data.contact]]);
    sheet.getRange(row,17).setValue(data.employeeUserId);
    return {ok:true,duplicate:true,requestId:data.requestId};
  }
  sheet.appendRow(['待核准',data.employeeName,new Date(data.date),data.projectName,data.caseType,data.destination,amount,taxMode,taxAmount,grossAmount,paymentDateValue,data.contact,new Date(),'','',data.requestId,data.employeeUserId,'']);
  return {ok:true,requestId:data.requestId};
}

function bonusResolve_(spreadsheet, payload, approved) {
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('P:P').createTextFinder(String(payload.requestId || '')).matchEntireCell(true).findNext();
  if (!found) return {ok:false,error:'Not found'};
  const row = found.getRow();
  const values = sheet.getRange(row,1,1,18).getValues()[0];
  const status = values[0];
  const result = {ok:true,employeeName:values[1],employeeUserId:values[16],projectName:values[3]};
  if (status === '已核准') return Object.assign(result,{duplicate:true,status:status});
  if (values[5] === '尚未確認') return {ok:false,error:'Destination unresolved'};
  sheet.getRange(row,1).setValue('已核准');
  sheet.getRange(row,14,1,2).setValues([[bonusApproverName_(payload.approverUserId),new Date()]]);
  sheet.getRange(row,18).setValue(payload.approverUserId || '');
  return Object.assign(result,{status:'已核准'});
}

function bonusDiscuss_(spreadsheet, payload) {
  const sheet = bonusLog_(spreadsheet);
  const found = sheet.getRange('P:P').createTextFinder(String(payload.requestId || '')).matchEntireCell(true).findNext();
  if (!found) return {ok:false,error:'Not found'};
  const row = found.getRow();
  const values = sheet.getRange(row,1,1,18).getValues()[0];
  const result = {ok:true,employeeName:values[1],employeeUserId:values[16],projectName:values[3]};
  if (values[0] === '已核准') return {ok:false,error:'Already approved'};
  if (values[0] === '待討論') return Object.assign(result,{duplicate:true,status:'待討論'});
  sheet.getRange(row,1).setValue('待討論');
  sheet.getRange(row,14,1,2).setValues([[bonusApproverName_(payload.approverUserId),new Date()]]);
  sheet.getRange(row,18).setValue(payload.approverUserId || '');
  return Object.assign(result,{status:'待討論'});
}

function bonusJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
