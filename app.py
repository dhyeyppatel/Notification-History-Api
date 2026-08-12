import os
import re
import time
import hmac
import hashlib
import secrets
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "notification_history").strip()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

API_KEY_PREFIX = "nh_"

MAX_NOTIFICATION_LENGTH = 3500

# Basic per-instance rate limit.
# Can later be replaced with Redis/Key Value for large scale.
RATE_LIMIT_COUNT = 120
RATE_LIMIT_WINDOW = 60


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

mongo_client = None
db = None

users_collection = None
groups_collection = None
topics_collection = None


def init_database():
    global mongo_client
    global db
    global users_collection
    global groups_collection
    global topics_collection

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing")

    mongo_client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000
    )

    db = mongo_client[DB_NAME]

    users_collection = db["users"]
    groups_collection = db["groups"]
    topics_collection = db["topics"]

    users_collection.create_index(
        [("telegram_user_id", ASCENDING)],
        unique=True
    )

    users_collection.create_index(
        [("api_key_hash", ASCENDING)],
        unique=True
    )

    users_collection.create_index(
        [("setup_code_hash", ASCENDING)],
        unique=True,
        sparse=True
    )

    groups_collection.create_index(
        [("chat_id", ASCENDING)],
        unique=True
    )

    groups_collection.create_index(
        [("owner_user_id", ASCENDING)]
    )

    topics_collection.create_index(
        [
            ("chat_id", ASCENDING),
            ("app_key", ASCENDING)
        ],
        unique=True
    )

    logger.info("MongoDB initialized")


# ============================================================
# TELEGRAM API
# ============================================================

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, payload=None, timeout=15):

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "ok": False,
            "description": response.text
        }

    if not response.ok or not data.get("ok"):
        logger.error(
            "Telegram API error: method=%s status=%s description=%s",
            method,
            response.status_code,
            data.get("description")
        )

        raise RuntimeError(
            data.get(
                "description",
                "Telegram API request failed"
            )
        )

    return data["result"]


def send_message(
    chat_id,
    text,
    message_thread_id=None,
    reply_markup=None
):

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if message_thread_id is not None:
        payload["message_thread_id"] = int(
            message_thread_id
        )

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return telegram(
        "sendMessage",
        payload
    )


def send_document(
    chat_id,
    document_name,
    document_content,
    caption=None
):

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    payload = {
        "chat_id": chat_id
    }
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"

    files = {
        "document": (document_name, document_content)
    }

    response = requests.post(
        f"{TELEGRAM_API}/sendDocument",
        data=payload,
        files=files,
        timeout=15
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "ok": False,
            "description": response.text
        }

    if not response.ok or not data.get("ok"):
        logger.error(
            "Telegram API error: method=sendDocument status=%s description=%s",
            response.status_code,
            data.get("description")
        )

        raise RuntimeError(
            data.get(
                "description",
                "Telegram API request failed"
            )
        )

    return data["result"]


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def escape_html(value):
    if value is None:
        return ""

    value = str(value)

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def clean_text(value, limit=MAX_NOTIFICATION_LENGTH):

    if value is None:
        return ""

    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + "\n…"


def normalize_app_name(app_name):

    if not app_name:
        return "Unknown"

    app_name = str(app_name).strip()

    app_name = re.sub(
        r"\s+",
        " ",
        app_name
    )

    return clean_text(
        app_name,
        100
    )


def app_key(app_name):

    normalized = normalize_app_name(
        app_name
    ).lower()

    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        normalized
    ).strip("_")

    return normalized or "unknown"


# ============================================================
# API KEY
# ============================================================

def hash_secret(value):

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def generate_api_key():

    return (
        API_KEY_PREFIX +
        secrets.token_urlsafe(32)
    )


def generate_setup_code():

    # Example:
    # A7K9-X2P4

    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "23456789"
    )

    first = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    second = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    return f"{first}-{second}"


# ============================================================
# USER MANAGEMENT
# ============================================================

