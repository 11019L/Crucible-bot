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

exchange = ccxt.mexc({
    'apiKey': os.getenv("MEXC_API_KEY"),
    'secret': os.getenv("MEXC_SECRET_KEY"),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed1.binance.org'))
USDT = '0x55d398326f99059fF775485246999027B3197955'
usdt_abi = [
    {"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]
usdt = w3.eth.contract(address=USDT, abi=usdt_abi)

conn = sqlite3.connect('data.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, wallet TEXT, personal_wallet TEXT, balance REAL DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, side TEXT, qty REAL, entry REAL, leverage INTEGER, notional REAL, fee REAL, tp REAL, sl REAL, current_sl REAL, status TEXT DEFAULT 'open', order_id TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
c.execute('''CREATE TABLE IF NOT EXISTS fees (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
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

def trade_fee(notional):
    if notional < 100: return 0.30
    elif notional <= 2000: return 1.00
    else: return 3.00

def silent_deduct(wallet, fee, fee_type):
    try:
        user = get_user_from_wallet(wallet)
        c.execute("INSERT INTO fees (user_id, amount, type) VALUES (?, ?, ?)", (user['user_id'], fee, fee_type))
        conn.commit()
    except: pass

def get_user_from_wallet(wallet):
    c.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
    row = c.fetchone()
    if not row: return None
    return dict(zip([d[0] for d in c.description], row))

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
                    fee = diff * 0.005
                    net = diff - fee
                    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (net, uid))
                    silent_deduct(wallet, fee, "deposit")
                last[uid] = bal
            conn.commit()
            time.sleep(15)
        except Exception as e:
            print("Deposit error:", e)
            time.sleep(5)

def parse_trade_command(text):
    lines = [l.strip().lower() for l in text.split('\n') if l.strip()]
    side = symbol = entry_type = leverage = size_input = tp = sl = limit_price = None
    for line in lines:
        line = line.replace('/', ' ').replace('-', ' ')
        if any(w in line for w in ['long', 'buy']): side = 'buy'
        elif any(w in line for w in ['short', 'sell']): side = 'sell'
        if not symbol and any(c.isalpha() for c in line): 
            symbol = ''.join([c.upper() for c in line if c.isalpha()]) + 'USDT'
        if any(w in line for w in ['cmp', 'market', 'current']): entry_type = 'cmp'
        if any(w in line for w in ['limit', 'lim']): 
            entry_type = 'limit'
            try:
                limit_price = float([x for x in line.split() if x.replace('.','').replace('$','').isdigit()][-1])
            except: pass
        if any(w in line for w in ['leverage', 'lev', 'x']): 
            leverage = int(''.join(filter(str.isdigit, line)))
        if any(w in line for w in ['%', '$', 'capital', 'equity']): size_input = line
        if any(w in line for w in ['tp', 'take']): 
            val = line.split()[-1]
            tp = float(val.replace('%','')) if '%' in val else float(val)
        if any(w in line for w in ['sl', 'stop']): 
            val = line.split()[-1]
            sl = float(val.replace('%','')) if '%' in val else float(val)
    return side, symbol, entry_type, leverage, size_input, tp, sl, limit_price

# === TRADE COMMAND ===
async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    side, symbol, entry_type, leverage, size_input, tp, sl, limit_price = parse_trade_command(update.message.text)
    
    if not all([side, symbol, entry_type, leverage, size_input]):
        await update.message.reply_text(
            "Invalid command. Example:\n"
            "Long btc\n"
            "Cmp\n"
            "20x leverage\n"
            "$50"
        )
        return

    if leverage > 125:
        await update.message.reply_text("Max leverage: 125x")
        return

    base_size = user['balance'] * (float(size_input.replace('%','').replace('capital','').replace('equity','')) / 100) \
                if '%' in size_input else float(size_input.replace('$',''))
    notional = base_size * leverage
    fee = trade_fee(notional)
    
    if user['balance'] < base_size + fee:
        await update.message.reply_text(f"Need ${base_size + fee:.2f}")
        return

    try:
        exchange.set_leverage(leverage, symbol)
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        qty = notional / price

        if tp: tp = price * (1 + tp/100) if side == 'buy' else price * (1 - tp/100)
        if sl: sl = price * (1 - abs(sl)/100) if side == 'buy' else price * (1 + abs(sl)/100)

        if entry_type == 'cmp':
            order = exchange.create_market_order(symbol, side, qty)
            status = "EXECUTED"
        else:
            order = exchange.create_limit_order(symbol, side, qty, limit_price)
            status = "PENDING LIMIT"

        silent_deduct(user['wallet'], fee, "trade_open")
        trade_id = c.execute(
            "INSERT INTO trades (user_id, symbol, side, qty, entry, leverage, notional, fee, tp, sl, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
            (user['user_id'], symbol, side, qty, price, leverage, notional, fee, tp, sl, order.get('id'))
        ).lastrowid
        conn.commit()

        keyboard = [[InlineKeyboardButton("Cancel", callback_data=f"cancel_{order.get('id', '')}")]] if entry_type == 'limit' else []
        await update.message.reply_text(
            f"{status} {side.upper()} {symbol}\n"
            f"Size: ${notional:.0f}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

# === MENU ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("History", callback_data="history"), InlineKeyboardButton("Trades", callback_data="trades")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw"), InlineKeyboardButton("Set Wallet", callback_data="setwallet")]
    ]
    await update.message.reply_text(
        f"Crucible Bot\n\nWallet: `{user['wallet']}`\nBalance: ${user['balance']:.2f}\n\nChoose or send command.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)

    if query.data == "deposit":
        qr = qrcode.make(user['wallet'])
        bio = BytesIO()
        qr.save(bio, 'PNG')
        bio.seek(0)
        await query.message.reply_photo(
            photo=bio,
            caption=f"Send USDT (BEP-20) to:\n`{user['wallet']}`\n\nBalance updates in 30s",
            parse_mode='Markdown'
        )

    elif query.data == "history":
        trades = c.execute("""
            SELECT symbol, side, entry, leverage, notional, status, timestamp 
            FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 10
        """, (user['user_id'],)).fetchall()
        if not trades:
            msg = "No trade history"
        else:
            msg = "Last 10 Trades:\n"
            for t in trades:
                symbol, side, entry, lev, notional, status, ts = t
                msg += f"• {side.upper()} {symbol} @ ${entry:.2f}\n"
                msg += f"  {lev}x | ${notional:.0f} | {status}\n\n"
        await query.edit_message_text(msg)

    elif query.data == "trades":
        open_trades = c.execute("""
            SELECT symbol, side, entry, leverage, notional FROM trades 
            WHERE user_id=? AND status='open'
        """, (user['user_id'],)).fetchall()
        if not open_trades:
            msg = "No open trades"
        else:
            msg = "Open Trades:\n"
            for t in open_trades:
                symbol, side, entry, lev, notional = t
                msg += f"• {side.upper()} {symbol} @ ${entry:.2f}\n"
                msg += f"  {lev}x | ${notional:.0f}\n\n"
        await query.edit_message_text(msg)

    elif query.data == "withdraw":
        await query.edit_message_text(
            "Use command:\n`/withdraw 50`\n\n"
            "1% fee + $0.30 gas\nYou receive: $49.20",
            parse_mode='Markdown'
        )

    elif query.data == "setwallet":
        await query.edit_message_text(
            "Use command:\n`/setwallet 0xYourWallet`",
            parse_mode='Markdown'
        )

    elif query.data.startswith("cancel_"):
        order_id = query.data.split("_", 1)[1]
        try:
            exchange.cancel_order(order_id)
            await query.edit_message_text("Limit order cancelled.")
        except:
            await query.edit_message_text("Order already filled.")

# === WITHDRAW (USER PAYS FEE + GAS) ===
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Use: `/withdraw 100`", parse_mode='Markdown')
        return
    
    amount = float(context.args[0])
    user = get_user(update.effective_user.id)
    
    if not user['personal_wallet']:
        await update.message.reply_text("Set wallet first: `/setwallet 0x...`", parse_mode='Markdown')
        return
    
    if amount > user['balance']:
        await update.message.reply_text("Insufficient balance")
        return

    fee_percent = amount * 0.01
    gas_cost_usdt = 0.30
    total_deduct = fee_percent + gas_cost_usdt
    net_amount = amount - total_deduct

    if net_amount <= 0:
        await update.message.reply_text("Amount too low after fees/gas")
        return

    try:
        # SEND NET TO USER
        tx = usdt.functions.transfer(
            w3.toChecksumAddress(user['personal_wallet']),
            int(net_amount * 1e18)
        ).build_transaction({
            'from': w3.toChecksumAddress(user['wallet']),
            'nonce': w3.eth.get_transaction_count(user['wallet']),
            'gas': 100000,
            'gasPrice': w3.to_wei(5, 'gwei')
        })
        signed = w3.eth.account.sign_transaction(tx, 'YOUR_MPC_SHARD')  # REPLACE
        w3.eth.send_raw_transaction(signed.rawTransaction)

        # SEND FEE TO ADMIN
        fee_tx = usdt.functions.transfer(
            w3.toChecksumAddress(ADMIN_WALLET),
            int(fee_percent * 1e18)
        ).build_transaction({
            'from': w3.toChecksumAddress(user['wallet']),
            'nonce': w3.eth.get_transaction_count(user['wallet']),
            'gas': 100000,
            'gasPrice': w3.to_wei(5, 'gwei')
        })
        signed_fee = w3.eth.account.sign_transaction(fee_tx, 'YOUR_MPC_SHARD')
        w3.eth.send_raw_transaction(signed_fee.rawTransaction)

        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user['user_id']))
        conn.commit()

        await update.message.reply_text(
            f"**Withdrawal Complete**\n\n"
            f"Requested: `${amount:.2f}`\n"
            f"Fee (1%): `${fee_percent:.2f}`\n"
            f"Gas: `${gas_cost_usdt:.2f}`\n"
            f"**Sent: `${net_amount:.2f}`**",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"Withdraw failed: {str(e)}")

# === OTHER COMMANDS ===
async def setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1: 
        await update.message.reply_text("Use: `/setwallet 0xYourAddress`")
        return
    wallet = context.args[0]
    c.execute("UPDATE users SET personal_wallet=? WHERE user_id=?", (wallet, update.effective_user.id))
    conn.commit()
    await update.message.reply_text(f"Wallet set: `{wallet[:10]}...`")

# === MAIN ===
if __name__ == "__main__":
    import threading
    threading.Thread(target=deposit_listener, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setwallet", setwallet))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trade))
    app.run_polling()
