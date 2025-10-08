const fetch = require('node-fetch');
const qrcode = require('qrcode-terminal');
const crypto = require('crypto');
const { Client, LocalAuth } = require('whatsapp-web.js');

const IA_ENDPOINT = process.env.IA_ENDPOINT || 'http://localhost:8000/ingesta';
const QR_ENDPOINT = (process.env.QR_ENDPOINT || IA_ENDPOINT.replace('/ingesta', '/qr'));
const GROUP_WHITELIST = (process.env.GROUP_WHITELIST || '')
  .split(',').map(s => s.trim()).filter(Boolean);
const HMAC_SECRET = process.env.HMAC_SECRET || '';

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
  puppeteer: {
    headless: true,
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    args: ['--no-sandbox','--disable-setuid-sandbox']
  }
});

client.on('qr', async (qr) => {
  console.log('Escaneá este QR:');
  qrcode.generate(qr, { small: true });
  try {
    const body = JSON.stringify({ qr });
    const headers = { 'Content-Type': 'application/json' };
    if (HMAC_SECRET) {
      const signature = crypto.createHmac('sha256', HMAC_SECRET).update(body).digest('hex');
      headers['x-signature'] = signature;
    }
    await fetch(QR_ENDPOINT, { method: 'POST', headers, body });
  } catch (e) {
    console.error('No se pudo publicar el QR', e);
  }
});

client.on('ready', async () => {
  console.log('WA listo ✅');
  // Al estar listo, limpia QR en backend
  try {
    const body = JSON.stringify({ qr: null });
    const headers = { 'Content-Type': 'application/json' };
    if (HMAC_SECRET) {
      const signature = crypto.createHmac('sha256', HMAC_SECRET).update(body).digest('hex');
      headers['x-signature'] = signature;
    }
    await fetch(QR_ENDPOINT, { method: 'POST', headers, body });
  } catch {}
});

client.on('message', async (msg) => {
  try {
    const chat = await msg.getChat();
    const isGroup = chat.isGroup;
    const groupName = isGroup ? chat.name : null;

    if (isGroup && GROUP_WHITELIST.length && !GROUP_WHITELIST.includes(groupName)) return;

    // Sender info (for both group and private)
    let senderName = null;
    try {
      const contact = msg.author
        ? await client.getContactById(msg.author)
        : await msg.getContact();
      senderName = (contact && (contact.pushname || contact.name || contact.shortName || contact.verifiedName)) || null;
    } catch {}
    const senderNumber = ((msg.author || msg.from || '').split('@')[0]) || null;
    const groupId = isGroup ? msg.from : null;

    let payload = {
      from_: msg.from,
      author: msg.author || null,
      timestamp: msg.timestamp,
      isGroup,
      groupName,
      groupId,
      senderName,
      senderNumber,
      type: 'text',
      text: msg.body || null
    };

    if (msg.hasMedia) {
      const media = await msg.downloadMedia();
      payload.type = 'media';
      payload.mimetype = media.mimetype;
      payload.filename = media.filename || 'file.bin';
      payload.data_base64 = media.data;
    }

    const body = JSON.stringify(payload);
    const headers = { 'Content-Type': 'application/json' };
    if (HMAC_SECRET) {
      const signature = crypto.createHmac('sha256', HMAC_SECRET).update(body).digest('hex');
      headers['x-signature'] = signature;
    }

    await fetch(IA_ENDPOINT, {
      method: 'POST',
      headers,
      body
    });
  } catch (e) {
    console.error('Error', e);
  }
});

client.initialize();

