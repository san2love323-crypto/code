# -*- coding: utf-8 -*-
# ============================================================
#  종합샵 관리 봇  (main.py 단일 파일 / discord.py 2.x)
#  관리 기능 100개 + 정보 명령어 6개  ·  슬래시(/) 그룹 방식
#  설치:  pip install -U discord.py
#  실행:  python3 main.py
# ============================================================
import asyncio, datetime, io, json, random, re, sqlite3, time
from collections import defaultdict, deque
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = "여기에_봇_토큰_넣기"

SHOP_NAME = "종합샵"          # 임베드에 표시될 샵 이름
WARN_TIMEOUT_AT = 3           # 경고 N회 누적 시 자동 타임아웃(1시간)
WARN_BAN_AT = 5               # 경고 N회 누적 시 자동 차단
KST = datetime.timezone(datetime.timedelta(hours=9))
BLUE, GREEN, RED, YELLOW, GRAY = 0x5865F2, 0x57F287, 0xED4245, 0xFEE75C, 0x2B2D31

TICKET_KINDS = {
    "buy": ("🛒", "구매 문의"),
    "ask": ("❓", "일반 문의"),
    "partner": ("🤝", "제휴 문의"),
    "report": ("🚨", "신고·환불"),
}

# ------------------------------------------------------------ DB
db = sqlite3.connect("shop.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS cfg(gid INTEGER, key TEXT, val TEXT, PRIMARY KEY(gid,key));
CREATE TABLE IF NOT EXISTS lists(gid INTEGER, kind TEXT, val TEXT, PRIMARY KEY(gid,kind,val));
CREATE TABLE IF NOT EXISTS tickets(ch INTEGER PRIMARY KEY, gid INTEGER, uid INTEGER, kind TEXT, num INTEGER,
    status TEXT, claimed INTEGER, priority TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, mod INTEGER,
    kind TEXT, reason TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS warns(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, mod INTEGER,
    reason TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, mod INTEGER,
    text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, name TEXT, price INTEGER,
    stock INTEGER, descr TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, pid INTEGER,
    pname TEXT, qty INTEGER, total INTEGER, status TEXT, staff INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, stars INTEGER,
    text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS timers(id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, kind TEXT, uid INTEGER,
    rid INTEGER, ch INTEGER, text TEXT, until INTEGER);
CREATE TABLE IF NOT EXISTS gws(msg INTEGER PRIMARY KEY, gid INTEGER, ch INTEGER, prize TEXT, winners INTEGER,
    until INTEGER, ended INTEGER, entrants TEXT);
""")
db.commit()


def q(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


def cfg(gid, key, default=None):
    r = q("SELECT val FROM cfg WHERE gid=? AND key=?", gid, key).fetchone()
    return r["val"] if r else default


def cfgint(gid, key, default=0):
    try:
        return int(cfg(gid, key, default))
    except (TypeError, ValueError):
        return default


def setcfg(gid, key, val):
    if val is None:
        q("DELETE FROM cfg WHERE gid=? AND key=?", gid, key)
    else:
        q("REPLACE INTO cfg VALUES(?,?,?)", gid, key, str(val))


def in_list(gid, kind, val):
    return q("SELECT 1 FROM lists WHERE gid=? AND kind=? AND val=?", gid, kind, str(val)).fetchone() is not None


def get_list(gid, kind):
    return [r["val"] for r in q("SELECT val FROM lists WHERE gid=? AND kind=?", gid, kind)]


def add_case(gid, uid, mod, kind, reason):
    q("INSERT INTO cases(gid,uid,mod,kind,reason,ts) VALUES(?,?,?,?,?,?)", gid, uid, mod, kind, reason, int(time.time()))


# ------------------------------------------------------------ 공용 함수
def emb(title, desc="", color=BLUE):
    e = discord.Embed(title=title, description=str(desc)[:4000], color=color)
    e.set_footer(text=SHOP_NAME)
    e.timestamp = discord.utils.utcnow()
    return e


async def say(i: discord.Interaction, text="", title="✅ 완료", color=GREEN, eph=True, **kw):
    e = emb(title, text, color)
    if i.response.is_done():
        await i.followup.send(embed=e, ephemeral=eph, **kw)
    else:
        await i.response.send_message(embed=e, ephemeral=eph, **kw)


async def fail(i, text):
    await say(i, text, "❌ 실패", RED)


def is_staff(m: discord.Member):
    if m.guild_permissions.manage_guild:
        return True
    rid = cfgint(m.guild.id, "staffrole")
    return any(r.id == rid for r in m.roles)


def can_act(i: discord.Interaction, target: discord.Member):
    g = i.guild
    if target.id == g.owner_id or target.id == i.user.id or target.id == g.me.id:
        return False
    if i.user.id != g.owner_id and target.top_role >= i.user.top_role:
        return False
    return target.top_role < g.me.top_role


async def log(guild: discord.Guild, title, desc, color=GRAY, file=None):
    ch = guild.get_channel(cfgint(guild.id, "log"))
    if ch:
        try:
            await ch.send(embed=emb(title, desc, color), file=file)
        except discord.HTTPException:
            pass


def parse_dur(s: str):
    """10m, 2h, 1d, 30분, 2시간, 7일 → 초"""
    m = re.fullmatch(r"\s*(\d+)\s*(s|m|h|d|초|분|시간|일)\s*", s.lower())
    if not m:
        return None
    mult = {"s": 1, "초": 1, "m": 60, "분": 60, "h": 3600, "시간": 3600, "d": 86400, "일": 86400}
    return int(m.group(1)) * mult[m.group(2)]


def ts(t, style="f"):
    return f"<t:{int(t)}:{style}>"


def won(n):
    return f"{n:,}원"


NOPING = discord.AllowedMentions.none()


class G(app_commands.Group):
    """관리자 전용 그룹 (서버 관리 권한 또는 설정된 관리자 역할)"""

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if isinstance(i.user, discord.Member) and is_staff(i.user):
            return True
        await fail(i, "관리자만 사용할 수 있는 명령어예요.")
        return False


STAFF_PERM = discord.Permissions(manage_messages=True)
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ============================================================ 티켓 시스템
def ticket_panel_view():
    v = discord.ui.View(timeout=None)
    for k, (emo, label) in TICKET_KINDS.items():
        v.add_item(discord.ui.Button(label=label, emoji=emo, style=discord.ButtonStyle.primary, custom_id=f"tk:open:{k}"))
    return v


def ticket_open_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="닫기", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="tk:close"))
    v.add_item(discord.ui.Button(label="담당하기", emoji="🙋", style=discord.ButtonStyle.success, custom_id="tk:claim"))
    return v


def ticket_closed_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="재오픈", emoji="🔓", style=discord.ButtonStyle.success, custom_id="tk:reopen"))
    v.add_item(discord.ui.Button(label="기록 저장", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="tk:save"))
    v.add_item(discord.ui.Button(label="삭제", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="tk:del"))
    return v


def get_ticket(ch_id):
    return q("SELECT * FROM tickets WHERE ch=?", ch_id).fetchone()


async def transcript_bytes(ch: discord.TextChannel):
    lines = [f"# {ch.guild.name} / #{ch.name} 대화 기록", ""]
    async for m in ch.history(limit=None, oldest_first=True):
        t = m.created_at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
        c = m.content or ""
        for e in m.embeds:
            c += f" [임베드] {e.title or ''} {e.description or ''}"
        for a in m.attachments:
            c += f" [파일] {a.url}"
        lines.append(f"[{t}] {m.author} ({m.author.id}): {c}")
    return "\n".join(lines).encode("utf-8")


class TicketModal(discord.ui.Modal):
    def __init__(self, kind):
        super().__init__(title=f"{TICKET_KINDS[kind][1]} 티켓 열기")
        self.kind = kind
        self.body = discord.ui.TextInput(label="문의 내용을 적어주세요", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.body)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        g, u = i.guild, i.user
        if in_list(g.id, "tkban", u.id):
            return await fail(i, "티켓 이용이 제한된 상태예요.")
        limit = cfgint(g.id, "tklimit", 1)
        opened = q("SELECT COUNT(*) c FROM tickets WHERE gid=? AND uid=? AND status='open'", g.id, u.id).fetchone()["c"]
        if opened >= limit:
            return await fail(i, f"이미 열려 있는 티켓이 있어요. (최대 {limit}개)")
        cat = g.get_channel(cfgint(g.id, "tkcat"))
        staff = g.get_role(cfgint(g.id, "staffrole"))
        ow = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            u: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        if staff:
            ow[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        num = cfgint(g.id, "tknum") + 1
        setcfg(g.id, "tknum", num)
        emo, label = TICKET_KINDS[self.kind]
        ch = await g.create_text_channel(f"{label.replace(' ', '')}-{num:04d}", category=cat if isinstance(cat, discord.CategoryChannel) else None,
                                         overwrites=ow, topic=f"{u} ({u.id}) 의 {label} 티켓")
        q("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?)", ch.id, g.id, u.id, self.kind, num, "open", 0, "보통", int(time.time()))
        e = emb(f"{emo} {label} #{num:04d}", f"{u.mention} 님, 문의해 주셔서 감사합니다.\n담당자가 곧 답변드릴게요.\n\n**문의 내용**\n{self.body.value}")
        await ch.send(content=u.mention + (f" {staff.mention}" if staff else ""), embed=e, view=ticket_open_view(),
                      allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await say(i, f"티켓이 열렸어요 → {ch.mention}")
        await log(g, "🎫 티켓 생성", f"{u.mention} · {label} · {ch.mention}", GREEN)


async def tk_close(i: discord.Interaction):
    tk = get_ticket(i.channel.id)
    if not tk:
        return await fail(i, "여기는 티켓 채널이 아니에요.")
    if tk["status"] != "open":
        return await fail(i, "이미 닫힌 티켓이에요.")
    if not (is_staff(i.user) or i.user.id == tk["uid"]):
        return await fail(i, "권한이 없어요.")
    await i.response.defer()
    owner = i.guild.get_member(tk["uid"])
    if owner:
        await i.channel.set_permissions(owner, view_channel=True, send_messages=False, read_message_history=True)
    q("UPDATE tickets SET status='closed' WHERE ch=?", i.channel.id)
    await i.followup.send(embed=emb("🔒 티켓이 닫혔습니다", f"닫은 사람: {i.user.mention}", YELLOW), view=ticket_closed_view())
    await log(i.guild, "🔒 티켓 닫힘", f"{i.channel.mention} · {i.user.mention}", YELLOW)


async def tk_reopen(i: discord.Interaction):
    tk = get_ticket(i.channel.id)
    if not tk or tk["status"] != "closed":
        return await fail(i, "닫힌 티켓 채널에서만 쓸 수 있어요.")
    if not is_staff(i.user):
        return await fail(i, "관리자만 재오픈할 수 있어요.")
    owner = i.guild.get_member(tk["uid"])
    if owner:
        await i.channel.set_permissions(owner, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    q("UPDATE tickets SET status='open' WHERE ch=?", i.channel.id)
    await i.response.send_message(embed=emb("🔓 티켓이 다시 열렸습니다", f"{i.user.mention}", GREEN), view=ticket_open_view())


async def tk_claim(i: discord.Interaction):
    tk = get_ticket(i.channel.id)
    if not tk:
        return await fail(i, "여기는 티켓 채널이 아니에요.")
    if not is_staff(i.user):
        return await fail(i, "관리자만 담당할 수 있어요.")
    q("UPDATE tickets SET claimed=? WHERE ch=?", i.user.id, i.channel.id)
    await i.response.send_message(embed=emb("🙋 담당자 배정", f"{i.user.mention} 님이 이 티켓을 담당합니다.", GREEN))


async def tk_save(i: discord.Interaction):
    if not get_ticket(i.channel.id):
        return await fail(i, "여기는 티켓 채널이 아니에요.")
    if not is_staff(i.user):
        return await fail(i, "관리자만 저장할 수 있어요.")
    await i.response.defer(ephemeral=True)
    data = await transcript_bytes(i.channel)
    await i.followup.send(file=discord.File(io.BytesIO(data), f"{i.channel.name}.txt"), ephemeral=True)


async def tk_delete(i: discord.Interaction):
    tk = get_ticket(i.channel.id)
    if not tk:
        return await fail(i, "여기는 티켓 채널이 아니에요.")
    if not is_staff(i.user):
        return await fail(i, "관리자만 삭제할 수 있어요.")
    await i.response.send_message(embed=emb("🗑️ 5초 뒤 티켓이 삭제됩니다", "대화 기록은 로그 채널에 저장돼요.", RED))
    data = await transcript_bytes(i.channel)
    await log(i.guild, "🗑️ 티켓 삭제", f"#{i.channel.name} · 개설자 <@{tk['uid']}> · 삭제 {i.user.mention}", RED,
              file=discord.File(io.BytesIO(data), f"{i.channel.name}.txt"))
    q("UPDATE tickets SET status='deleted' WHERE ch=?", i.channel.id)
    await asyncio.sleep(5)
    await i.channel.delete(reason=f"티켓 삭제 by {i.user}")


class Ticket(G):
    @app_commands.command(name="패널", description="티켓 열기 버튼 패널을 보냅니다")
    async def panel(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None, 제목: str = "🎫 문의 티켓",
                    설명: str = "아래 버튼을 눌러 원하는 문의 티켓을 열어주세요.\\n장난 티켓은 제재될 수 있습니다."):
        ch = 채널 or i.channel
        await ch.send(embed=emb(제목, 설명.replace("\\n", "\n")), view=ticket_panel_view())
        await say(i, f"{ch.mention} 에 티켓 패널을 보냈어요.")

    @app_commands.command(name="설정", description="티켓이 생성될 카테고리를 설정합니다")
    async def setup(self, i: discord.Interaction, 카테고리: discord.CategoryChannel):
        setcfg(i.guild.id, "tkcat", 카테고리.id)
        await say(i, f"티켓 카테고리 → **{카테고리.name}**\n(담당 역할은 `/설정 관리자역할`, 기록은 `/설정 로그채널`)")

    @app_commands.command(name="닫기", description="현재 티켓을 닫습니다")
    async def close(self, i: discord.Interaction):
        await tk_close(i)

    @app_commands.command(name="재오픈", description="닫힌 티켓을 다시 엽니다")
    async def reopen(self, i: discord.Interaction):
        await tk_reopen(i)

    @app_commands.command(name="삭제", description="티켓을 기록 저장 후 삭제합니다")
    async def delete(self, i: discord.Interaction):
        await tk_delete(i)

    @app_commands.command(name="추가", description="티켓에 유저를 추가합니다")
    async def add(self, i: discord.Interaction, 유저: discord.Member):
        if not get_ticket(i.channel.id):
            return await fail(i, "여기는 티켓 채널이 아니에요.")
        await i.channel.set_permissions(유저, view_channel=True, send_messages=True, read_message_history=True)
        await say(i, f"{유저.mention} 님을 티켓에 추가했어요.", eph=False)

    @app_commands.command(name="제거", description="티켓에서 유저를 제거합니다")
    async def remove(self, i: discord.Interaction, 유저: discord.Member):
        if not get_ticket(i.channel.id):
            return await fail(i, "여기는 티켓 채널이 아니에요.")
        await i.channel.set_permissions(유저, overwrite=None)
        await say(i, f"{유저.mention} 님을 티켓에서 제거했어요.", eph=False)

    @app_commands.command(name="담당", description="이 티켓의 담당자가 됩니다")
    async def claim(self, i: discord.Interaction):
        await tk_claim(i)

    @app_commands.command(name="이름변경", description="티켓 채널 이름을 바꿉니다")
    async def rename(self, i: discord.Interaction, 이름: str):
        if not get_ticket(i.channel.id):
            return await fail(i, "여기는 티켓 채널이 아니에요.")
        await i.channel.edit(name=이름)
        await say(i, f"채널 이름 → **{이름}**")

    @app_commands.command(name="저장", description="티켓 대화 기록을 파일로 받습니다")
    async def save(self, i: discord.Interaction):
        await tk_save(i)

    @app_commands.command(name="우선순위", description="티켓 우선순위를 표시합니다")
    async def priority(self, i: discord.Interaction, 단계: Literal["긴급", "높음", "보통", "낮음"]):
        tk = get_ticket(i.channel.id)
        if not tk:
            return await fail(i, "여기는 티켓 채널이 아니에요.")
        q("UPDATE tickets SET priority=? WHERE ch=?", 단계, i.channel.id)
        icon = {"긴급": "🔴", "높음": "🟠", "보통": "🟢", "낮음": "⚪"}[단계]
        await say(i, f"우선순위: {icon} **{단계}**", "📌 우선순위 변경", YELLOW, eph=False)

    @app_commands.command(name="목록", description="열려 있는 티켓 목록")
    async def list_(self, i: discord.Interaction):
        rows = q("SELECT * FROM tickets WHERE gid=? AND status='open' ORDER BY created", i.guild.id).fetchall()
        txt = "\n".join(f"<#{r['ch']}> · <@{r['uid']}> · {TICKET_KINDS.get(r['kind'], ('', r['kind']))[1]} · {r['priority']} · "
                        f"{'담당 <@%d>' % r['claimed'] if r['claimed'] else '미배정'} · {ts(r['created'], 'R')}" for r in rows)
        await say(i, txt or "열린 티켓이 없어요.", f"🎫 열린 티켓 {len(rows)}개", BLUE)

    @app_commands.command(name="통계", description="티켓 통계를 봅니다")
    async def stats(self, i: discord.Interaction):
        g = i.guild.id
        tot = q("SELECT COUNT(*) c FROM tickets WHERE gid=?", g).fetchone()["c"]
        op = q("SELECT COUNT(*) c FROM tickets WHERE gid=? AND status='open'", g).fetchone()["c"]
        kinds = q("SELECT kind, COUNT(*) c FROM tickets WHERE gid=? GROUP BY kind", g).fetchall()
        staff = q("SELECT claimed, COUNT(*) c FROM tickets WHERE gid=? AND claimed>0 GROUP BY claimed ORDER BY c DESC LIMIT 10", g).fetchall()
        txt = f"전체 **{tot}** · 열림 **{op}** · 종료 **{tot - op}**\n\n**종류별**\n"
        txt += "\n".join(f"{TICKET_KINDS.get(r['kind'], ('', r['kind']))[1]}: {r['c']}" for r in kinds) or "없음"
        txt += "\n\n**담당자 TOP**\n" + ("\n".join(f"<@{r['claimed']}>: {r['c']}건" for r in staff) or "없음")
        await say(i, txt, "📊 티켓 통계", BLUE)

    @app_commands.command(name="차단", description="유저의 티켓 이용을 막습니다")
    async def ban(self, i: discord.Interaction, 유저: discord.Member):
        q("REPLACE INTO lists VALUES(?,?,?)", i.guild.id, "tkban", str(유저.id))
        await say(i, f"{유저.mention} 님은 이제 티켓을 열 수 없어요.")

    @app_commands.command(name="차단해제", description="티켓 이용 차단을 해제합니다")
    async def unban(self, i: discord.Interaction, 유저: discord.Member):
        q("DELETE FROM lists WHERE gid=? AND kind='tkban' AND val=?", i.guild.id, str(유저.id))
        await say(i, f"{유저.mention} 님의 티켓 차단을 풀었어요.")

    @app_commands.command(name="제한", description="1인당 동시에 열 수 있는 티켓 수")
    async def limit(self, i: discord.Interaction, 개수: app_commands.Range[int, 1, 5]):
        setcfg(i.guild.id, "tklimit", 개수)
        await say(i, f"1인당 최대 **{개수}개**까지 열 수 있어요.")


# ============================================================ 제재
class Mod(G):
    async def _guard(self, i, m):
        if not can_act(i, m):
            await fail(i, "이 유저에게는 사용할 수 없어요. (역할 순위 확인)")
            return False
        return True

    @app_commands.command(name="추방", description="유저를 추방합니다")
    async def kick(self, i: discord.Interaction, 유저: discord.Member, 사유: str = "사유 없음"):
        if not await self._guard(i, 유저):
            return
        await 유저.kick(reason=f"{i.user}: {사유}")
        add_case(i.guild.id, 유저.id, i.user.id, "추방", 사유)
        await say(i, f"{유저} 추방 · {사유}")
        await log(i.guild, "👢 추방", f"{유저} ({유저.id})\n처리: {i.user.mention}\n사유: {사유}", RED)

    @app_commands.command(name="차단", description="유저를 차단합니다")
    async def ban(self, i: discord.Interaction, 유저: discord.User, 사유: str = "사유 없음",
                  메시지삭제일: app_commands.Range[int, 0, 7] = 0):
        m = i.guild.get_member(유저.id)
        if m and not await self._guard(i, m):
            return
        await i.guild.ban(유저, reason=f"{i.user}: {사유}", delete_message_seconds=메시지삭제일 * 86400)
        add_case(i.guild.id, 유저.id, i.user.id, "차단", 사유)
        await say(i, f"{유저} 차단 · {사유}")
        await log(i.guild, "🔨 차단", f"{유저} ({유저.id})\n처리: {i.user.mention}\n사유: {사유}", RED)

    @app_commands.command(name="차단해제", description="유저 ID로 차단을 해제합니다")
    async def unban(self, i: discord.Interaction, 유저아이디: str, 사유: str = "사유 없음"):
        if not 유저아이디.isdigit():
            return await fail(i, "유저 ID(숫자)를 입력해 주세요.")
        try:
            await i.guild.unban(discord.Object(int(유저아이디)), reason=f"{i.user}: {사유}")
        except discord.NotFound:
            return await fail(i, "차단 목록에 없는 유저예요.")
        add_case(i.guild.id, int(유저아이디), i.user.id, "차단해제", 사유)
        await say(i, f"`{유저아이디}` 차단 해제")
        await log(i.guild, "🔓 차단 해제", f"<@{유저아이디}>\n처리: {i.user.mention}", GREEN)

    @app_commands.command(name="소프트밴", description="차단 후 바로 해제해 최근 메시지만 지웁니다")
    async def softban(self, i: discord.Interaction, 유저: discord.Member, 사유: str = "사유 없음"):
        if not await self._guard(i, 유저):
            return
        await i.guild.ban(유저, reason=f"소프트밴 {i.user}: {사유}", delete_message_seconds=86400)
        await i.guild.unban(유저, reason="소프트밴 해제")
        add_case(i.guild.id, 유저.id, i.user.id, "소프트밴", 사유)
        await say(i, f"{유저} 소프트밴 완료")
        await log(i.guild, "🧹 소프트밴", f"{유저} ({유저.id})\n처리: {i.user.mention}\n사유: {사유}", RED)

    @app_commands.command(name="기간차단", description="기간이 지나면 자동으로 풀리는 차단 (예: 7d, 12h, 3일)")
    async def tempban(self, i: discord.Interaction, 유저: discord.Member, 기간: str, 사유: str = "사유 없음"):
        sec = parse_dur(기간)
        if not sec:
            return await fail(i, "기간 형식: `30m` `12h` `7d` `3일`")
        if not await self._guard(i, 유저):
            return
        await i.guild.ban(유저, reason=f"기간차단 {기간} {i.user}: {사유}")
        until = int(time.time()) + sec
        q("INSERT INTO timers(gid,kind,uid,until) VALUES(?,?,?,?)", i.guild.id, "unban", 유저.id, until)
        add_case(i.guild.id, 유저.id, i.user.id, f"기간차단({기간})", 사유)
        await say(i, f"{유저} 차단 · 해제 예정 {ts(until)}")
        await log(i.guild, "⏳ 기간 차단", f"{유저} ({유저.id}) · {기간}\n처리: {i.user.mention}\n사유: {사유}", RED)

    @app_commands.command(name="타임아웃", description="유저를 일정 시간 채팅 금지합니다 (예: 10m, 1h, 1d)")
    async def timeout(self, i: discord.Interaction, 유저: discord.Member, 기간: str, 사유: str = "사유 없음"):
        sec = parse_dur(기간)
        if not sec or sec > 28 * 86400:
            return await fail(i, "기간 형식: `10m` `1h` `1d` (최대 28일)")
        if not await self._guard(i, 유저):
            return
        await 유저.timeout(datetime.timedelta(seconds=sec), reason=f"{i.user}: {사유}")
        add_case(i.guild.id, 유저.id, i.user.id, f"타임아웃({기간})", 사유)
        await say(i, f"{유저.mention} 타임아웃 {기간} · {사유}")
        await log(i.guild, "🔇 타임아웃", f"{유저.mention} · {기간}\n처리: {i.user.mention}\n사유: {사유}", YELLOW)

    @app_commands.command(name="타임아웃해제", description="타임아웃을 해제합니다")
    async def untimeout(self, i: discord.Interaction, 유저: discord.Member):
        await 유저.timeout(None, reason=f"해제 by {i.user}")
        add_case(i.guild.id, 유저.id, i.user.id, "타임아웃해제", "-")
        await say(i, f"{유저.mention} 타임아웃 해제")

    @app_commands.command(name="경고", description="유저에게 경고를 줍니다 (누적 시 자동 제재)")
    async def warn(self, i: discord.Interaction, 유저: discord.Member, 사유: str):
        if not await self._guard(i, 유저):
            return
        g = i.guild
        q("INSERT INTO warns(gid,uid,mod,reason,ts) VALUES(?,?,?,?,?)", g.id, 유저.id, i.user.id, 사유, int(time.time()))
        add_case(g.id, 유저.id, i.user.id, "경고", 사유)
        n = q("SELECT COUNT(*) c FROM warns WHERE gid=? AND uid=?", g.id, 유저.id).fetchone()["c"]
        extra = ""
        try:
            await 유저.send(embed=emb(f"⚠️ {g.name} 경고", f"사유: {사유}\n누적 경고: {n}회", YELLOW))
        except discord.HTTPException:
            pass
        try:
            if n >= WARN_BAN_AT:
                await g.ban(유저, reason=f"경고 {n}회 누적")
                extra = "\n→ 누적으로 **자동 차단**"
            elif n >= WARN_TIMEOUT_AT:
                await 유저.timeout(datetime.timedelta(hours=1), reason=f"경고 {n}회 누적")
                extra = "\n→ 누적으로 **1시간 타임아웃**"
        except discord.HTTPException:
            extra = "\n→ 자동 제재 실패(권한 확인)"
        await say(i, f"{유저.mention} 경고 (누적 {n}회) · {사유}{extra}", "⚠️ 경고", YELLOW, eph=False)
        await log(g, "⚠️ 경고", f"{유저.mention} 누적 {n}회\n처리: {i.user.mention}\n사유: {사유}{extra}", YELLOW)

    @app_commands.command(name="경고목록", description="유저의 경고 내역")
    async def warns(self, i: discord.Interaction, 유저: discord.User):
        rows = q("SELECT * FROM warns WHERE gid=? AND uid=? ORDER BY id DESC LIMIT 20", i.guild.id, 유저.id).fetchall()
        txt = "\n".join(f"`#{r['id']}` {ts(r['ts'], 'd')} · {r['reason']} · <@{r['mod']}>" for r in rows)
        await say(i, txt or "경고가 없어요.", f"⚠️ {유저} 경고 {len(rows)}건", YELLOW)

    @app_commands.command(name="경고삭제", description="경고 번호로 경고 1건을 삭제합니다")
    async def delwarn(self, i: discord.Interaction, 번호: int):
        c = q("DELETE FROM warns WHERE gid=? AND id=?", i.guild.id, 번호).rowcount
        await (say(i, f"경고 `#{번호}` 삭제") if c else fail(i, "해당 번호의 경고가 없어요."))

    @app_commands.command(name="경고초기화", description="유저의 경고를 모두 지웁니다")
    async def clearwarn(self, i: discord.Interaction, 유저: discord.User):
        c = q("DELETE FROM warns WHERE gid=? AND uid=?", i.guild.id, 유저.id).rowcount
        await say(i, f"{유저.mention} 경고 {c}건 초기화")

    @app_commands.command(name="기록", description="유저의 전체 제재 기록")
    async def history(self, i: discord.Interaction, 유저: discord.User):
        rows = q("SELECT * FROM cases WHERE gid=? AND uid=? ORDER BY id DESC LIMIT 20", i.guild.id, 유저.id).fetchall()
        txt = "\n".join(f"`#{r['id']}` {ts(r['ts'], 'd')} **{r['kind']}** · {r['reason']} · <@{r['mod']}>" for r in rows)
        await say(i, txt or "기록이 없어요.", f"📂 {유저} 제재 기록", BLUE)

    @app_commands.command(name="메모", description="유저에 대한 관리자 메모를 남깁니다")
    async def note(self, i: discord.Interaction, 유저: discord.User, 내용: str):
        q("INSERT INTO notes(gid,uid,mod,text,ts) VALUES(?,?,?,?,?)", i.guild.id, 유저.id, i.user.id, 내용, int(time.time()))
        await say(i, f"{유저.mention} 메모 저장")

    @app_commands.command(name="메모목록", description="유저에 대한 관리자 메모를 봅니다")
    async def notes(self, i: discord.Interaction, 유저: discord.User):
        rows = q("SELECT * FROM notes WHERE gid=? AND uid=? ORDER BY id DESC LIMIT 20", i.guild.id, 유저.id).fetchall()
        txt = "\n".join(f"{ts(r['ts'], 'd')} <@{r['mod']}>: {r['text']}" for r in rows)
        await say(i, txt or "메모가 없어요.", f"📝 {유저} 메모", BLUE)

    @app_commands.command(name="닉네임", description="유저의 닉네임을 바꿉니다 (비우면 초기화)")
    async def nick(self, i: discord.Interaction, 유저: discord.Member, 닉네임: Optional[str] = None):
        await 유저.edit(nick=닉네임, reason=f"by {i.user}")
        await say(i, f"{유저.mention} 닉네임 → {닉네임 or '초기화'}")

    @app_commands.command(name="대량차단", description="유저 ID 여러 개를 띄어쓰기로 구분해 한 번에 차단합니다")
    async def massban(self, i: discord.Interaction, 아이디들: str, 사유: str = "대량 차단"):
        ids = [int(x) for x in re.findall(r"\d{15,20}", 아이디들)][:50]
        if not ids:
            return await fail(i, "유저 ID를 찾지 못했어요.")
        await i.response.defer(ephemeral=True)
        done = 0
        for uid in ids:
            m = i.guild.get_member(uid)
            if m and not can_act(i, m):
                continue
            try:
                await i.guild.ban(discord.Object(uid), reason=f"{i.user}: {사유}")
                add_case(i.guild.id, uid, i.user.id, "차단", 사유)
                done += 1
            except discord.HTTPException:
                pass
        await say(i, f"{len(ids)}명 중 **{done}명** 차단")
        await log(i.guild, "🔨 대량 차단", f"{done}명 · 처리: {i.user.mention}\n사유: {사유}", RED)


# ============================================================ 채널
class Chan(G):
    async def _purge(self, i, n, check=None):
        await i.response.defer(ephemeral=True)
        kw = {"check": check} if check else {}
        d = await i.channel.purge(limit=n, **kw)
        await say(i, f"메시지 **{len(d)}개** 삭제")
        await log(i.guild, "🧹 메시지 청소", f"{i.channel.mention} · {len(d)}개 · {i.user.mention}")

    @app_commands.command(name="청소", description="최근 메시지를 삭제합니다")
    async def purge(self, i: discord.Interaction, 개수: app_commands.Range[int, 1, 500]):
        await self._purge(i, 개수)

    @app_commands.command(name="유저청소", description="특정 유저의 최근 메시지만 삭제합니다")
    async def purge_user(self, i: discord.Interaction, 유저: discord.User, 범위: app_commands.Range[int, 1, 500] = 100):
        await self._purge(i, 범위, lambda m: m.author.id == 유저.id)

    @app_commands.command(name="봇청소", description="봇이 보낸 최근 메시지만 삭제합니다")
    async def purge_bot(self, i: discord.Interaction, 범위: app_commands.Range[int, 1, 500] = 100):
        await self._purge(i, 범위, lambda m: m.author.bot)

    @app_commands.command(name="잠금", description="이 채널에서 일반 유저의 채팅을 막습니다")
    async def lock(self, i: discord.Interaction, 사유: str = "채널 잠금"):
        await i.channel.set_permissions(i.guild.default_role, send_messages=False, reason=사유)
        await say(i, 사유, "🔒 채널이 잠겼습니다", YELLOW, eph=False)

    @app_commands.command(name="잠금해제", description="채널 잠금을 풉니다")
    async def unlock(self, i: discord.Interaction):
        await i.channel.set_permissions(i.guild.default_role, send_messages=None)
        await say(i, "", "🔓 채널 잠금이 해제되었습니다", GREEN, eph=False)

    @app_commands.command(name="슬로우", description="슬로우 모드를 설정합니다 (0 = 해제)")
    async def slow(self, i: discord.Interaction, 초: app_commands.Range[int, 0, 21600]):
        await i.channel.edit(slowmode_delay=초)
        await say(i, f"슬로우 모드: **{초}초**" if 초 else "슬로우 모드 해제")

    @app_commands.command(name="숨김", description="이 채널을 일반 유저에게 숨깁니다")
    async def hide(self, i: discord.Interaction):
        await i.channel.set_permissions(i.guild.default_role, view_channel=False)
        await say(i, "채널을 숨겼어요.")

    @app_commands.command(name="공개", description="숨긴 채널을 다시 보이게 합니다")
    async def show(self, i: discord.Interaction):
        await i.channel.set_permissions(i.guild.default_role, view_channel=None)
        await say(i, "채널을 공개했어요.")

    @app_commands.command(name="초기화", description="채널을 복제하고 원본을 삭제해 대화를 전부 비웁니다")
    async def nuke(self, i: discord.Interaction, 확인: Literal["초기화합니다"]):
        if get_ticket(i.channel.id):
            return await fail(i, "티켓 채널은 초기화할 수 없어요.")
        await i.response.defer(ephemeral=True)
        new = await i.channel.clone(reason=f"초기화 by {i.user}")
        await new.edit(position=i.channel.position)
        await i.channel.delete()
        await new.send(embed=emb("💥 채널이 초기화되었습니다", f"처리: {i.user.mention}", YELLOW))
        await log(i.guild, "💥 채널 초기화", f"{new.mention} · {i.user.mention}", YELLOW)

    @app_commands.command(name="서버잠금", description="모든 채팅 채널을 한 번에 잠급니다 (테러 대응)")
    async def lockdown(self, i: discord.Interaction, 사유: str = "서버 긴급 잠금"):
        await i.response.defer(ephemeral=True)
        ids = []
        for ch in i.guild.text_channels:
            ow = ch.overwrites_for(i.guild.default_role)
            if ow.send_messages is False or ow.view_channel is False:
                continue
            try:
                await ch.set_permissions(i.guild.default_role, overwrite=_with(ow, send_messages=False), reason=사유)
                ids.append(ch.id)
            except discord.HTTPException:
                pass
        setcfg(i.guild.id, "lockdown", json.dumps(ids))
        await say(i, f"채널 **{len(ids)}개** 잠금 · `/채널 서버잠금해제` 로 복구")
        await log(i.guild, "🚨 서버 잠금", f"{len(ids)}개 채널 · {i.user.mention}\n사유: {사유}", RED)

    @app_commands.command(name="서버잠금해제", description="서버잠금으로 잠근 채널을 복구합니다")
    async def unlockdown(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        ids = json.loads(cfg(i.guild.id, "lockdown", "[]"))
        n = 0
        for cid in ids:
            ch = i.guild.get_channel(cid)
            if ch:
                try:
                    await ch.set_permissions(i.guild.default_role, overwrite=_with(ch.overwrites_for(i.guild.default_role), send_messages=None))
                    n += 1
                except discord.HTTPException:
                    pass
        setcfg(i.guild.id, "lockdown", None)
        await say(i, f"채널 **{n}개** 잠금 해제")
        await log(i.guild, "✅ 서버 잠금 해제", f"{n}개 채널 · {i.user.mention}", GREEN)

    @app_commands.command(name="주제", description="채널 주제(설명)를 바꿉니다")
    async def topic(self, i: discord.Interaction, 내용: str):
        await i.channel.edit(topic=내용)
        await say(i, f"채널 주제 → {내용}")


def _with(ow: discord.PermissionOverwrite, **kw):
    ow.update(**kw)
    return ow


# ============================================================ 역할
class RoleG(G):
    def _ok(self, i, role):
        return role < i.guild.me.top_role and (i.user.id == i.guild.owner_id or role < i.user.top_role) and not role.managed

    @app_commands.command(name="지급", description="유저에게 역할을 줍니다")
    async def give(self, i: discord.Interaction, 유저: discord.Member, 역할: discord.Role):
        if not self._ok(i, 역할):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await 유저.add_roles(역할, reason=f"by {i.user}")
        await say(i, f"{유저.mention} ← {역할.mention}")

    @app_commands.command(name="회수", description="유저의 역할을 뺏습니다")
    async def take(self, i: discord.Interaction, 유저: discord.Member, 역할: discord.Role):
        if not self._ok(i, 역할):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await 유저.remove_roles(역할, reason=f"by {i.user}")
        await say(i, f"{유저.mention} ✕ {역할.mention}")

    async def _all(self, i, role, add):
        if not self._ok(i, role):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await i.response.defer(ephemeral=True)
        n = 0
        for m in i.guild.members:
            if m.bot or ((role in m.roles) == add):
                continue
            try:
                await (m.add_roles(role) if add else m.remove_roles(role))
                n += 1
            except discord.HTTPException:
                pass
        await say(i, f"{role.mention} · **{n}명** {'지급' if add else '회수'}")

    @app_commands.command(name="전체지급", description="모든 유저에게 역할을 줍니다")
    async def giveall(self, i: discord.Interaction, 역할: discord.Role):
        await self._all(i, 역할, True)

    @app_commands.command(name="전체회수", description="모든 유저에게서 역할을 뺏습니다")
    async def takeall(self, i: discord.Interaction, 역할: discord.Role):
        await self._all(i, 역할, False)

    @app_commands.command(name="기간지급", description="기간이 끝나면 자동 회수되는 역할 (예: 30d)")
    async def temp(self, i: discord.Interaction, 유저: discord.Member, 역할: discord.Role, 기간: str):
        sec = parse_dur(기간)
        if not sec:
            return await fail(i, "기간 형식: `12h` `30d` `7일`")
        if not self._ok(i, 역할):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await 유저.add_roles(역할, reason=f"기간지급 {기간} by {i.user}")
        until = int(time.time()) + sec
        q("INSERT INTO timers(gid,kind,uid,rid,until) VALUES(?,?,?,?,?)", i.guild.id, "unrole", 유저.id, 역할.id, until)
        await say(i, f"{유저.mention} ← {역할.mention} · 만료 {ts(until)}")

    @app_commands.command(name="생성", description="새 역할을 만듭니다 (색상 예: #ff0000)")
    async def create(self, i: discord.Interaction, 이름: str, 색상: Optional[str] = None, 분리표시: bool = False):
        try:
            col = discord.Colour.from_str(색상) if 색상 else discord.Colour.default()
        except ValueError:
            return await fail(i, "색상 형식: `#ff0000`")
        r = await i.guild.create_role(name=이름, colour=col, hoist=분리표시, reason=f"by {i.user}")
        await say(i, f"역할 생성: {r.mention}")

    @app_commands.command(name="삭제", description="역할을 삭제합니다")
    async def delete(self, i: discord.Interaction, 역할: discord.Role):
        if not self._ok(i, 역할):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await 역할.delete(reason=f"by {i.user}")
        await say(i, f"역할 **{역할.name}** 삭제")

    @app_commands.command(name="색상", description="역할 색상을 바꿉니다 (예: #00ffaa)")
    async def color(self, i: discord.Interaction, 역할: discord.Role, 색상: str):
        try:
            col = discord.Colour.from_str(색상)
        except ValueError:
            return await fail(i, "색상 형식: `#00ffaa`")
        if not self._ok(i, 역할):
            return await fail(i, "그 역할은 다룰 수 없어요. (역할 순위 확인)")
        await 역할.edit(colour=col)
        await say(i, f"{역할.mention} 색상 변경")

    @app_commands.command(name="자동역할", description="입장 시 자동으로 줄 역할 (비우면 해제)")
    async def auto(self, i: discord.Interaction, 역할: Optional[discord.Role] = None):
        setcfg(i.guild.id, "autorole", 역할.id if 역할 else None)
        await say(i, f"자동 역할 → {역할.mention}" if 역할 else "자동 역할 해제")

    @app_commands.command(name="버튼패널", description="버튼을 누르면 역할을 받는 패널을 보냅니다")
    async def panel(self, i: discord.Interaction, 제목: str, 역할1: discord.Role, 역할2: Optional[discord.Role] = None,
                    역할3: Optional[discord.Role] = None, 역할4: Optional[discord.Role] = None, 역할5: Optional[discord.Role] = None):
        roles = [r for r in (역할1, 역할2, 역할3, 역할4, 역할5) if r]
        if any(not self._ok(i, r) for r in roles):
            return await fail(i, "다룰 수 없는 역할이 있어요. (역할 순위 확인)")
        v = discord.ui.View(timeout=None)
        for r in roles:
            v.add_item(discord.ui.Button(label=r.name, style=discord.ButtonStyle.secondary, custom_id=f"rr:{r.id}"))
        await i.channel.send(embed=emb(제목, "버튼을 누르면 역할을 받고, 다시 누르면 해제돼요."), view=v)
        await say(i, "역할 패널을 보냈어요.")


# ============================================================ 자동관리
class Auto(G):
    async def _toggle(self, i, key, label, on):
        setcfg(i.guild.id, key, 1 if on else None)
        await say(i, f"{label}: **{'켜짐' if on else '꺼짐'}**")

    @app_commands.command(name="링크차단", description="일반 유저의 링크 전송을 막습니다")
    async def link(self, i: discord.Interaction, 켜기: bool):
        await self._toggle(i, "am_link", "링크 차단", 켜기)

    @app_commands.command(name="초대차단", description="다른 서버 초대 링크(홍보)를 막습니다")
    async def invite(self, i: discord.Interaction, 켜기: bool):
        await self._toggle(i, "am_inv", "초대 링크 차단", 켜기)

    @app_commands.command(name="도배차단", description="5초에 5개 이상 도배 시 1분 타임아웃")
    async def spam(self, i: discord.Interaction, 켜기: bool):
        await self._toggle(i, "am_spam", "도배 차단", 켜기)

    @app_commands.command(name="금칙어추가", description="금칙어를 추가합니다")
    async def addword(self, i: discord.Interaction, 단어: str):
        q("REPLACE INTO lists VALUES(?,?,?)", i.guild.id, "word", 단어.lower())
        await say(i, f"금칙어 추가: ||{단어}||")

    @app_commands.command(name="금칙어삭제", description="금칙어를 삭제합니다")
    async def delword(self, i: discord.Interaction, 단어: str):
        c = q("DELETE FROM lists WHERE gid=? AND kind='word' AND val=?", i.guild.id, 단어.lower()).rowcount
        await (say(i, "금칙어 삭제") if c else fail(i, "등록되지 않은 단어예요."))

    @app_commands.command(name="금칙어목록", description="금칙어 목록을 봅니다")
    async def words(self, i: discord.Interaction):
        w = get_list(i.guild.id, "word")
        await say(i, ", ".join(f"||{x}||" for x in w) or "없음", f"🚫 금칙어 {len(w)}개", BLUE)

    @app_commands.command(name="멘션제한", description="한 메시지의 최대 멘션 수 (0 = 끄기)")
    async def mention(self, i: discord.Interaction, 개수: app_commands.Range[int, 0, 50]):
        setcfg(i.guild.id, "am_ment", 개수 or None)
        await say(i, f"멘션 제한: **{개수}개**" if 개수 else "멘션 제한 꺼짐")

    @app_commands.command(name="신규계정", description="가입한 지 N일이 안 된 계정은 입장 시 추방 (0 = 끄기)")
    async def age(self, i: discord.Interaction, 일수: app_commands.Range[int, 0, 365]):
        setcfg(i.guild.id, "am_age", 일수 or None)
        await say(i, f"계정 생성 **{일수}일** 미만 입장 차단" if 일수 else "신규 계정 차단 꺼짐")

    @app_commands.command(name="레이드방지", description="10초 안에 N명 이상 입장 시 2분간 신규 입장자를 추방 (0 = 끄기)")
    async def raid(self, i: discord.Interaction, 인원: app_commands.Range[int, 0, 50]):
        setcfg(i.guild.id, "am_raid", 인원 or None)
        await say(i, f"레이드 방지: 10초 내 **{인원}명**" if 인원 else "레이드 방지 꺼짐")

    @app_commands.command(name="면제역할", description="자동관리에서 제외할 역할을 추가/해제합니다")
    async def exrole(self, i: discord.Interaction, 역할: discord.Role):
        if in_list(i.guild.id, "exrole", 역할.id):
            q("DELETE FROM lists WHERE gid=? AND kind='exrole' AND val=?", i.guild.id, str(역할.id))
            return await say(i, f"{역할.mention} 면제 해제")
        q("REPLACE INTO lists VALUES(?,?,?)", i.guild.id, "exrole", str(역할.id))
        await say(i, f"{역할.mention} 자동관리 면제")

    @app_commands.command(name="면제채널", description="자동관리에서 제외할 채널을 추가/해제합니다")
    async def exch(self, i: discord.Interaction, 채널: discord.TextChannel):
        if in_list(i.guild.id, "exch", 채널.id):
            q("DELETE FROM lists WHERE gid=? AND kind='exch' AND val=?", i.guild.id, str(채널.id))
            return await say(i, f"{채널.mention} 면제 해제")
        q("REPLACE INTO lists VALUES(?,?,?)", i.guild.id, "exch", str(채널.id))
        await say(i, f"{채널.mention} 자동관리 면제")

    @app_commands.command(name="상태", description="자동관리 설정 현황")
    async def status(self, i: discord.Interaction):
        g = i.guild.id
        o = lambda k: "🟢 켜짐" if cfg(g, k) else "⚫ 꺼짐"
        txt = (f"링크 차단: {o('am_link')}\n초대 링크 차단: {o('am_inv')}\n도배 차단: {o('am_spam')}\n"
               f"멘션 제한: {cfg(g, 'am_ment') or '꺼짐'}\n신규 계정 차단: {cfg(g, 'am_age', '꺼짐')}일\n"
               f"레이드 방지: {cfg(g, 'am_raid', '꺼짐')}명\n금칙어: {len(get_list(g, 'word'))}개\n"
               f"면제 역할: {' '.join(f'<@&{x}>' for x in get_list(g, 'exrole')) or '없음'}\n"
               f"면제 채널: {' '.join(f'<#{x}>' for x in get_list(g, 'exch')) or '없음'}")
        await say(i, txt, "🛡️ 자동관리 상태", BLUE)


# ============================================================ 설정
class ReviewModal(discord.ui.Modal, title="⭐ 구매 후기 작성"):
    stars = discord.ui.TextInput(label="별점 (1~5)", max_length=1, placeholder="5")
    body = discord.ui.TextInput(label="후기 내용", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        if not self.stars.value.isdigit() or not 1 <= int(self.stars.value) <= 5:
            return await fail(i, "별점은 1~5 숫자로 적어주세요.")
        ch = i.guild.get_channel(cfgint(i.guild.id, "reviewch"))
        if not ch:
            return await fail(i, "후기 채널이 설정되지 않았어요.")
        n = int(self.stars.value)
        q("INSERT INTO reviews(gid,uid,stars,text,ts) VALUES(?,?,?,?,?)", i.guild.id, i.user.id, n, self.body.value, int(time.time()))
        e = emb(f"{'⭐' * n}{'☆' * (5 - n)}", self.body.value, YELLOW)
        e.set_author(name=str(i.user.display_name), icon_url=i.user.display_avatar.url)
        await ch.send(embed=e)
        await say(i, "후기 감사합니다! 🙏")


class Setup(G):
    @app_commands.command(name="로그채널", description="제재·티켓·메시지 로그가 기록될 채널")
    async def logch(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        setcfg(i.guild.id, "log", 채널.id if 채널 else None)
        await say(i, f"로그 채널 → {채널.mention}" if 채널 else "로그 꺼짐")

    @app_commands.command(name="환영", description="입장 메시지 설정 ({user} {server} {count} 사용 가능)")
    async def welcome(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None,
                      메시지: str = "{user} 님, **{server}** 에 오신 걸 환영합니다! ({count}번째 멤버)"):
        setcfg(i.guild.id, "welch", 채널.id if 채널 else None)
        setcfg(i.guild.id, "welmsg", 메시지)
        await say(i, f"환영 메시지 → {채널.mention}\n{메시지}" if 채널 else "환영 메시지 꺼짐")

    @app_commands.command(name="퇴장", description="퇴장 메시지 설정 ({user} {server} {count} 사용 가능)")
    async def bye(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None,
                  메시지: str = "**{user}** 님이 서버를 떠났습니다."):
        setcfg(i.guild.id, "byech", 채널.id if 채널 else None)
        setcfg(i.guild.id, "byemsg", 메시지)
        await say(i, f"퇴장 메시지 → {채널.mention}\n{메시지}" if 채널 else "퇴장 메시지 꺼짐")

    @app_commands.command(name="인증패널", description="버튼을 누르면 인증 역할을 받는 패널을 보냅니다")
    async def verify(self, i: discord.Interaction, 역할: discord.Role, 채널: Optional[discord.TextChannel] = None):
        if 역할 >= i.guild.me.top_role:
            return await fail(i, "봇 역할보다 낮은 역할만 지정할 수 있어요.")
        setcfg(i.guild.id, "verifyrole", 역할.id)
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="인증하기", emoji="✅", style=discord.ButtonStyle.success, custom_id="vf"))
        await (채널 or i.channel).send(embed=emb("✅ 서버 인증", "아래 버튼을 눌러 인증하고 서버를 이용해 주세요."), view=v)
        await say(i, f"인증 패널 전송 · 인증 역할 {역할.mention}")

    @app_commands.command(name="구매자역할", description="주문 완료 시 자동 지급할 구매자 역할")
    async def buyer(self, i: discord.Interaction, 역할: Optional[discord.Role] = None):
        setcfg(i.guild.id, "buyerrole", 역할.id if 역할 else None)
        await say(i, f"구매자 역할 → {역할.mention}" if 역할 else "구매자 역할 해제")

    @app_commands.command(name="관리자역할", description="봇 관리 명령어와 티켓을 담당할 역할")
    async def staff(self, i: discord.Interaction, 역할: Optional[discord.Role] = None):
        if not i.user.guild_permissions.manage_guild:
            return await fail(i, "서버 관리 권한이 있는 사람만 바꿀 수 있어요.")
        setcfg(i.guild.id, "staffrole", 역할.id if 역할 else None)
        await say(i, f"관리자 역할 → {역할.mention}" if 역할 else "관리자 역할 해제")

    @app_commands.command(name="후기채널", description="후기가 올라갈 채널")
    async def reviewch(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        setcfg(i.guild.id, "reviewch", 채널.id if 채널 else None)
        await say(i, f"후기 채널 → {채널.mention}" if 채널 else "후기 채널 해제")

    @app_commands.command(name="후기패널", description="구매자가 후기를 작성하는 버튼 패널을 보냅니다")
    async def reviewpanel(self, i: discord.Interaction):
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="후기 작성", emoji="⭐", style=discord.ButtonStyle.primary, custom_id="rv"))
        await i.channel.send(embed=emb("⭐ 구매 후기", "구매가 완료된 분은 아래 버튼으로 후기를 남겨주세요!"), view=v)
        await say(i, "후기 패널을 보냈어요.")

    @app_commands.command(name="보기", description="현재 서버 설정을 한눈에 봅니다")
    async def view(self, i: discord.Interaction):
        g = i.guild.id
        c = lambda k: f"<#{cfg(g, k)}>" if cfg(g, k) else "미설정"
        r = lambda k: f"<@&{cfg(g, k)}>" if cfg(g, k) else "미설정"
        txt = (f"로그 채널: {c('log')}\n환영 채널: {c('welch')}\n퇴장 채널: {c('byech')}\n후기 채널: {c('reviewch')}\n"
               f"티켓 카테고리: {c('tkcat')}\n티켓 1인 제한: {cfgint(g, 'tklimit', 1)}개\n"
               f"관리자 역할: {r('staffrole')}\n구매자 역할: {r('buyerrole')}\n인증 역할: {r('verifyrole')}\n"
               f"자동 역할: {r('autorole')}\n영업 상태: {cfg(g, 'shopstate', '영업중')}")
        await say(i, txt, "⚙️ 서버 설정", BLUE)

    @app_commands.command(name="백업", description="봇 데이터(DB) 파일을 받습니다")
    async def backup(self, i: discord.Interaction):
        if not i.user.guild_permissions.administrator:
            return await fail(i, "서버 관리자(Administrator)만 받을 수 있어요.")
        db.commit()
        await i.response.send_message(file=discord.File("shop.db"), ephemeral=True)


# ============================================================ 샵
ORDER_ICON = {"대기": "🕐", "결제확인": "💳", "완료": "✅", "취소": "❌"}


def price_embed(g: discord.Guild):
    rows = q("SELECT * FROM products WHERE gid=? ORDER BY id", g.id).fetchall()
    state = cfg(g.id, "shopstate", "영업중")
    e = emb(f"🛍️ {g.name} 가격표", f"현재 상태: **{state}**\n구매는 티켓으로 문의해 주세요.")
    for r in rows[:25]:
        stock = "품절" if r["stock"] == 0 else ("재고 충분" if r["stock"] < 0 else f"재고 {r['stock']}개")
        e.add_field(name=f"`{r['id']}` {r['name']}", value=f"**{won(r['price'])}** · {stock}\n{r['descr'] or ''}", inline=False)
    if not rows:
        e.description += "\n\n등록된 상품이 없어요."
    return e


class Shop(G):
    @app_commands.command(name="상품추가", description="상품을 등록합니다 (재고 -1 = 무제한)")
    async def add(self, i: discord.Interaction, 이름: str, 가격: app_commands.Range[int, 0], 재고: int = -1, 설명: str = ""):
        c = q("INSERT INTO products(gid,name,price,stock,descr) VALUES(?,?,?,?,?)", i.guild.id, 이름, 가격, 재고, 설명)
        await say(i, f"`{c.lastrowid}` **{이름}** · {won(가격)} 등록")

    @app_commands.command(name="상품삭제", description="상품 번호로 상품을 삭제합니다")
    async def delete(self, i: discord.Interaction, 번호: int):
        c = q("DELETE FROM products WHERE gid=? AND id=?", i.guild.id, 번호).rowcount
        await (say(i, f"상품 `{번호}` 삭제") if c else fail(i, "없는 상품 번호예요."))

    @app_commands.command(name="상품수정", description="상품 이름·가격·설명을 수정합니다")
    async def edit(self, i: discord.Interaction, 번호: int, 이름: Optional[str] = None,
                   가격: Optional[app_commands.Range[int, 0]] = None, 설명: Optional[str] = None):
        p = q("SELECT * FROM products WHERE gid=? AND id=?", i.guild.id, 번호).fetchone()
        if not p:
            return await fail(i, "없는 상품 번호예요.")
        q("UPDATE products SET name=?, price=?, descr=? WHERE id=?", 이름 or p["name"], p["price"] if 가격 is None else 가격,
          p["descr"] if 설명 is None else 설명, 번호)
        await say(i, f"상품 `{번호}` 수정 완료")

    @app_commands.command(name="재고", description="상품 재고를 설정합니다 (-1 = 무제한, 0 = 품절)")
    async def stock(self, i: discord.Interaction, 번호: int, 수량: int):
        c = q("UPDATE products SET stock=? WHERE gid=? AND id=?", 수량, i.guild.id, 번호).rowcount
        await (say(i, f"상품 `{번호}` 재고 → {수량}") if c else fail(i, "없는 상품 번호예요."))

    @app_commands.command(name="가격표", description="가격표를 채널에 게시합니다")
    async def price(self, i: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
        await (채널 or i.channel).send(embed=price_embed(i.guild))
        await say(i, "가격표를 게시했어요.")

    @app_commands.command(name="주문생성", description="유저의 주문을 기록합니다 (재고 자동 차감)")
    async def order(self, i: discord.Interaction, 유저: discord.Member, 상품번호: int, 수량: app_commands.Range[int, 1, 1000] = 1):
        g = i.guild.id
        if in_list(g, "shopban", 유저.id):
            return await fail(i, "블랙리스트에 등록된 유저예요.")
        p = q("SELECT * FROM products WHERE gid=? AND id=?", g, 상품번호).fetchone()
        if not p:
            return await fail(i, "없는 상품 번호예요.")
        if 0 <= p["stock"] < 수량:
            return await fail(i, f"재고가 부족해요. (남은 재고 {p['stock']}개)")
        if p["stock"] > 0:
            q("UPDATE products SET stock=stock-? WHERE id=?", 수량, 상품번호)
        total = p["price"] * 수량
        c = q("INSERT INTO orders(gid,uid,pid,pname,qty,total,status,staff,ts) VALUES(?,?,?,?,?,?,?,?,?)",
              g, 유저.id, 상품번호, p["name"], 수량, total, "대기", i.user.id, int(time.time()))
        await say(i, f"주문번호 `{c.lastrowid}`\n구매자: {유저.mention}\n상품: **{p['name']}** × {수량}\n금액: **{won(total)}**\n상태: 🕐 대기",
                  "🧾 주문 생성", BLUE, eph=False)
        await log(i.guild, "🧾 주문 생성", f"`#{c.lastrowid}` {유저.mention} · {p['name']} × {수량} · {won(total)} · {i.user.mention}")

    @app_commands.command(name="주문상태", description="주문 상태를 바꿉니다 (완료 시 구매자 역할 지급, 취소 시 재고 복구)")
    async def status(self, i: discord.Interaction, 주문번호: int, 상태: Literal["대기", "결제확인", "완료", "취소"]):
        o = q("SELECT * FROM orders WHERE gid=? AND id=?", i.guild.id, 주문번호).fetchone()
        if not o:
            return await fail(i, "없는 주문번호예요.")
        if o["status"] == 상태:
            return await fail(i, "이미 그 상태예요.")
        if 상태 == "취소" or o["status"] == "취소":
            sign = 1 if 상태 == "취소" else -1
            q("UPDATE products SET stock=stock+? WHERE id=? AND stock>=0", sign * o["qty"], o["pid"])
        q("UPDATE orders SET status=?, staff=? WHERE id=?", 상태, i.user.id, 주문번호)
        m = i.guild.get_member(o["uid"])
        if m and 상태 == "완료":
            role = i.guild.get_role(cfgint(i.guild.id, "buyerrole"))
            try:
                if role:
                    await m.add_roles(role, reason="구매 완료")
                await m.send(embed=emb("✅ 주문이 완료되었습니다", f"**{o['pname']}** × {o['qty']}\n이용해 주셔서 감사합니다!", GREEN))
            except discord.HTTPException:
                pass
        await say(i, f"주문 `#{주문번호}` → {ORDER_ICON[상태]} **{상태}**", eph=False)
        await log(i.guild, "🧾 주문 상태 변경", f"`#{주문번호}` <@{o['uid']}> · {o['pname']} → {상태} · {i.user.mention}")

    @app_commands.command(name="주문목록", description="최근 주문 목록 (상태로 필터 가능)")
    async def orders(self, i: discord.Interaction, 상태: Optional[Literal["대기", "결제확인", "완료", "취소"]] = None):
        if 상태:
            rows = q("SELECT * FROM orders WHERE gid=? AND status=? ORDER BY id DESC LIMIT 20", i.guild.id, 상태).fetchall()
        else:
            rows = q("SELECT * FROM orders WHERE gid=? ORDER BY id DESC LIMIT 20", i.guild.id).fetchall()
        txt = "\n".join(f"`#{r['id']}` {ORDER_ICON[r['status']]} <@{r['uid']}> · {r['pname']} × {r['qty']} · {won(r['total'])} · {ts(r['ts'], 'd')}" for r in rows)
        await say(i, txt or "주문이 없어요.", f"🧾 주문 목록{' · ' + 상태 if 상태 else ''}", BLUE)

    @app_commands.command(name="구매기록", description="유저의 구매 기록을 봅니다")
    async def history(self, i: discord.Interaction, 유저: discord.User):
        rows = q("SELECT * FROM orders WHERE gid=? AND uid=? ORDER BY id DESC LIMIT 20", i.guild.id, 유저.id).fetchall()
        done = [r for r in rows if r["status"] == "완료"]
        txt = f"완료 {len(done)}건 · 합계 {won(sum(r['total'] for r in done))}\n\n"
        txt += "\n".join(f"`#{r['id']}` {ORDER_ICON[r['status']]} {r['pname']} × {r['qty']} · {won(r['total'])} · {ts(r['ts'], 'd')}" for r in rows)
        await say(i, txt, f"🧾 {유저} 구매 기록", BLUE)

    @app_commands.command(name="영업", description="영업 상태를 바꾸고 공지합니다")
    async def state(self, i: discord.Interaction, 상태: Literal["영업중", "휴무", "점검중"], 공지채널: Optional[discord.TextChannel] = None, 안내: str = ""):
        setcfg(i.guild.id, "shopstate", 상태)
        icon, col = {"영업중": ("🟢", GREEN), "휴무": ("🔴", RED), "점검중": ("🟡", YELLOW)}[상태]
        if 공지채널:
            await 공지채널.send(embed=emb(f"{icon} 현재 {상태}입니다", 안내, col))
        await say(i, f"영업 상태 → {icon} **{상태}**")

    @app_commands.command(name="매출", description="완료된 주문 기준 매출 통계")
    async def sales(self, i: discord.Interaction):
        g, now = i.guild.id, int(time.time())
        s = lambda since: q("SELECT COUNT(*) c, COALESCE(SUM(total),0) t FROM orders WHERE gid=? AND status='완료' AND ts>=?", g, since).fetchone()
        d, w, m, a = s(now - 86400), s(now - 7 * 86400), s(now - 30 * 86400), s(0)
        top = q("SELECT pname, SUM(qty) n, SUM(total) t FROM orders WHERE gid=? AND status='완료' GROUP BY pname ORDER BY t DESC LIMIT 5", g).fetchall()
        txt = (f"오늘(24h): **{won(d['t'])}** · {d['c']}건\n7일: **{won(w['t'])}** · {w['c']}건\n"
               f"30일: **{won(m['t'])}** · {m['c']}건\n전체: **{won(a['t'])}** · {a['c']}건\n\n**인기 상품**\n")
        txt += "\n".join(f"{r['pname']} · {r['n']}개 · {won(r['t'])}" for r in top) or "없음"
        await say(i, txt, "📈 매출 통계", GREEN)

    @app_commands.command(name="블랙추가", description="거래 블랙리스트에 유저를 추가합니다")
    async def black(self, i: discord.Interaction, 유저: discord.User, 사유: str = "사유 없음"):
        q("REPLACE INTO lists VALUES(?,?,?)", i.guild.id, "shopban", str(유저.id))
        add_case(i.guild.id, 유저.id, i.user.id, "거래 블랙", 사유)
        await say(i, f"{유저.mention} 블랙리스트 등록 · {사유}")
        await log(i.guild, "⛔ 거래 블랙리스트", f"{유저.mention} · {사유} · {i.user.mention}", RED)

    @app_commands.command(name="블랙해제", description="거래 블랙리스트에서 유저를 뺍니다")
    async def unblack(self, i: discord.Interaction, 유저: discord.User):
        c = q("DELETE FROM lists WHERE gid=? AND kind='shopban' AND val=?", i.guild.id, str(유저.id)).rowcount
        await (say(i, f"{유저.mention} 블랙리스트 해제") if c else fail(i, "블랙리스트에 없는 유저예요."))

    @app_commands.command(name="블랙목록", description="거래 블랙리스트를 봅니다")
    async def blacklist(self, i: discord.Interaction):
        ids = get_list(i.guild.id, "shopban")
        await say(i, "\n".join(f"<@{x}> (`{x}`)" for x in ids) or "없음", f"⛔ 블랙리스트 {len(ids)}명", RED)


# ============================================================ 공지
def gw_view(disabled=False):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="참여하기", emoji="🎉", style=discord.ButtonStyle.success, custom_id="gw:join", disabled=disabled))
    return v


async def gw_finish(row, reroll=False):
    g = bot.get_guild(row["gid"])
    ch = g.get_channel(row["ch"]) if g else None
    q("UPDATE gws SET ended=1 WHERE msg=?", row["msg"])
    if not ch:
        return
    ent = json.loads(row["entrants"])
    win = random.sample(ent, min(row["winners"], len(ent)))
    txt = " ".join(f"<@{u}>" for u in win) or "참여자가 없어요 😢"
    try:
        msg = await ch.fetch_message(row["msg"])
        if not reroll:
            await msg.edit(embed=emb(f"🎉 {row['prize']} (종료)", f"당첨자: {txt}\n참여 {len(ent)}명", GRAY), view=gw_view(True))
        await msg.reply(embed=emb("🎊 재추첨 결과" if reroll else "🎊 추첨 결과", f"**{row['prize']}**\n당첨자: {txt}", GREEN),
                        content=txt if win else None, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass


class Notice(G):
    @app_commands.command(name="보내기", description="임베드 공지를 보냅니다 (줄바꿈은 \\n)")
    async def send(self, i: discord.Interaction, 채널: discord.TextChannel, 제목: str, 내용: str,
                   멘션역할: Optional[discord.Role] = None, 이미지주소: Optional[str] = None):
        e = emb(제목, 내용.replace("\\n", "\n"))
        if 이미지주소 and 이미지주소.startswith("http"):
            e.set_image(url=이미지주소)
        m = await 채널.send(content=멘션역할.mention if 멘션역할 else None, embed=e, allowed_mentions=discord.AllowedMentions(roles=True))
        await say(i, f"공지 전송 → {m.jump_url}")

    @app_commands.command(name="수정", description="봇이 보낸 공지의 내용을 수정합니다")
    async def edit(self, i: discord.Interaction, 채널: discord.TextChannel, 메시지아이디: str, 내용: str, 제목: Optional[str] = None):
        try:
            m = await 채널.fetch_message(int(메시지아이디))
        except (ValueError, discord.HTTPException):
            return await fail(i, "메시지를 찾지 못했어요.")
        if m.author.id != bot.user.id or not m.embeds:
            return await fail(i, "봇이 보낸 임베드 공지만 수정할 수 있어요.")
        e = m.embeds[0]
        e.description = 내용.replace("\\n", "\n")
        if 제목:
            e.title = 제목
        await m.edit(embed=e)
        await say(i, "공지를 수정했어요.")

    @app_commands.command(name="디엠", description="유저에게 봇 DM으로 안내를 보냅니다")
    async def dm(self, i: discord.Interaction, 유저: discord.Member, 내용: str):
        try:
            await 유저.send(embed=emb(f"📩 {i.guild.name} 안내", 내용.replace("\\n", "\n")))
        except discord.HTTPException:
            return await fail(i, "DM을 보낼 수 없어요. (상대가 DM을 막아둠)")
        await say(i, f"{유저.mention} 에게 DM 전송")
        await log(i.guild, "📩 DM 전송", f"{i.user.mention} → {유저.mention}\n{내용}")

    @app_commands.command(name="예약", description="일정 시간 뒤에 공지를 보냅니다 (예: 30m, 2h, 1d)")
    async def schedule(self, i: discord.Interaction, 채널: discord.TextChannel, 시간: str, 제목: str, 내용: str):
        sec = parse_dur(시간)
        if not sec:
            return await fail(i, "시간 형식: `30m` `2h` `1d`")
        until = int(time.time()) + sec
        c = q("INSERT INTO timers(gid,kind,ch,text,until) VALUES(?,?,?,?,?)", i.guild.id, "notice", 채널.id,
              json.dumps([제목, 내용.replace("\\n", "\n")]), until)
        await say(i, f"예약번호 `{c.lastrowid}` · {채널.mention} · {ts(until)}")

    @app_commands.command(name="예약목록", description="예약된 공지 목록")
    async def schedules(self, i: discord.Interaction):
        rows = q("SELECT * FROM timers WHERE gid=? AND kind='notice' ORDER BY until", i.guild.id).fetchall()
        txt = "\n".join(f"`{r['id']}` <#{r['ch']}> · {ts(r['until'])} · {json.loads(r['text'])[0]}" for r in rows)
        await say(i, txt or "예약된 공지가 없어요.", "🗓️ 예약 공지", BLUE)

    @app_commands.command(name="예약취소", description="예약 공지를 취소합니다")
    async def unschedule(self, i: discord.Interaction, 예약번호: int):
        c = q("DELETE FROM timers WHERE gid=? AND kind='notice' AND id=?", i.guild.id, 예약번호).rowcount
        await (say(i, f"예약 `{예약번호}` 취소") if c else fail(i, "없는 예약번호예요."))

    @app_commands.command(name="투표", description="투표를 만듭니다 (선택지는 쉼표로 구분, 최대 10개)")
    async def poll(self, i: discord.Interaction, 질문: str, 선택지: str = "찬성,반대"):
        opts = [o.strip() for o in 선택지.split(",") if o.strip()][:10]
        if len(opts) < 2:
            return await fail(i, "선택지를 2개 이상 적어주세요.")
        nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        await i.response.send_message(embed=emb(f"📊 {질문}", "\n".join(f"{nums[k]} {o}" for k, o in enumerate(opts))))
        m = await i.original_response()
        for k in range(len(opts)):
            await m.add_reaction(nums[k])

    @app_commands.command(name="추첨", description="버튼 참여형 추첨 이벤트를 시작합니다")
    async def giveaway(self, i: discord.Interaction, 상품: str, 기간: str, 당첨인원: app_commands.Range[int, 1, 20] = 1):
        sec = parse_dur(기간)
        if not sec:
            return await fail(i, "기간 형식: `30m` `2h` `1d`")
        until = int(time.time()) + sec
        await i.response.send_message(embed=emb(f"🎉 {상품}", f"버튼을 눌러 참여하세요!\n당첨 인원: **{당첨인원}명**\n종료: {ts(until, 'R')}\n참여 0명", GREEN), view=gw_view())
        m = await i.original_response()
        q("INSERT INTO gws VALUES(?,?,?,?,?,?,?,?)", m.id, i.guild.id, i.channel.id, 상품, 당첨인원, until, 0, "[]")

    @app_commands.command(name="추첨종료", description="진행 중인 추첨을 지금 바로 끝냅니다")
    async def gw_end(self, i: discord.Interaction, 메시지아이디: str):
        row = q("SELECT * FROM gws WHERE gid=? AND msg=? AND ended=0", i.guild.id, 메시지아이디).fetchone()
        if not row:
            return await fail(i, "진행 중인 추첨이 아니에요.")
        await say(i, "추첨을 종료했어요.")
        await gw_finish(row)

    @app_commands.command(name="재추첨", description="끝난 추첨의 당첨자를 다시 뽑습니다")
    async def gw_reroll(self, i: discord.Interaction, 메시지아이디: str):
        row = q("SELECT * FROM gws WHERE gid=? AND msg=? AND ended=1", i.guild.id, 메시지아이디).fetchone()
        if not row:
            return await fail(i, "종료된 추첨이 아니에요.")
        await say(i, "재추첨했어요.")
        await gw_finish(row, reroll=True)


# ============================================================ 정보 (누구나 사용)
class Info(app_commands.Group):
    @app_commands.command(name="유저", description="유저 정보를 봅니다")
    async def user(self, i: discord.Interaction, 유저: Optional[discord.Member] = None):
        m = 유저 or i.user
        e = emb(f"👤 {m}", f"ID: `{m.id}`\n계정 생성: {ts(m.created_at.timestamp(), 'D')}\n서버 입장: {ts(m.joined_at.timestamp(), 'D')}\n"
                           f"역할: {' '.join(r.mention for r in m.roles[1:][-15:]) or '없음'}")
        if is_staff(i.user):
            w = q("SELECT COUNT(*) c FROM warns WHERE gid=? AND uid=?", i.guild.id, m.id).fetchone()["c"]
            o = q("SELECT COUNT(*) c FROM orders WHERE gid=? AND uid=? AND status='완료'", i.guild.id, m.id).fetchone()["c"]
            e.description += f"\n\n경고 {w}회 · 구매 {o}건 · 블랙리스트 {'O' if in_list(i.guild.id, 'shopban', m.id) else 'X'}"
        e.set_thumbnail(url=m.display_avatar.url)
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="서버", description="서버 정보를 봅니다")
    async def server(self, i: discord.Interaction):
        g = i.guild
        e = emb(f"🏠 {g.name}", f"멤버: **{g.member_count}명**\n채널: {len(g.channels)}개 · 역할: {len(g.roles)}개\n"
                               f"부스트: {g.premium_subscription_count}개 (Lv.{g.premium_tier})\n개설: {ts(g.created_at.timestamp(), 'D')}\n"
                               f"영업 상태: **{cfg(g.id, 'shopstate', '영업중')}**")
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="아바타", description="프로필 사진을 크게 봅니다")
    async def avatar(self, i: discord.Interaction, 유저: Optional[discord.Member] = None):
        m = 유저 or i.user
        e = emb(f"🖼️ {m.display_name}")
        e.set_image(url=m.display_avatar.url)
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="핑", description="봇 응답 속도")
    async def ping(self, i: discord.Interaction):
        await say(i, f"{round(bot.latency * 1000)}ms", "🏓 퐁!", BLUE)

    @app_commands.command(name="가격표", description="가격표를 봅니다")
    async def price(self, i: discord.Interaction):
        await i.response.send_message(embed=price_embed(i.guild), ephemeral=True)

    @app_commands.command(name="도움말", description="봇 명령어 전체 목록")
    async def help(self, i: discord.Interaction):
        e = emb(f"📖 {SHOP_NAME} 관리 봇 명령어", "`/그룹 명령어` 형태로 사용해요.")
        for grp in bot.tree.get_commands():
            if isinstance(grp, app_commands.Group):
                e.add_field(name=f"/{grp.name} ({len(grp.commands)})", value=" ".join(f"`{c.name}`" for c in grp.commands)[:1024], inline=False)
        await i.response.send_message(embed=e, ephemeral=True)


for cls, name, desc in [(Ticket, "티켓", "티켓 관리"), (Mod, "제재", "유저 제재"), (Chan, "채널", "채널 관리"), (RoleG, "역할", "역할 관리"),
                        (Auto, "자동관리", "자동 관리(오토모드)"), (Setup, "설정", "서버 설정"), (Shop, "샵", "상품·주문 관리"), (Notice, "공지", "공지·이벤트")]:
    bot.tree.add_command(cls(name=name, description=desc, guild_only=True, default_permissions=STAFF_PERM))
bot.tree.add_command(Info(name="정보", description="정보 보기", guild_only=True))


# ============================================================ 버튼 처리 (재시작해도 계속 작동)
@bot.event
async def on_interaction(i: discord.Interaction):
    if i.type != discord.InteractionType.component or not i.guild:
        return
    cid = (i.data or {}).get("custom_id", "")
    try:
        if cid.startswith("tk:open:"):
            kind = cid.split(":")[2]
            if kind in TICKET_KINDS:
                await i.response.send_modal(TicketModal(kind))
        elif cid == "tk:close":
            await tk_close(i)
        elif cid == "tk:claim":
            await tk_claim(i)
        elif cid == "tk:reopen":
            await tk_reopen(i)
        elif cid == "tk:save":
            await tk_save(i)
        elif cid == "tk:del":
            await tk_delete(i)
        elif cid == "vf":
            role = i.guild.get_role(cfgint(i.guild.id, "verifyrole"))
            if not role:
                return await fail(i, "인증 역할이 설정되지 않았어요.")
            if role in i.user.roles:
                return await say(i, "이미 인증되어 있어요.")
            await i.user.add_roles(role, reason="버튼 인증")
            await say(i, "인증이 완료되었습니다!")
        elif cid == "rv":
            ok = q("SELECT 1 FROM orders WHERE gid=? AND uid=? AND status='완료'", i.guild.id, i.user.id).fetchone()
            if not ok:
                return await fail(i, "구매가 완료된 분만 후기를 쓸 수 있어요.")
            await i.response.send_modal(ReviewModal())
        elif cid.startswith("rr:"):
            role = i.guild.get_role(int(cid[3:]))
            if not role:
                return await fail(i, "삭제된 역할이에요.")
            if role in i.user.roles:
                await i.user.remove_roles(role)
                await say(i, f"{role.mention} 역할을 해제했어요.")
            else:
                await i.user.add_roles(role)
                await say(i, f"{role.mention} 역할을 받았어요.")
        elif cid == "gw:join":
            row = q("SELECT * FROM gws WHERE msg=? AND ended=0", i.message.id).fetchone()
            if not row:
                return await fail(i, "이미 끝난 추첨이에요.")
            ent = json.loads(row["entrants"])
            joined = i.user.id not in ent
            ent.append(i.user.id) if joined else ent.remove(i.user.id)
            q("UPDATE gws SET entrants=? WHERE msg=?", json.dumps(ent), i.message.id)
            e = i.message.embeds[0]
            e.description = re.sub(r"참여 \d+명", f"참여 {len(ent)}명", e.description)
            await i.response.edit_message(embed=e)
            await i.followup.send("🎉 참여 완료!" if joined else "참여를 취소했어요.", ephemeral=True)
    except discord.Forbidden:
        await fail(i, "봇 권한이 부족해요. (봇 역할을 위로 올려주세요)")


@bot.tree.error
async def on_app_error(i: discord.Interaction, err: app_commands.AppCommandError):
    orig = getattr(err, "original", err)
    if isinstance(err, app_commands.CheckFailure):
        return
    if isinstance(orig, discord.Forbidden):
        msg = "봇 권한이 부족해요. 봇 역할을 대상보다 위로 올리고 권한을 확인해 주세요."
    else:
        msg = f"오류가 발생했어요: `{type(orig).__name__}: {orig}`"
        print("[오류]", repr(orig))
    try:
        await fail(i, msg)
    except discord.HTTPException:
        pass


# ============================================================ 자동관리 · 로그 이벤트
INVITE_RE = re.compile(r"(discord\.gg/|discord(app)?\.com/invite/)\S+", re.I)
LINK_RE = re.compile(r"https?://\S+", re.I)
spam_track = defaultdict(lambda: deque(maxlen=6))
join_track = defaultdict(lambda: deque(maxlen=60))
raid_until = {}


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild or not isinstance(msg.author, discord.Member):
        return
    g, m = msg.guild.id, msg.author
    if is_staff(m) or in_list(g, "exch", msg.channel.id) or any(in_list(g, "exrole", r.id) for r in m.roles):
        return
    low, why = msg.content.lower(), None
    if cfg(g, "am_inv") and INVITE_RE.search(low):
        why = "초대 링크"
    elif cfg(g, "am_link") and LINK_RE.search(low):
        why = "링크"
    elif any(w in low for w in get_list(g, "word")):
        why = "금칙어"
    elif cfgint(g, "am_ment") and len(msg.mentions) + len(msg.role_mentions) > cfgint(g, "am_ment"):
        why = "과도한 멘션"
    elif cfg(g, "am_spam"):
        dq = spam_track[(g, m.id)]
        dq.append(time.time())
        if len(dq) >= 5 and dq[-1] - dq[-5] < 5:
            why = "도배"
            dq.clear()
            try:
                await m.timeout(datetime.timedelta(minutes=1), reason="도배")
            except discord.HTTPException:
                pass
    if why:
        try:
            await msg.delete()
            await msg.channel.send(f"{m.mention} {why}은(는) 허용되지 않아요.", delete_after=5, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
        await log(msg.guild, f"🛡️ 자동관리 · {why}", f"{m.mention} · {msg.channel.mention}\n{msg.content[:1000]}", YELLOW)


@bot.event
async def on_message_delete(msg: discord.Message):
    if msg.guild and not msg.author.bot and msg.content:
        await log(msg.guild, "🗑️ 메시지 삭제", f"{msg.author.mention} · {msg.channel.mention}\n{msg.content[:1500]}")


@bot.event
async def on_message_edit(a: discord.Message, b: discord.Message):
    if a.guild and not a.author.bot and a.content != b.content:
        await log(a.guild, "✏️ 메시지 수정", f"{a.author.mention} · {a.channel.mention} · [이동]({b.jump_url})\n**전** {a.content[:700]}\n**후** {b.content[:700]}")


def fmt(tpl, m: discord.Member):
    return tpl.replace("{user}", m.mention).replace("{server}", m.guild.name).replace("{count}", str(m.guild.member_count))


@bot.event
async def on_member_join(m: discord.Member):
    g, now = m.guild, time.time()
    if not m.bot:
        age = cfgint(g.id, "am_age")
        if age and (discord.utils.utcnow() - m.created_at).days < age:
            try:
                await m.kick(reason=f"계정 생성 {age}일 미만")
                return await log(g, "🛡️ 신규 계정 차단", f"{m} ({m.id}) · 생성 {ts(m.created_at.timestamp(), 'R')}", YELLOW)
            except discord.HTTPException:
                pass
        thr = cfgint(g.id, "am_raid")
        if thr:
            dq = join_track[g.id]
            dq.append(now)
            if len([t for t in dq if now - t < 10]) >= thr and raid_until.get(g.id, 0) < now:
                raid_until[g.id] = now + 120
                await log(g, "🚨 레이드 감지", "2분간 신규 입장자를 자동 추방합니다.", RED)
            if raid_until.get(g.id, 0) > now:
                try:
                    return await m.kick(reason="레이드 방지")
                except discord.HTTPException:
                    pass
        role = g.get_role(cfgint(g.id, "autorole"))
        if role:
            try:
                await m.add_roles(role, reason="자동 역할")
            except discord.HTTPException:
                pass
    ch = g.get_channel(cfgint(g.id, "welch"))
    if ch:
        e = emb("👋 환영합니다!", fmt(cfg(g.id, "welmsg", "{user} 님 환영합니다!"), m), GREEN)
        e.set_thumbnail(url=m.display_avatar.url)
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass
    await log(g, "📥 입장", f"{m.mention} ({m.id}) · 계정 생성 {ts(m.created_at.timestamp(), 'R')}", GREEN)


@bot.event
async def on_member_remove(m: discord.Member):
    g = m.guild
    ch = g.get_channel(cfgint(g.id, "byech"))
    if ch:
        try:
            await ch.send(embed=emb("👋 안녕히 가세요", fmt(cfg(g.id, "byemsg", "**{user}** 님이 떠났습니다."), m).replace(m.mention, str(m)), GRAY))
        except discord.HTTPException:
            pass
    await log(g, "📤 퇴장", f"{m} ({m.id})")
    for r in q("SELECT ch FROM tickets WHERE gid=? AND uid=? AND status='open'", g.id, m.id).fetchall():
        c = g.get_channel(r["ch"])
        if c:
            try:
                await c.send(embed=emb("⚠️ 티켓 개설자가 서버를 나갔습니다", "`/티켓 삭제` 로 정리해 주세요.", YELLOW))
            except discord.HTTPException:
                pass


# ============================================================ 타이머 (기간차단·기간역할·예약공지·추첨)
@tasks.loop(seconds=20)
async def ticker():
    now = int(time.time())
    for r in q("SELECT * FROM timers WHERE until<=?", now).fetchall():
        q("DELETE FROM timers WHERE id=?", r["id"])
        g = bot.get_guild(r["gid"])
        if not g:
            continue
        try:
            if r["kind"] == "unban":
                await g.unban(discord.Object(r["uid"]), reason="기간 차단 만료")
                await log(g, "⏳ 기간 차단 만료", f"<@{r['uid']}> 자동 해제", GREEN)
            elif r["kind"] == "unrole":
                m, role = g.get_member(r["uid"]), g.get_role(r["rid"])
                if m and role:
                    await m.remove_roles(role, reason="기간 역할 만료")
            elif r["kind"] == "notice":
                ch = g.get_channel(r["ch"])
                title, body = json.loads(r["text"])
                if ch:
                    await ch.send(embed=emb(title, body))
        except discord.HTTPException:
            pass
    for r in q("SELECT * FROM gws WHERE ended=0 AND until<=?", now).fetchall():
        await gw_finish(r)


@ticker.before_loop
async def _wait():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    ticker.start()
    synced = await bot.tree.sync()
    print(f"슬래시 명령어 그룹 {len(synced)}개 동기화 완료")


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{SHOP_NAME} | /정보 도움말"))
    print(f"로그인: {bot.user} · 서버 {len(bot.guilds)}개")


if __name__ == "__main__":
    bot.run(TOKEN)
