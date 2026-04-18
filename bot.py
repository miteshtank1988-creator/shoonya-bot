# ================================================================
# SHOONYA F&O CLOUD TRADING BOT
# Platform: Railway.app (Free)
# Type: F&O Intraday (Options BUY/SELL)
# ================================================================

import os
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime, date
from NorenRestApiPy.NorenApi import NorenApi
from flask import Flask, jsonify
import threading

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ── Flask app (Railway ke liye ping server) ───────────────────────
app = Flask(__name__)
bot_status = {"signal": "WAITING", "pnl": 0, "trades": 0, "log": []}

@app.route("/")
def home():
    return jsonify({
        "status": "🤖 Shoonya F&O Bot Running",
        "signal": bot_status["signal"],
        "pnl":    f"₹{bot_status['pnl']:.2f}",
        "trades": bot_status["trades"],
        "log":    bot_status["log"][-10:],
        "time":   datetime.now().strftime("%H:%M:%S IST"),
    })

@app.route("/health")
def health():
    return jsonify({"ok": True})

# ================================================================
# CREDENTIALS — Railway Environment Variables se aayenge
# Railway Dashboard → Variables mein set karo
# ================================================================
USER_ID      = os.environ.get("SHOONYA_USER",     "YOUR_USER_ID")
PASSWORD     = os.environ.get("SHOONYA_PASSWORD",  "YOUR_PASSWORD")
TOTP_SECRET  = os.environ.get("SHOONYA_TOTP",      "")   # Google Auth secret key
VENDOR_CODE  = os.environ.get("SHOONYA_VC",        "YOUR_VENDOR_CODE")
API_KEY      = os.environ.get("SHOONYA_APIKEY",    "YOUR_API_KEY")
IMEI         = os.environ.get("SHOONYA_IMEI",      "cloud-bot-001")

# ================================================================
# F&O SETTINGS
# ================================================================
INDEX         = "NIFTY"          # NIFTY ya BANKNIFTY
EXCHANGE      = "NFO"            # F&O exchange
SPOT_EXCHANGE = "NSE"
LOT_SIZE      = 50               # NIFTY = 50, BANKNIFTY = 15
LOTS          = 1                # Kitne lots trade karo
QTY           = LOT_SIZE * LOTS

MAX_DAILY_LOSS  = 2000           # ₹2000 se zyada loss hua to bot band
MAX_DAILY_PROFIT= 4000           # ₹4000 profit mein band
CHECK_EVERY     = 30             # 30 second mein check

# Strategy thresholds
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
STOP_LOSS_POINTS= 25             # 25 points stop loss
TARGET_POINTS   = 50             # 50 points target

