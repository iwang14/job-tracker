/**
 * OPTIONAL: copies a posting to the Applications tab the moment you set its Status to "applied".
 *
 * Without this, the hourly Python run does the same copy (within the hour). Both check the
 * Applications "Posting ID" column first, so they never create duplicates, and the Python sync
 * stays the source of truth: if this script ever breaks, nothing is lost.
 *
 * Install: in the Sheet, Extensions -> Apps Script -> paste this file -> Save. That's it: onEdit
 * is a "simple trigger" and needs no authorization because it only touches this spreadsheet.
 */
var FOLLOW_UP_DAYS = 10; // keep in sync with settings.yaml -> sheet.follow_up_days

function onEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getName() !== 'Postings' || e.range.getRow() < 2) return;
  var pHeader = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var statusCol = pHeader.indexOf('Status') + 1;
  // handles single-cell edits and pasting into a block of Status cells
  if (statusCol < e.range.getColumn() || statusCol > e.range.getLastColumn()) return;

  var apps = e.source.getSheetByName('Applications');
  if (!apps) return;
  var aHeader = apps.getRange(1, 1, 1, apps.getLastColumn()).getValues()[0];
  var idCol = aHeader.indexOf('Posting ID');
  var existing = {};
  if (apps.getLastRow() > 1 && idCol >= 0) {
    apps.getRange(2, idCol + 1, apps.getLastRow() - 1, 1).getValues().forEach(function (r) { existing[r[0]] = true; });
  }
  var tz = e.source.getSpreadsheetTimeZone();
  var today = new Date();
  var next = new Date(today.getTime() + FOLLOW_UP_DAYS * 86400000);
  var fmt = function (d) { return Utilities.formatDate(d, tz, 'yyyy-MM-dd'); };

  for (var row = e.range.getRow(); row <= e.range.getLastRow(); row++) {
    var values = sheet.getRange(row, 1, 1, pHeader.length).getValues()[0];
    var get = function (name) { var i = pHeader.indexOf(name); return i >= 0 ? values[i] : ''; };
    if (String(get('Status')).toLowerCase() !== 'applied') continue;
    var pid = get('ID');
    if (!pid || existing[pid]) continue;
    var rec = {
      'Posting ID': pid, 'Company': get('Company'), 'Role': get('Title'), 'Link': get('Link'),
      'Tier': get('Tier'), 'Country': get('Country'), 'Date applied': fmt(today), 'Channel': 'company site',
      'Stage': 'applied', 'Last update': fmt(today), 'Next action': 'Follow up if no response',
      'Next action date': fmt(next)
    };
    apps.appendRow(aHeader.map(function (h) { return rec.hasOwnProperty(h) ? rec[h] : ''; }));
    existing[pid] = true;
  }
}