def get_or_create_user(
    telegram_user_id,
    first_name=""
):

    user = users_collection.find_one({
        "telegram_user_id": telegram_user_id
    })

    if user:
        if "api_key" not in user:
            api_key = generate_api_key()
            users_collection.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "api_key": api_key,
                        "api_key_hash": hash_secret(api_key),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            user["api_key"] = api_key
        return user

    api_key = generate_api_key()

    document = {
        "telegram_user_id": telegram_user_id,
        "first_name": first_name,
        "api_key": api_key,
        "api_key_hash": hash_secret(api_key),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }

    try:

        users_collection.insert_one(
            document
        )

    except DuplicateKeyError:

        return users_collection.find_one({
            "telegram_user_id":
                telegram_user_id
        })

    return document


def create_new_api_key(
    telegram_user_id
):

    api_key = generate_api_key()

    users_collection.update_one(
        {
            "telegram_user_id":
                telegram_user_id
        },
        {
            "$set": {
                "api_key_hash":
                    hash_secret(api_key),

                "updated_at":
                    datetime.now(timezone.utc)
            }
        }
    )

    return api_key


# ============================================================
# SETUP CODE
# ============================================================

def create_setup_code(
    telegram_user_id
):

    code = generate_setup_code()

    users_collection.update_one(
        {
            "telegram_user_id":
                telegram_user_id
        },
        {
            "$set": {
                "setup_code_hash":
                    hash_secret(code),

                "setup_code_created_at":
                    datetime.now(timezone.utc)
            }
        }
    )

    return code


def consume_setup_code(
    code
):

    if not code:
        return None

    code = code.strip().upper()

    user = users_collection.find_one(
        {
            "setup_code_hash":
                hash_secret(code)
        }
    )

    if not user:
        return None

    # Setup codes are single-use.
    users_collection.update_one(
        {
            "_id": user["_id"]
        },
        {
            "$unset": {
                "setup_code_hash": "",
                "setup_code_created_at": ""
            }
        }
    )

    return user


# ============================================================
# API AUTHENTICATION
# ============================================================

def get_api_user():

    authorization = request.headers.get(
        "Authorization",
        ""
    )

    if not authorization.startswith(
        "Bearer "
    ):
        return None

    api_key = authorization[
        len("Bearer "):
    ].strip()

    if not api_key:
        return None

    return users_collection.find_one({
        "api_key_hash":
            hash_secret(api_key)
    })


# ============================================================
# RATE LIMITING
# ============================================================

rate_lock = threading.Lock()

rate_limits = {}


def check_rate_limit(user_id):

    now = time.time()

    with rate_lock:

        entry = rate_limits.get(
            user_id
        )

        if not entry:

            rate_limits[user_id] = [
                now,
                1
            ]

            return True

        window_start, count = entry

        if now - window_start >= RATE_LIMIT_WINDOW:

            rate_limits[user_id] = [
                now,
                1
            ]

            return True

        if count >= RATE_LIMIT_COUNT:

            return False

        rate_limits[user_id][1] += 1

        return True


# ============================================================
# GROUP MANAGEMENT
# ============================================================

def get_group(chat_id):

    return groups_collection.find_one({
        "chat_id": chat_id
    })


def connect_group(
    chat,
    user
):

    chat_id = chat.get("id")

    groups_collection.update_one(
        {
            "chat_id": chat_id
        },
        {
            "$set": {
                "chat_id": chat_id,
                "chat_title":
                    chat.get(
                        "title",
                        "Telegram Group"
                    ),
                "chat_type":
                    chat.get(
                        "type",
                        ""
                    ),
                "owner_user_id":
                    user["telegram_user_id"],
                "updated_at":
                    datetime.now(timezone.utc)
            },
            "$setOnInsert": {
                "created_at":
                    datetime.now(timezone.utc)
            }
        },
        upsert=True
    )


# ============================================================
# TELEGRAM GROUP VALIDATION
# ============================================================