# ================================================================
# BOT CLASS
# ================================================================
class FnOBot:
    def __init__(self):
        self.api        = ShoonyaApiPy()
        self.logged_in  = False
        self.position   = None    # 'CE_BUY', 'PE_BUY', None
        self.buy_price  = None
        self.order_no   = None
        self.daily_pnl  = 0
        self.trade_count= 0
        self.current_symbol = None

    # ── TOTP Generate ─────────────────────────────────────────────
    def get_totp(self):
        if TOTP_SECRET:
            try:
                import pyotp
                return pyotp.TOTP(TOTP_SECRET).now()
            except:
                pass
        return PASSWORD  # fallback: password se hi OTP

    # ── Login ─────────────────────────────────────────────────────
    def login(self):
        log.info("Shoonya login ho raha hai...")
        totp = self.get_totp()
        ret = self.api.login(
            userid      = USER_ID,
            password    = PASSWORD,
            twoFA       = totp,
            vendor_code = VENDOR_CODE,
            api_secret  = API_KEY,
            imei        = IMEI,
        )
        if ret and ret.get("stat") == "Ok":
            self.logged_in = True
            log.info(f"✅ Login OK! User: {ret.get('uname','')}")
            self._add_log("Login successful")
            return True
        log.error(f"❌ Login fail: {ret}")
        return False

    # ── ATM Strike Nikalo ─────────────────────────────────────────
    def get_atm_strike(self):
        """Current NIFTY spot price se ATM strike calculate karo"""
        ret = self.api.get_quotes(exchange=SPOT_EXCHANGE, token="26000")
        if ret and ret.get("stat") == "Ok":
            spot = float(ret.get("lp", 0))
            # ATM = nearest 50 multiple (NIFTY) ya 100 (BANKNIFTY)
            step = 50 if INDEX == "NIFTY" else 100
            atm = round(spot / step) * step
            log.info(f"📍 Spot: {spot:.0f} | ATM Strike: {atm}")
            return int(atm), spot
        return None, None

    # ── Option Symbol Banao ───────────────────────────────────────
    def get_option_symbol(self, strike, opt_type):
        """
        opt_type: 'CE' ya 'PE'
        Format: NIFTY25APR24500CE
        """
        exp = self._nearest_expiry()
        symbol = f"{INDEX}{exp}{strike}{opt_type}"
        return symbol

    def _nearest_expiry(self):
        """Is hafte ka Thursday expiry date"""
        today = date.today()
        # Thursday = 3
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        expiry = today.replace(day=today.day + days_ahead)
        # Format: 25APR (ddMMM)
        months = ["JAN","FEB","MAR","APR","MAY","JUN",
                  "JUL","AUG","SEP","OCT","NOV","DEC"]
        return f"{expiry.day:02d}{months[expiry.month-1]}"

    # ── Option Token Nikalo ───────────────────────────────────────
    def search_option(self, symbol):
        ret = self.api.searchscrip(exchange=EXCHANGE, searchtext=symbol)
        if ret and ret.get("values"):
            return ret["values"][0]["token"], ret["values"][0]["tsym"]
        return None, None

    # ── Option Price Lao ──────────────────────────────────────────
    def get_option_price(self, token):
        ret = self.api.get_quotes(exchange=EXCHANGE, token=token)
        if ret and ret.get("stat") == "Ok":
            return float(ret.get("lp", 0))
        return None

    # ── Candle Data ───────────────────────────────────────────────
    def get_candles(self):
        """NIFTY spot ka 5-min data"""
        ret = self.api.get_time_price_series(
            exchange  = SPOT_EXCHANGE,
            token     = "26000",
            starttime = int(time.time()) - 7200,  # 2 hours
            endtime   = int(time.time()),
            interval  = "5",
        )
        if not ret or len(ret) < 20:
            return None
        df = pd.DataFrame(ret)
        df["close"] = df["intc"].astype(float)
        df["high"]  = df["inth"].astype(float)
        df["low"]   = df["intl"].astype(float)
        return df

    # ── RSI ───────────────────────────────────────────────────────
    def calc_rsi(self, prices, period=14):
        delta = prices.diff()
        gain  = delta.where(delta > 0, 0).rolling(period).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs    = gain / loss
        return 100 - (100 / (1 + rs))

    # ── Supertrend ────────────────────────────────────────────────
    def calc_supertrend(self, df, period=7, multiplier=3):
        hl2  = (df["high"] + df["low"]) / 2
        atr  = (df["high"] - df["low"]).rolling(period).mean()
        upper = hl2 + (multiplier * atr)
        lower = hl2 - (multiplier * atr)
        close = df["close"]
        trend = pd.Series(index=df.index, dtype=float)
        for i in range(period, len(df)):
            if close.iloc[i] > upper.iloc[i-1]:
                trend.iloc[i] = 1   # Bullish
            elif close.iloc[i] < lower.iloc[i-1]:
                trend.iloc[i] = -1  # Bearish
            else:
                trend.iloc[i] = trend.iloc[i-1] if i > period else 0
        return trend

    # ── Signal Decide ─────────────────────────────────────────────
    def get_signal(self):
        df = self.get_candles()
        if df is None:
            return "WAIT", None

        close = df["close"]
        rsi   = self.calc_rsi(close).iloc[-1]
        ma5   = close.rolling(5).mean().iloc[-1]
        ma20  = close.rolling(20).mean().iloc[-1]
        trend = self.calc_supertrend(df).iloc[-1]

        log.info(f"📊 RSI:{rsi:.1f} | MA5:{ma5:.0f} | MA20:{ma20:.0f} | Trend:{trend:.0f}")

        # CE BUY (market upar jayega)
        if (rsi < RSI_OVERSOLD and
            ma5 > ma20 and
            trend == 1):
            return "CE_BUY", f"RSI:{rsi:.0f} Oversold + Supertrend UP"

        # PE BUY (market neeche jayega)
        elif (rsi > RSI_OVERBOUGHT and
              ma5 < ma20 and
              trend == -1):
            return "PE_BUY", f"RSI:{rsi:.0f} Overbought + Supertrend DOWN"

        return "WAIT", f"RSI:{rsi:.0f} — Koi signal nahi"

    # ── Order Place ───────────────────────────────────────────────
    def enter_trade(self, signal, reason):
        if self.position:
            return  # Already mein position hai

        atm, spot = self.get_atm_strike()
        if not atm:
            return

        opt_type = "CE" if signal == "CE_BUY" else "PE"
        sym      = self.get_option_symbol(atm, opt_type)
        token, tsym = self.search_option(sym)
        if not token:
            log.error(f"Option symbol nahi mila: {sym}")
            return

        price = self.get_option_price(token)
        log.info(f"🎯 {signal}: {tsym} @ ₹{price} | {reason}")

        ret = self.api.place_order(
            buy_or_sell   = "B",
            product_type  = "M",        # M = MIS (intraday)
            exchange      = EXCHANGE,
            tradingsymbol = tsym,
            quantity      = QTY,
            discloseqty   = 0,
            price_type    = "MKT",
            price         = 0,
            trigger_price = None,
            retention     = "DAY",
            remarks       = f"FnOBot_{signal}",
        )
        if ret and ret.get("stat") == "Ok":
            self.position       = signal
            self.buy_price      = price
            self.current_symbol = tsym
            self.order_no       = ret.get("norenordno")
            self.trade_count   += 1
            msg = f"✅ {signal} entered: {tsym} @ ₹{price:.2f}"
            log.info(msg)
            self._add_log(msg)
            bot_status["signal"] = signal
        else:
            log.error(f"❌ Order fail: {ret}")

    def exit_trade(self, reason):
        if not self.position or not self.current_symbol:
            return

        token, tsym = self.search_option(self.current_symbol)
        price = self.get_option_price(token) if token else 0

        ret = self.api.place_order(
            buy_or_sell   = "S",
            product_type  = "M",
            exchange      = EXCHANGE,
            tradingsymbol = tsym or self.current_symbol,
            quantity      = QTY,
            discloseqty   = 0,
            price_type    = "MKT",
            price         = 0,
            trigger_price = None,
            retention     = "DAY",
            remarks       = f"FnOBot_EXIT_{reason}",
        )
        if ret and ret.get("stat") == "Ok":
            pnl = (price - self.buy_price) * QTY if price and self.buy_price else 0
            self.daily_pnl += pnl
            msg = f"🔴 EXIT ({reason}) | P&L: ₹{pnl:.2f} | Daily: ₹{self.daily_pnl:.2f}"
            log.info(msg)
            self._add_log(msg)
            bot_status["pnl"]    = self.daily_pnl
            bot_status["trades"] = self.trade_count
            bot_status["signal"] = "WAITING"
            self.position        = None
            self.buy_price       = None
            self.current_symbol  = None
        else:
            log.error(f"❌ EXIT fail: {ret}")

    # ── Stop Loss / Target Check ──────────────────────────────────
    def check_exit_conditions(self):
        if not self.position or not self.current_symbol:
            return
        token, _ = self.search_option(self.current_symbol)
        if not token:
            return
        cur_price = self.get_option_price(token)
        if not cur_price or not self.buy_price:
            return

        diff = cur_price - self.buy_price
        if diff <= -STOP_LOSS_POINTS:
            self.exit_trade("StopLoss")
        elif diff >= TARGET_POINTS:
            self.exit_trade("Target")

    # ── Market Hours ──────────────────────────────────────────────
    def is_market_open(self):
        now = datetime.now()
        if now.weekday() > 4:
            return False
        s = now.replace(hour=9,  minute=15, second=0)
        e = now.replace(hour=15, minute=20, second=0)  # F&O 15:20 close
        return s <= now <= e

    def is_closing_time(self):
        now = datetime.now()
        return now.hour == 15 and now.minute >= 15

    # ── Log ───────────────────────────────────────────────────────
    def _add_log(self, msg):
        t = datetime.now().strftime("%H:%M")
        bot_status["log"].append(f"[{t}] {msg}")

    # ── Main Loop ─────────────────────────────────────────────────
    def run(self):
        if not self.login():
            return

        log.info("🚀 F&O Bot Cloud par chal raha hai!")
        log.info(f"📋 Index: {INDEX} | Lot: {LOT_SIZE} x {LOTS} = {QTY} qty")
        log.info(f"🛡️  SL: {STOP_LOSS_POINTS}pts | Target: {TARGET_POINTS}pts")
        log.info(f"💰 Max Loss/Day: ₹{MAX_DAILY_LOSS} | Max Profit: ₹{MAX_DAILY_PROFIT}")

        while True:
            try:
                if not self.is_market_open():
                    log.info("⏰ Market band — 9:15 AM ka wait...")
                    time.sleep(300)
                    continue

                # Daily limits check
                if self.daily_pnl <= -MAX_DAILY_LOSS:
                    log.warning(f"🛑 MAX LOSS hit! ₹{self.daily_pnl:.2f} — Bot band")
                    self.exit_trade("MaxLoss")
                    time.sleep(3600)
                    continue

                if self.daily_pnl >= MAX_DAILY_PROFIT:
                    log.info(f"🎉 TARGET reached! ₹{self.daily_pnl:.2f} — Bot band")
                    self.exit_trade("DailyTarget")
                    time.sleep(3600)
                    continue

                # 3:15 PM — force close all positions
                if self.is_closing_time() and self.position:
                    self.exit_trade("DayEnd_ForceClose")
                    time.sleep(600)
                    continue

                # Stop loss / target check
                self.check_exit_conditions()

                # Naya signal
                if not self.position:
                    signal, reason = self.get_signal()
                    if signal != "WAIT":
                        self.enter_trade(signal, reason)
                    else:
                        log.info(f"⏸️  {reason}")

                time.sleep(CHECK_EVERY)

            except KeyboardInterrupt:
                log.info("Bot band (Ctrl+C)")
                self.exit_trade("ManualExit")
                break
            except Exception as e:
                log.error(f"Error: {e}")
                time.sleep(30)

# ================================================================
# START — Bot aur Flask dono parallel chalao
# ================================================================
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Flask server background mein
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    log.info("🌐 Web server start hua")

    # Trading bot main thread mein
    bot = FnOBot()
    bot.run()
