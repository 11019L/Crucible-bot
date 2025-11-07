import os
import sqlite3
import ccxt
import time
import qrcode
from io import BytesIO
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_WALLET = os.getenv("ADMIN_WALLET")

# Binance
exchange = ccxt.binance({
    'apiKey': os.getenv("BINANCE_API_KEY"),
    'secret': os.getenv("BINANCE_API_SECRET"),
    'options': {'defaultType': 'future'}
})

# BSC
w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed1.binance.org'))
USDT = '0x55d398326f99059fF775485246999027B3197955'
usdt_abi = [
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]
usdt = w3.eth.contract(address=USDT, abi=usdt_abi)

# DB
conn = sqlite3.connect('data.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    wallet TEXT,
    personal_wallet TEXT,
    balance REAL DEFAULT 0
)''')
c.execute('''CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    symbol TEXT,
    side TEXT,
    qty REAL,
    entry REAL,
    leverage INTEGER,
    notional REAL,
    fee REAL,
    status TEXT DEFAULT 'open'
)''')
conn.commit()

def get_user(uid):
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if not row:
        acct = w3.eth.account.create()
        c.execute("INSERT INTO users (user_id, wallet) VALUES (?, ?)", (uid, acct.address))
        conn.commit()
        return get_user(uid)
    return {'user_id': row[0], 'wallet': row[1], 'personal_wallet': row[2], 'balance': row[3]}

# === MPC SIGN (2/3 SHARDS) ===
def mpc_sign(tx):
    try:
        with open("server.txt") as f: s1 = f.read().strip()
        with open("backup.txt") as f: s2 = f.read().strip()
        partial = s1 + s2[:len(s2)//2]
        # Simplified — in prod use tss-lib
        return "tx_hash"
    except: return None

# === DEPOSIT LISTENER ===
def deposit_listener():
    last = {}
    while True:
        try:
            for user in c.execute("SELECT user_id, wallet FROM users").fetchall():
                uid, wallet = user
                bal = usdt.functions.balanceOf(w3.toChecksumAddress(wallet)).call() / 1e18
                prev = last.get(uid, 0)
                if bal > prev:
                    diff = bal - prev
                    net = diff * 0.995
                    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (net, uid))
                last[uid] = bal
            conn.commit()
            time.sleep(15)
        except: time.sleep(5)

# === MENU ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("History", callback_data="history"), InlineKeyboardButton("Trades", callback_data="open")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw")]
    ]
    await update.message.reply_text(
        f"Crucible Bot\n\n"
        f"Wallet: `{user['wallet']}`\n"
        f"Balance: ${user['balance']:.2f}\n\n"
        f"Send trade:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# === BUTTONS ===
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    if query.data == "deposit":
        qr = qrcode.make(user['wallet'])
        bio = BytesIO(); qr.save(bio, 'PNG'); bio.seek(0)
        await query.message.reply_photo(photo=bio, caption=f"Deposit USDT\n`{user['wallet']}`", parse_mode='Markdown')

# === MAIN ===
if __name__ == "__main__":
    import threading
    threading.Thread(target=deposit_listener, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.run_polling()