def validate_bot_admin(
    chat_id
):

    bot_info = telegram(
        "getMe"
    )

    bot_id = bot_info["id"]

    member = telegram(
        "getChatMember",
        {
            "chat_id": chat_id,
            "user_id": bot_id
        }
    )

    status = member.get(
        "status"
    )

    if status not in (
        "administrator",
        "creator"
    ):
        return False, (
            "I must be an administrator "
            "in this group."
        )

    if status == "creator":
        return True, None

    can_manage_topics = member.get(
        "can_manage_topics",
        False
    )

    if not can_manage_topics:

        return False, (
            "I need the "
            "Manage Topics permission."
        )

    return True, None


def validate_forum_group(
    chat_id
):

    chat = telegram(
        "getChat",
        {
            "chat_id": chat_id
        }
    )

    if not chat.get(
        "is_forum",
        False
    ):

        return False, (
            "This group is not a forum. "
            "Please enable Topics in "
            "the group settings."
        )

    return True, chat


# ============================================================
# TOPICS
# ============================================================

topic_creation_lock = threading.Lock()


def get_or_create_topic(
    chat_id,
    app_name
):

    key = app_key(app_name)

    existing = topics_collection.find_one({
        "chat_id": chat_id,
        "app_key": key
    })

    if existing:
        return existing["message_thread_id"]

    # Prevent duplicate topic creation
    # when multiple notifications arrive
    # for a new app at the same time.
    with topic_creation_lock:

        existing = topics_collection.find_one({
            "chat_id": chat_id,
            "app_key": key
        })

        if existing:
            return existing[
                "message_thread_id"
            ]

        result = telegram(
            "createForumTopic",
            {
                "chat_id": chat_id,
                "name": normalize_app_name(
                    app_name
                )
            }
        )

        thread_id = result[
            "message_thread_id"
        ]

        document = {
            "chat_id": chat_id,
            "app_key": key,
            "app_name":
                normalize_app_name(app_name),
            "message_thread_id":
                thread_id,
            "created_at":
                datetime.now(timezone.utc)
        }

        try:

            topics_collection.insert_one(
                document
            )

        except DuplicateKeyError:

            existing = topics_collection.find_one({
                "chat_id": chat_id,
                "app_key": key
            })

            if existing:

                return existing[
                    "message_thread_id"
                ]

        return thread_id


# ============================================================
# NOTIFICATION FORMAT
# ============================================================

def build_notification_message(
    data
):
    def get_field(key, limit=MAX_NOTIFICATION_LENGTH):
        return escape_html(clean_text(data.get(key, ""), limit))

    app_name = get_field("app", 100)
    title = get_field("title", 500)
    text = get_field("text")
    big_text = get_field("big_text")
    lines_text = get_field("lines")
    sub_text = get_field("sub_text")
    ticker = get_field("ticker")
    actions = get_field("actions")
    package_name = get_field("package", 200)
    channel = get_field("channel", 200)

    lines = [
        "🚨 <b>New Notification</b>\n",
        f"📱 App: <b>{app_name}</b>",
        f"👤 Sender: {title}\n",
        f"💬 Message:\n{text}\n",
        f"📄 Expanded Content:\n{big_text}\n",
        f"📋 Additional Lines:\n{lines_text}\n",
        f"🔹 Account / Sub Text:\n{sub_text}\n",
        f"⚡ Preview:\n{ticker}\n",
        f"🎯 Available Actions:\n{actions}\n",
        "━━━━━━━━━━━━━━━━━━━━\n",
        f"📦 Package:\n<code>{package_name}</code>\n",
        f"🔔 Channel:\n{channel}   ━━━━━━━━━━━━━━━━━━━━",
        "🤖 Forwarded by Dhyey Notify Bot",
        "Service by @commonthread"
    ]

    return "\n".join(lines)


