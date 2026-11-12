function jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    var request = JSON.parse((e.postData && e.postData.contents) || '{}');
    var expectedSecret = PropertiesService.getScriptProperties().getProperty('WEBHOOK_SECRET');
    if (!expectedSecret || request.secret !== expectedSecret) {
      return jsonResponse({ ok: false, error: 'Unauthorized' });
    }

    var recipients = Array.isArray(request.to) ? request.to : [];
    recipients = recipients
      .map(function (email) { return String(email || '').trim(); })
      .filter(function (email) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email); });
    if (!recipients.length || recipients.length > 50) {
      return jsonResponse({ ok: false, error: 'Invalid recipients' });
    }

    var subject = String(request.subject || '').trim();
    var body = String(request.body || '');
    if (!subject || !body) {
      return jsonResponse({ ok: false, error: 'Subject and body are required' });
    }

    MailApp.sendEmail({
      to: recipients.join(','),
      subject: subject.substring(0, 250),
      body: body,
      name: 'Ollies Production Dashboard'
    });

    return jsonResponse({
      ok: true,
      remainingDailyQuota: MailApp.getRemainingDailyQuota()
    });
  } catch (error) {
    return jsonResponse({ ok: false, error: 'Request failed' });
  }
}
