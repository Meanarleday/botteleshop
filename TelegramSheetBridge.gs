/**
 * TelegramSheetBridge.gs — lets the Python POS bot use your Google Sheet
 * through THIS Apps Script project's /exec URL. No Google Cloud console,
 * no service account, no sharing needed.
 *
 * INSTALL (one time):
 * 1. In your Apps Script project, add this file (or paste into Code.gs).
 * 2. Apps Script > Project Settings > Script Properties > add:
 *      TG_BRIDGE_SECRET = <pick a long random password, e.g. 32 letters>
 * 3. In Code.gs doPost(e), add the tgBridge branch (see Code_gs_patch.txt).
 * 4. Deploy > Manage deployments > Edit > New version > Deploy.
 * 5. Copy the Web App URL (ends with /exec) — the Python bot needs it.
 *
 * Every request must carry the secret, otherwise it is rejected.
 */

var TG_BRIDGE_FALLBACK_SHEET_ID = '1f6eaCgRakOymWsSrfCM0Xgg3MV0K8ShnXI5CHyosnXY';

function tgBridgeSecret_() {
  return String(PropertiesService.getScriptProperties().getProperty('TG_BRIDGE_SECRET') || '');
}

function tgBridgeSpreadsheet_() {
  try {
    var active = SpreadsheetApp.getActiveSpreadsheet();
    if (active) return active;
  } catch (e) {}
  return SpreadsheetApp.openById(TG_BRIDGE_FALLBACK_SHEET_ID);
}

/**
 * Called from doPost(e). payload = { secret, op, sheet, ...op args }.
 * Returns a plain object; doPost wraps it as { success: true, data }.
 */
function tgBridge_(payload) {
  var secret = tgBridgeSecret_();
  if (!secret || !payload || String(payload.secret || '') !== secret) {
    throw new Error('Forbidden');
  }
  var op = String(payload.op || '');
  var ss = tgBridgeSpreadsheet_();

  if (op === 'ensureSheet') {
    var name = String(payload.sheet || '');
    if (!name) throw new Error('sheet is required');
    var sh = ss.getSheetByName(name);
    if (!sh) sh = ss.insertSheet(name);
    var headers = payload.headers || [];
    if (sh.getLastRow() === 0 && headers.length) {
      sh.getRange(1, 1, 1, headers.length).setValues([headers]);
    }
    return { ok: true };
  }

  var sheet = payload.sheet ? ss.getSheetByName(String(payload.sheet)) : null;
  if (!sheet) throw new Error('Sheet not found: ' + payload.sheet);

  if (op === 'getValues') {
    // whole used range, e.g. Products
    return { values: sheet.getDataRange().getValues() };
  }
  if (op === 'getRange') {
    // payload.range like "A2:B" or "B5"
    return { values: sheet.getRange(String(payload.range)).getValues() };
  }
  if (op === 'colValues') {
    var col = Number(payload.col) || 1;
    var last = sheet.getLastRow();
    if (last < 1) return { values: [] };
    return { values: sheet.getRange(1, col, last, 1).getValues() };
  }
  if (op === 'rowValues') {
    var row = Number(payload.row) || 1;
    return { values: sheet.getRange(row, 1, 1, sheet.getLastColumn()).getValues() };
  }
  if (op === 'appendRow') {
    sheet.appendRow(payload.row || []);
    return { ok: true, row: sheet.getLastRow() };
  }
  if (op === 'updateRange') {
    sheet.getRange(String(payload.range)).setValues(payload.values || []);
    return { ok: true };
  }
  if (op === 'updateCell') {
    sheet.getRange(Number(payload.row), Number(payload.col)).setValue(payload.value);
    return { ok: true };
  }
  if (op === 'batchUpdate') {
    (payload.updates || []).forEach(function (u) {
      sheet.getRange(String(u.range)).setValues(u.values);
    });
    return { ok: true };
  }
  throw new Error('Unknown op: ' + op);
}