def get_customized_macro(api_key):
    try:
        macro_path = os.path.join(
            os.path.dirname(__file__),
            "Notification_History.macro"
        )
        with open(macro_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = content.replace("YOUR_API_KEY", api_key)

        if WEBHOOK_URL:
            content = content.replace(
                "https://noti.dhyey.cc",
                WEBHOOK_URL.rstrip('/')
            )

        return content.encode("utf-8")
    except Exception as exc:
        logger.error("Failed to read macro template: %s", exc)
        return None


# ============================================================
# BOT COMMANDS
# ============================================================

def handle_start(
    message
):

    chat = message.get("chat", {})
    user = message.get("from", {})

    if chat.get("type") != "private":
        send_message(
            chat["id"],
            "Please open a private chat with me and use /start."
        )
        return

    telegram_user_id = user["id"]
    db_user = get_or_create_user(telegram_user_id, user.get("first_name", ""))

    setup_code = create_setup_code(telegram_user_id)

    text = (
        "👋 <b>Welcome to Notification History!</b>\n\n"
        "I can forward notifications from your Android phone directly into organized Telegram topics.\n\n"
        "<b>Here is how to set me up:</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Add me to your Telegram forum group (using the button below) and make me an <b>Administrator</b> with <b>Manage Topics</b> permission.\n\n"
        "2️⃣ In that group send:\n"
        f"<code>/connect {escape_html(setup_code)}</code>\n\n"
        "Once successfully connected, I will send you your API Key and your personalized MacroDroid file here in our private chat!"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Add to group", "url": f"https://t.me/notificationhistorybot?startgroup=connect_{setup_code}&admin=change_info+post_messages+delete_messages+manage_topics"}
            ],
            [
                {"text": "Commonthread", "url": "https://t.me/commonthread"}
            ]
        ]
    }

    send_message(
        chat["id"],
        text,
        reply_markup=reply_markup
    )


def handle_key(
    message
):

    chat = message.get(
        "chat",
        {}
    )

    user = message.get(
        "from",
        {}
    )

    if chat.get("type") != "private":
        send_message(
            chat["id"],
            "Use /key in my private chat."
        )
        return

    db_user = get_or_create_user(user["id"], user.get("first_name", ""))
    api_key = db_user.get("api_key")

    send_message(
        chat["id"],
        (
            "🔑 <b>Your API key</b>\n\n"
            f"<code>{escape_html(api_key)}</code>\n\n"
            "Keep this key private."
        )
    )

    macro_bytes = get_customized_macro(api_key)
    if macro_bytes:
        send_document(
            chat["id"],
            "Notification_History.macro",
            macro_bytes,
            "📥 Here is your personalized MacroDroid file."
        )


def handle_connect(
    message
):

    chat = message.get(
        "chat",
        {}
    )

    user = message.get(
        "from",
        {}
    )

    if chat.get("type") not in (
        "group",
        "supergroup"
    ):

        send_message(
            chat["id"],
            "Use /connect inside your Telegram group."
        )

        return

    text = message.get(
        "text",
        ""
    )

    parts = text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        send_message(
            chat["id"],
            (
                "Usage:\n"
                "<code>/connect ABCD-1234</code>"
            )
        )

        return

    code = parts[1].strip()

    setup_user = consume_setup_code(
        code
    )

    if not setup_user:

        send_message(
            chat["id"],
            (
                "❌ Invalid or expired setup code.\n\n"
                "Open my private chat and use /start "
                "to get a new code."
            )
        )

        return

    try:

        is_admin, error = validate_bot_admin(
            chat["id"]
        )

        if not is_admin:

            send_message(
                chat["id"],
                f"❌ {error}"
            )

            return

        is_forum, result = validate_forum_group(
            chat["id"]
        )

        if not is_forum:

            send_message(
                chat["id"],
                f"❌ {result}"
            )

            return

        connect_group(
            chat,
            setup_user
        )

        send_message(
            chat["id"],
            (
                "✅ <b>Group connected!</b>\n\n"
                f"📁 {escape_html(chat.get('title', 'Group'))}\n\n"
                "I have sent your API key and personalized MacroDroid file to your private messages."
            )
        )

        db_user = get_or_create_user(setup_user["telegram_user_id"])
        api_key = db_user.get("api_key")

        send_message(
            setup_user["telegram_user_id"],
            (
                "✅ <b>Group connected successfully!</b>\n\n"
                "🔑 <b>Your API Key</b>\n"
                f"<code>{escape_html(api_key)}</code>\n\n"
                "⚠️ Keep this key private.\n\n"
                "<b>Next Step:</b>\n"
                "Install the universal MacroDroid macro below. It is already configured with your API key."
            )
        )
        
        macro_bytes = get_customized_macro(api_key)
        if macro_bytes:
            send_document(
                setup_user["telegram_user_id"],
                "Notification_History.macro",
                macro_bytes,
                "📥 Your personalized MacroDroid file!"
            )

    except Exception as exc:

        logger.error(
            "Connect error: %s",
            type(exc).__name__
        )

        send_message(
            chat["id"],
            (
                "❌ I couldn't connect this group.\n\n"
                "Make sure I am an administrator and "
                "have Manage Topics permission."
            )
        )


