import os, sqlite3, asyncio, subprocess, sys, signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "txg_maker.db"
TEMPLATES = ROOT / "templates"
DATA = ROOT / "bot_data"
TEMPLATES.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

BOT_TOKEN = os.environ.get("MAKER_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
FERNET_KEY = os.environ.get("FERNET_KEY", "").strip()
if not BOT_TOKEN or not ADMIN_ID or not FERNET_KEY:
    raise SystemExit("Set MAKER_BOT_TOKEN, ADMIN_ID and FERNET_KEY environment variables.")
cipher = Fernet(FERNET_KEY.encode())

ADD_THEME_NAME, ADD_THEME_FILE, USER_TOKEN = range(3)
active_processes = {}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS themes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL, is_payment INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1)""")
        con.execute("""CREATE TABLE IF NOT EXISTS bots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
            username TEXT NOT NULL, token_enc TEXT NOT NULL, theme_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped', started_at TEXT,
            created_at TEXT NOT NULL, FOREIGN KEY(theme_id) REFERENCES themes(id))""")

def is_admin(uid):
    return uid == ADMIN_ID

def utcnow():
    return datetime.now(timezone.utc)

def enc_token(token):
    return cipher.encrypt(token.encode()).decode()

def dec_token(token_enc):
    return cipher.decrypt(token_enc.encode()).decode()

def get_themes(enabled=True):
    with db() as con:
        q = "SELECT * FROM themes"
        if enabled: q += " WHERE enabled=1"
        q += " ORDER BY name"
        return con.execute(q).fetchall()

def get_theme(tid):
    with db() as con:
        return con.execute("SELECT * FROM themes WHERE id=?", (tid,)).fetchone()

def get_bot(bot_id, owner_id=None):
    with db() as con:
        if owner_id is None:
            return con.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        return con.execute("SELECT * FROM bots WHERE id=? AND owner_id=?", (bot_id, owner_id)).fetchone()

def bot_list(owner_id):
    with db() as con:
        return con.execute("""SELECT b.*, t.name AS theme_name, t.is_payment
            FROM bots b JOIN themes t ON t.id=b.theme_id
            WHERE b.owner_id=? ORDER BY b.id DESC""", (owner_id,)).fetchall()

async def start_worker(bot_id):
    row = get_bot(bot_id)
    if not row:
        return False, "Bot record not found."
    theme = get_theme(row["theme_id"])
    if not theme or not theme["enabled"]:
        return False, "This theme is unavailable."
    source = TEMPLATES / theme["filename"]
    if not source.is_file():
        return False, "Theme code file is missing."
    if bot_id in active_processes and active_processes[bot_id].poll() is None:
        return True, "Already running."
    try:
        token = dec_token(row["token_enc"])
        env = os.environ.copy()
        env["BOT_TOKEN"] = token
        env["TXG_BOT_ID"] = str(bot_id)
        env["TXG_OWNER_ID"] = str(row["owner_id"])
        proc = subprocess.Popen(
            [sys.executable, str(source)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(DATA / f"{bot_id}.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        active_processes[bot_id] = proc
        with db() as con:
            con.execute("UPDATE bots SET status='running', started_at=? WHERE id=?",
                        (utcnow().isoformat(), bot_id))
        return True, "Started."
    except (InvalidToken, OSError) as e:
        return False, f"Could not start: {e}"

def stop_worker(bot_id, status="stopped"):
    proc = active_processes.get(bot_id)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.terminate()
    active_processes.pop(bot_id, None)
    with db() as con:
        con.execute("UPDATE bots SET status=?, started_at=NULL WHERE id=?", (status, bot_id))

async def auto_stop_loop():
    while True:
        try:
            with db() as con:
                rows = con.execute("""SELECT b.id,b.started_at,t.is_payment
                    FROM bots b JOIN themes t ON t.id=b.theme_id
                    WHERE b.status='running' AND b.started_at IS NOT NULL""").fetchall()
            now = utcnow()
            for row in rows:
                if row["is_payment"]:
                    continue
                try:
                    started = datetime.fromisoformat(row["started_at"])
                    if now - started >= timedelta(days=7):
                        stop_worker(row["id"], "auto_stopped")
                except Exception:
                    continue
            # Detect exited worker processes
            for bid, proc in list(active_processes.items()):
                if proc.poll() is not None:
                    active_processes.pop(bid, None)
                    with db() as con:
                        con.execute("UPDATE bots SET status='stopped', started_at=NULL WHERE id=?", (bid,))
        except Exception:
            pass
        await asyncio.sleep(60)

async def post_init(app):
    init_db()
    # Restore bots that have not reached their 7-day limit. Payment themes are exempt.
    with db() as con:
        rows = con.execute("""SELECT b.id,b.started_at,t.is_payment
            FROM bots b JOIN themes t ON t.id=b.theme_id
            WHERE b.status='running'""").fetchall()
    now = utcnow()
    for row in rows:
        try:
            if row["started_at"] and not row["is_payment"]:
                if now - datetime.fromisoformat(row["started_at"]) >= timedelta(days=7):
                    stop_worker(row["id"], "auto_stopped")
                    continue
            await start_worker(row["id"])
        except Exception:
            pass
    app.create_task(auto_stop_loop())

def home_keyboard(uid):
    rows = [[InlineKeyboardButton("➕ Add Bot", callback_data="addbot"),
             InlineKeyboardButton("🤖 My Bots", callback_data="mybots")]]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "Welcome to TXG Maker\\nChoose an option:",
        reply_markup=home_keyboard(uid)
    )

async def home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "addbot":
        keyboard = [[InlineKeyboardButton("🆕 Add New Bot", callback_data="newbot")],
                    [InlineKeyboardButton("📂 Old Bot", callback_data="oldbot")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="home")]]
        await q.edit_message_text("Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data == "home":
        await q.edit_message_text("TXG Maker main menu:", reply_markup=home_keyboard(uid))
    elif q.data == "mybots":
        rows = bot_list(uid)
        if not rows:
            await q.edit_message_text("You have no bots yet.", reply_markup=home_keyboard(uid))
            return
        keyboard = [[InlineKeyboardButton(f"@{b['username']} · {b['status']}", callback_data=f"bot:{b['id']}")] for b in rows]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
        await q.edit_message_text("Your bots:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data in ("newbot", "oldbot"):
        if q.data == "oldbot":
            rows = bot_list(uid)
            if not rows:
                await q.edit_message_text("No old bots found. Choose Add New Bot.", reply_markup=home_keyboard(uid))
                return
            keyboard = [[InlineKeyboardButton(f"@{b['username']}", callback_data=f"oldselect:{b['id']}")] for b in rows]
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="addbot")])
            await q.edit_message_text("Select your old bot:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            themes = get_themes()
            if not themes:
                await q.edit_message_text("No themes are available yet. Please contact the admin.")
                return
            keyboard = [[InlineKeyboardButton(t["name"], callback_data=f"theme:{t['id']}")] for t in themes]
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="addbot")])
            context.user_data["flow"] = "new"
            await q.edit_message_text("Select a theme:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data.startswith("oldselect:"):
        bid = int(q.data.split(":")[1])
        b = get_bot(bid, uid)
        if not b:
            await q.answer("Bot not found.", show_alert=True); return
        themes = get_themes()
        keyboard = [[InlineKeyboardButton(t["name"], callback_data=f"oldtheme:{bid}:{t['id']}")] for t in themes]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="oldbot")])
        await q.edit_message_text("Select a theme for this bot:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data.startswith("theme:"):
        tid = int(q.data.split(":")[1])
        if not get_theme(tid):
            await q.answer("Theme not found.", show_alert=True); return
        context.user_data["selected_theme"] = tid
        context.user_data["selected_bot"] = None
        context.user_data["flow"] = "new"
        await q.edit_message_text("Send the Telegram bot token from @BotFather.")
        return USER_TOKEN
    elif q.data.startswith("oldtheme:"):
        _, bid, tid = q.data.split(":")
        b = get_bot(int(bid), uid)
        if not b or not get_theme(int(tid)):
            await q.answer("Invalid selection.", show_alert=True); return
        context.user_data["selected_theme"] = int(tid)
        context.user_data["selected_bot"] = int(bid)
        context.user_data["flow"] = "old"
        await q.edit_message_text("Send the Telegram bot token for this old bot.")
        return USER_TOKEN
    elif q.data.startswith("bot:"):
        bid = int(q.data.split(":")[1])
        b = get_bot(bid, uid)
        if not b:
            await q.answer("Bot not found.", show_alert=True); return
        keyboard = [
            [InlineKeyboardButton("▶️ Start", callback_data=f"start:{bid}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{bid}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="mybots")]
        ]
        await q.edit_message_text(
            f"Bot: @{b['username']}\\nTheme ID: {b['theme_id']}\\nStatus: {b['status']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif q.data.startswith("start:") or q.data.startswith("stop:"):
        action, bid_s = q.data.split(":")
        bid = int(bid_s)
        b = get_bot(bid, uid)
        if not b:
            await q.answer("Not your bot.", show_alert=True); return
        if action == "start":
            ok, msg = await start_worker(bid)
            await q.answer(msg, show_alert=not ok)
        else:
            stop_worker(bid)
            await q.answer("Bot stopped.")
        await home_cb_refresh(q, uid, bid)
    elif q.data == "admin":
        if not is_admin(uid):
            await q.answer("Admin only.", show_alert=True); return
        keyboard = [[InlineKeyboardButton("➕ Add Theme", callback_data="adm_add")],
                    [InlineKeyboardButton("📚 Manage Themes", callback_data="adm_list")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="home")]]
        await q.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data == "adm_add":
        if not is_admin(uid): return
        context.user_data["admin_step"] = "theme_name"
        await q.edit_message_text("Send the new theme name. Add [PAYMENT] in the name for the 7-day exemption.")
    elif q.data == "adm_list":
        if not is_admin(uid): return
        themes = get_themes(enabled=False)
        if not themes:
            await q.edit_message_text("No themes uploaded yet."); return
        keyboard = [[InlineKeyboardButton(f"{t['name']} · {'Payment exempt' if t['is_payment'] else '7-day limit'}",
                    callback_data=f"adm_theme:{t['id']}")] for t in themes]
        await q.edit_message_text("Themes:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data.startswith("adm_theme:"):
        if not is_admin(uid): return
        tid = int(q.data.split(":")[1]); t = get_theme(tid)
        if not t: return
        keyboard = [[InlineKeyboardButton("📤 Replace Python file", callback_data=f"adm_replace:{tid}")],
                    [InlineKeyboardButton("Enable / Disable", callback_data=f"adm_toggle:{tid}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="adm_list")]]
        await q.edit_message_text(f"Theme: {t['name']}\\nFile: {t['filename']}", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data.startswith("adm_replace:"):
        tid = int(q.data.split(":")[1])
        context.user_data["replace_theme"] = tid
        context.user_data["admin_step"] = "replace_file"
        await q.edit_message_text("Upload the replacement .py file.")
    elif q.data.startswith("adm_toggle:"):
        tid = int(q.data.split(":")[1])
        t = get_theme(tid)
        if t:
            with db() as con:
                con.execute("UPDATE themes SET enabled=? WHERE id=?", (0 if t["enabled"] else 1, tid))
        await q.answer("Theme updated.")
        await q.edit_message_text("Admin Panel:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 Manage Themes", callback_data="adm_list")]]))
    return ConversationHandler.END

async def home_cb_refresh(q, uid, bid):
    b = get_bot(bid, uid)
    if not b: return
    keyboard = [[InlineKeyboardButton("▶️ Start", callback_data=f"start:{bid}"),
                 InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{bid}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="mybots")]]
    await q.edit_message_text(f"Bot: @{b['username']}\\nStatus: {b['status']}", reply_markup=InlineKeyboardMarkup(keyboard))

async def token_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = (update.message.text or "").strip()
    if ":" not in token or len(token) < 20:
        await update.message.reply_text("That does not look like a Telegram bot token. Send a valid token.")
        return USER_TOKEN
    try:
        import urllib.request, json
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        if not data.get("ok"):
            raise ValueError("Telegram rejected this token.")
        username = data["result"]["username"]
    except Exception:
        await update.message.reply_text("Token validation failed. Check it with @BotFather and try again.")
        return USER_TOKEN
    tid = context.user_data.get("selected_theme")
    theme = get_theme(tid)
    if not theme:
        await update.message.reply_text("Theme is no longer available. Start again with /start.")
        return ConversationHandler.END
    old_id = context.user_data.get("selected_bot")
    if old_id:
        old = get_bot(old_id, update.effective_user.id)
        if not old:
            await update.message.reply_text("Old bot not found.")
            return ConversationHandler.END
        stop_worker(old_id)
        with db() as con:
            con.execute("UPDATE bots SET username=?, token_enc=?, theme_id=?, status='stopped', started_at=NULL WHERE id=?",
                        (username, enc_token(token), tid, old_id))
        bid = old_id
    else:
        with db() as con:
            cur = con.execute("""INSERT INTO bots(owner_id,username,token_enc,theme_id,status,created_at)
                VALUES(?,?,?,?,?,?)""",
                (update.effective_user.id, username, enc_token(token), tid, "stopped", utcnow().isoformat()))
            bid = cur.lastrowid
    # Do not echo token back to chat.
    ok, msg = await start_worker(bid)
    await update.message.reply_text(
        f"Bot @{username} saved.\\nTheme: {theme['name']}\\nLaunch: {msg}\\n\\nUse /start → My Bots to manage it."
    )
    context.user_data.clear()
    return ConversationHandler.END

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    step = context.user_data.get("admin_step")
    if step == "theme_name":
        name = (update.message.text or "").strip()
        if not name or len(name) > 40:
            await update.message.reply_text("Theme name must be 1–40 characters.")
            return
        payment = 1 if "[PAYMENT]" in name.upper() else 0
        clean = name.replace("[PAYMENT]", "").replace("[payment]", "").strip()
        context.user_data["pending_theme"] = (clean, payment)
        context.user_data["admin_step"] = "theme_file"
        await update.message.reply_text("Now upload the Python .py file for this theme. It must read its bot token from the BOT_TOKEN environment variable.")
    return

async def admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    step = context.user_data.get("admin_step")
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".py"):
        await update.message.reply_text("Please upload a .py file.")
        return
    if doc.file_size and doc.file_size > 200_000:
        await update.message.reply_text("File too large. Maximum 200 KB.")
        return
    tgfile = await doc.get_file()
    safe_name = f"theme_{doc.file_unique_id[:18]}.py"
    dest = TEMPLATES / safe_name
    await tgfile.download_to_drive(custom_path=dest)
    if step == "theme_file":
        pending = context.user_data.get("pending_theme")
        if not pending:
            await update.message.reply_text("Start Add Theme again.")
            return
        name, payment = pending
        try:
            with db() as con:
                con.execute("INSERT INTO themes(name,filename,is_payment) VALUES(?,?,?)",
                            (name, safe_name, payment))
            await update.message.reply_text(f"Theme '{name}' added successfully.")
        except sqlite3.IntegrityError:
            dest.unlink(missing_ok=True)
            await update.message.reply_text("That theme name already exists.")
        context.user_data.clear()
    elif step == "replace_file":
        tid = context.user_data.get("replace_theme")
        t = get_theme(tid)
        if t:
            with db() as con:
                con.execute("UPDATE themes SET filename=? WHERE id=?", (safe_name, tid))
            await update.message.reply_text(f"Python file replaced for theme '{t['name']}'.")
        context.user_data.clear()
    else:
        dest.unlink(missing_ok=True)
        await update.message.reply_text("Open Admin Panel → Add Theme or Replace Python File first.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Use /start to open the menu.")
    return ConversationHandler.END

async def error_handler(update, context):
    # Never log bot tokens or message contents.
    print(f"TXG Maker error: {type(context.error).__name__}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(home_cb, pattern=r"^(theme:|oldtheme:)")],
        states={USER_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, token_message)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(home_cb))
    app.add_handler(MessageHandler(filters.Document.ALL, admin_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
