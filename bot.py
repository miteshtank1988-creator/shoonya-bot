import os,time,logging,threading
import pandas as pd
import numpy as np
from datetime import datetime
from flask import Flask,jsonify

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',datefmt='%H:%M:%S')
log=logging.getLogger(__name__)
app=Flask(__name__)
S={"signal":"WAITING","pnl":0.0,"trades":0,"wins":0,"losses":0,"log":[]}

@app.route("/")
def home():
    wr=round((S["wins"]/S["trades"])*100,1) if S["trades"]>0 else 0
    return jsonify({"Bot":"Shoonya Paper Bot","Signal":S["signal"],"PnL":f"Rs.{S['pnl']:.0f}","Trades":S["trades"],"WinRate":f"{wr}%","Log":S["log"][-5:]})

@app.route("/health")
def health():
    return jsonify({"ok":True})

class Bot:
    def __init__(self):
        self.pos=None;self.bp=None;self.pnl=0;self.t=0;self.w=0;self.l=0

    def data(self):
        np.random.seed(int(time.time())%1000)
        c=22000+np.random.randn(60).cumsum()*15
        return pd.DataFrame({"close":c,"high":c+abs(np.random.randn(60)*10),"low":c-abs(np.random.randn(60)*10)})

    def rsi(self,p):
        d=p.diff();g=d.where(d>0,0).rolling(14).mean();l=(-d.where(d<0,0)).rolling(14).mean()
        return (100-(100/(1+g/l))).iloc[-1]

    def sig(self):
        df=self.data();c=df["close"]
        r=self.rsi(c);m5=c.rolling(5).mean().iloc[-1];m20=c.rolling(20).mean().iloc[-1]
        log.info(f"RSI:{r:.1f} MA5:{m5:.0f} MA20:{m20:.0f}")
        if r<35 and m5>m20:return "CE",f"RSI:{r:.0f} Oversold"
        if r>65 and m5<m20:return "PE",f"RSI:{r:.0f} Overbought"
        return None,f"RSI:{r:.0f} Wait"

    def enter(self,o,r):
        p=round(80+np.random.uniform(20,100),2);self.pos=o;self.bp=p;self.t+=1
        msg=f"PAPER {o} BUY Rs.{p} | {r}";log.info(msg);self._log(msg)
        S["signal"]=f"{o} BUY";S["trades"]=self.t

    def exit(self,r):
        if not self.pos:return
        ep=self.bp+(50 if "Target" in r else -25 if "Stop" in r else np.random.uniform(-10,10))
        pnl=(ep-self.bp)*50;self.pnl+=pnl
        if pnl>0:self.w+=1
        else:self.l+=1
        msg=f"EXIT({r}) PnL:Rs.{pnl:.0f} Daily:Rs.{self.pnl:.0f}";log.info(msg);self._log(msg)
        S["pnl"]=self.pnl;S["signal"]="WAITING";S["wins"]=self.w;S["losses"]=self.l
        self.pos=None;self.bp=None

    def chk(self):
        if not self.pos:return
        d=np.random.uniform(-30,30)
        if d<=-25:self.exit("StopLoss")
        elif d>=50:self.exit("Target")

    def open(self):
        n=datetime.now()
        if n.weekday()>4:return False
        return n.replace(hour=9,minute=15,second=0)<=n<=n.replace(hour=15,minute=20,second=0)

    def _log(self,m):
        S["log"].append(f"[{datetime.now().strftime('%H:%M')}] {m}")
        if len(S["log"])>30:S["log"]=S["log"][-30:]

    def run(self):
        log.info("SHOONYA PAPER BOT START!");self._log("Bot start!")
        while True:
            try:
                if not self.open():time.sleep(300);continue
                if self.pnl<=-2000:self.exit("MaxLoss");time.sleep(3600);continue
                if self.pnl>=4000:self.exit("Target");time.sleep(3600);continue
                n=datetime.now()
                if n.hour==15 and n.minute>=15 and self.pos:self.exit("DayEnd");continue
                self.chk()
                if not self.pos:
                    s,r=self.sig()
                    if s:self.enter(s,r)
                    else:log.info(r)
                log.info(f"Wait... PnL:Rs.{self.pnl:.0f} Trades:{self.t}");time.sleep(30)
            except KeyboardInterrupt:break
            except Exception as e:log.error(f"Err:{e}");time.sleep(30)

def fl():
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)),use_reloader=False)

if __name__=="__main__":
    threading.Thread(target=fl,daemon=True).start()
    Bot().run()
