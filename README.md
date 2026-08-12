# Notification History Bot

A universal Android notification forwarding system using MacroDroid, Flask, MongoDB and Telegram forum topics.

## Features

- One universal MacroDroid macro
- No Telegram bot token inside MacroDroid
- Per-user API keys
- Telegram setup bot
- Automatic group connection
- Automatic topic creation
- Separate topic for every Android application
- MongoDB storage
- API authentication
- Basic rate limiting
- Telegram webhook
- Render deployment
- Works with WhatsApp, Gmail and any other Android application

## Architecture

MacroDroid
    ↓
Notification History API
    ↓
MongoDB
    ↓
Telegram Bot API
    ↓
Telegram Forum Topic

## Setup

### 1. Create Telegram Bot

Create a bot using @BotFather.

Copy the bot token.

### 2. Create MongoDB Atlas Database

Create a MongoDB database and obtain the MongoDB connection URI.

### 3. Deploy to Render

Connect this repository to Render.

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
gunicorn app:app --workers 1 --threads 4 --timeout 30
```

### 4. Environment Variables

Set:

```text
TELEGRAM_BOT_TOKEN
MONGO_URI
DB_NAME
WEBHOOK_URL
WEBHOOK_SECRET
```

Example:

```text
DB_NAME=notification_history
WEBHOOK_URL=https://your-service.onrender.com
```

### 5. Start the Bot

Open the Telegram bot and send:

```text
/start
```

The bot will provide:

* API key
* Setup code

### 6. Connect a Telegram Group

Create a Telegram group.

Enable Topics.

Add the bot as an administrator.

Give the bot:

```text
Manage Topics
```

Then send inside the group:

```text
/connect YOUR_SETUP_CODE
```

The bot will connect the group to your account.

### 7. MacroDroid

Create one Notification Received trigger.

Do not restrict it to WhatsApp or Gmail.

The macro should receive notifications from all applications.

Send a POST request to:

```text
https://your-service.onrender.com/api/notification
```

Header:

```text
Authorization: Bearer YOUR_API_KEY
```

Content-Type:

```text
application/json
```

Body:

```json
{
  "app": "WhatsApp",
  "title": "Dhyey",
  "text": "Hello!",
  "package": "com.whatsapp"
}
```

Use MacroDroid notification variables for the actual notification values.

### Topic Routing

If the first notification is:

```text
WhatsApp
```

the server creates:

```text
WhatsApp
```

topic.

If the next notification is:

```text
Gmail
```

the server creates:

```text
Gmail
```

topic.

If Instagram sends a notification:

```text
Instagram
```

a new Instagram topic is automatically created.

## API Authentication

The API uses:

```text
Authorization: Bearer YOUR_API_KEY
```

Telegram bot tokens are never sent to Android devices.

## Security

Do not expose:

* Telegram bot token
* MongoDB URI
* API keys
* webhook secret

Do not commit `.env`.

## API

### Health

```text
GET /health
```

### Notification

```text
POST /api/notification
```

Header:

```text
Authorization: Bearer YOUR_API_KEY
```

Body:

```json
{
  "app": "WhatsApp",
  "title": "Example",
  "text": "Hello",
  "package": "com.whatsapp"
}
```

## Telegram Commands

Private chat:

```text
/start
/newkey
/help
```

Group:

```text
/connect CODE
/status
/disconnect
/help
```
