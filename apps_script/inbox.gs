// money-bot inbox: the iPhone Shortcut posts each Apple Pay payment here, and this adds it to the "inbox" tab.
// The bot reads that tab every minute. Setup: README.md, "Apple Pay feed".
const HEADER = ["received", "card", "merchant", "amount", "time"];

function doPost(e) {
  let data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    console.log("bad request: body is not JSON");
    return ContentService.createTextOutput("bad request");
  }
  const secret = (PropertiesService.getScriptProperties().getProperty("SECRET") || "").trim();
  if (!secret || String(data.secret || "").trim() !== secret) {
    console.log(secret ? "forbidden: secret does not match" : "forbidden: SECRET script property is not set");
    return ContentService.createTextOutput("forbidden");
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const book = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = book.getSheetByName("inbox") || book.insertSheet("inbox");
    sheet.getRange("A:E").setNumberFormat("@"); // plain text, so Sheets doesn't turn times into dates
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(HEADER);
    }
    sheet.appendRow([
      new Date().toISOString(),
      String(data.card || ""),
      String(data.merchant || ""),
      String(data.amount || ""),
      String(data.time || ""),
    ]);
  } finally {
    lock.releaseLock();
  }
  return ContentService.createTextOutput("ok");
}