def handle_status(
    message
):

    chat = message.get(
        "chat",
        {}
    )

    group = get_group(
        chat["id"]
    )

    if not group:

        send_message(
            chat["id"],
            (
                "❌ This group is not connected.\n\n"
                "Use /connect CODE."
            )
        )

        return

    topic_count = topics_collection.count_documents({
        "chat_id": chat["id"]
    })

    send_message(
        chat["id"],
        (
            "📊 <b>Notification History Status</b>\n\n"
            f"📁 Group: "
            f"{escape_html(group.get('chat_title', ''))}\n"
            f"🧵 Topics: <b>{topic_count}</b>\n\n"
            "🟢 Connected"
        )
    )


def handle_disconnect(
    message
):

    chat = message.get(
        "chat",
        {}
    )

    user = message.get(
        "from",
        {}
    )

    group = get_group(
        chat["id"]
    )

    if not group:

        send_message(
            chat["id"],
            "This group is not connected."
        )

        return

    if group.get(
        "owner_user_id"
    ) != user.get("id"):

        send_message(
            chat["id"],
            "❌ Only the account that connected "
            "this group can disconnect it."
        )

        return

    groups_collection.delete_one({
        "chat_id": chat["id"]
    })

    topics_collection.delete_many({
        "chat_id": chat["id"]
    })

    send_message(
        chat["id"],
        (
            "🔌 <b>Disconnected.</b>\n\n"
            "This group will no longer receive "
            "notifications from the API."
        )
    )


def handle_help(
    message
):

    chat_id = message[
        "chat"
    ]["id"]

    send_message(
        chat_id,
        (
            "📖 <b>Notification History Help</b>\n\n"
            "/start — Setup account\n"
            "/key — Show your API key and macro file\n"
            "/status — Show group status\n"
            "/disconnect — Disconnect group\n"
            "/help — Show help\n\n"
            "Notifications are automatically "
            "organized into Telegram topics."
        )
    )


def process_telegram_update(
    update
):

    message = update.get(
        "message"
    )

    if not message:
        return

    text = message.get(
        "text",
        ""
    )

    if not text.startswith("/"):
        return

    command = text.split(
        maxsplit=1
    )[0].lower()

    # Remove @BotUsername
    command = command.split("@")[0]

    if command == "/start":

        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("connect_"):
            message["text"] = f"/connect {parts[1].split('_')[1]}"
            handle_connect(message)
        else:
            handle_start(message)

    elif command == "/key":

        handle_key(message)

    elif command == "/connect":

        handle_connect(message)

    elif command == "/status":

        handle_status(message)

    elif command == "/disconnect":

        handle_disconnect(message)

    elif command == "/help":

        handle_help(message)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

def set_telegram_webhook():

    if not WEBHOOK_URL:
        logger.warning(
            "WEBHOOK_URL is not configured."
        )
        return

    webhook_endpoint = (
        WEBHOOK_URL.rstrip("/") +
        "/telegram/webhook"
    )

    try:

        result = telegram(
            "setWebhook",
            {
                "url": webhook_endpoint,
                "secret_token":
                    WEBHOOK_SECRET,
                "allowed_updates": [
                    "message"
                ],
                "drop_pending_updates": True
            }
        )

        logger.info(
            "Telegram webhook configured: %s",
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to configure Telegram webhook: %s",
            type(exc).__name__
        )


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "service":
            "Notification History API",
        "status":
            "online"
    })


@app.get("/health")
def health():

    try:

        mongo_client.admin.command(
            "ping"
        )

        return jsonify({
            "status": "healthy",
            "database": "connected"
        })

    except Exception:

        return jsonify({
            "status": "unhealthy",
            "database": "disconnected"
        }), 503


@app.post("/telegram/webhook")
def telegram_webhook():

    # Verify Telegram webhook secret.
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not WEBHOOK_SECRET or not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):

        return jsonify({
            "ok": False
        }), 403

    update = request.get_json(
        silent=True
    )

    if not update:

        return jsonify({
            "ok": True
        })

    try:

        process_telegram_update(
            update
        )

    except Exception as exc:

        logger.exception(
            "Telegram update processing failed: %s",
            type(exc).__name__
        )

    return jsonify({
        "ok": True
    })


@app.post("/api/notification")
def receive_notification():

    # ------------------------------------------
    # Authenticate
    # ------------------------------------------

    user = get_api_user()

    if not user:
        logger.warning("Notification rejected: Unauthorized")
        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 401

    # ------------------------------------------
    # Rate limit
    # ------------------------------------------

    if not check_rate_limit(
        user["telegram_user_id"]
    ):
        logger.warning("Notification rejected: Rate limit exceeded for user %s", user["telegram_user_id"])
        return jsonify({
            "success": False,
            "error": "Rate limit exceeded"
        }), 429

    # ------------------------------------------
    # Validate JSON
    # ------------------------------------------

    if not request.is_json:
        logger.warning("Notification rejected: JSON required")
        return jsonify({
            "success": False,
            "error": "JSON required"
        }), 400

    data = request.get_json(
        silent=True
    )

    if not isinstance(
        data,
        dict
    ):
        logger.warning("Notification rejected: Invalid JSON")
        return jsonify({
            "success": False,
            "error": "Invalid JSON"
        }), 400

    # ------------------------------------------
    # Validate app
    # ------------------------------------------

    app_name = normalize_app_name(
        data.get("app")
    )

    if not app_name:
        logger.warning("Notification rejected: app is required")
        return jsonify({
            "success": False,
            "error": "app is required"
        }), 400

    logger.info("Processing incoming notification for app: %s", app_name)

    # ------------------------------------------
    # Find connected group
    # ------------------------------------------

    group = groups_collection.find_one({
        "owner_user_id":
            user["telegram_user_id"]
    })

    if not group:
        logger.warning("Notification rejected: No Telegram group connected for user %s", user["telegram_user_id"])
        return jsonify({
            "success": False,
            "error":
                "No Telegram group connected"
        }), 409

    chat_id = group[
        "chat_id"
    ]

    # ------------------------------------------
    # Get/create topic
    # ------------------------------------------

    try:

        topic_id = get_or_create_topic(
            chat_id,
            app_name
        )

    except Exception as exc:

        logger.error(
            "Topic creation failed: %s",
            type(exc).__name__
        )

        return jsonify({
            "success": False,
            "error":
                "Could not create Telegram topic"
        }), 502

    # ------------------------------------------
    # Build message
    # ------------------------------------------

    message = build_notification_message(
        data
    )

    # ------------------------------------------
    # Send notification
    # ------------------------------------------

    try:

        send_message(
            chat_id,
            message,
            topic_id
        )
        logger.info("Successfully forwarded notification to Telegram topic: %s", app_name)

    except Exception as exc:

        logger.error(
            "Notification send failed: %s",
            type(exc).__name__
        )

        return jsonify({
            "success": False,
            "error":
                "Could not send Telegram message"
        }), 502

    return jsonify({
        "success": True,
        "topic": app_name
    })


# ============================================================
# STARTUP
# ============================================================

try:

    init_database()

    set_telegram_webhook()

except Exception as exc:

    logger.error(
        "Startup initialization failed: %s",
        type(exc).__name__
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
