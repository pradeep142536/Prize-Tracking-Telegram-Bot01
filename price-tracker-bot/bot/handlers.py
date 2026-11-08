"""Button-first Telegram interface; rules and ownership live in TrackerService."""
from __future__ import annotations
import html
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup as KB, LabeledPrice, LinkPreviewOptions, Message, PreCheckoutQuery
from .affiliate import affiliate_url
from .alerts import format_price
from .config import Settings
from .parsers import parse_price
from .service import PLAN_FREE, PLAN_OWNER, PLAN_PREMIUM, AddResult, TrackerService
from .urlsafety import extract_urls
if TYPE_CHECKING:
    from .scheduler import PriceMonitor

router = Router()
log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
HOME = "👋 <b>Welcome to Price Tracker!</b>\n\nYour personal shopping assistant.\n\nI'll watch product prices and notify you when they become cheaper.\n\n<b>What would you like to do?</b>"

class Add(StatesGroup):
    url = State()
    confirm = State()
class Target(StatesGroup):
    custom = State()

def esc(x: object | None) -> str: return html.escape(str(x or ""), quote=False)
def uid(m: Message) -> tuple[int, str | None] | None: return (m.from_user.id, m.from_user.username) if m.from_user else None
def integer(x: object | None) -> int | None:
    try: return int(str(x))
    except (TypeError, ValueError): return None
def money(x: object | None, c: object | None = "INR") -> str:
    try: return format_price(float(x), str(c or "INR")) if x is not None else "Not available"
    except (TypeError, ValueError): return "Not available"
def kb(rows: list[list[Btn]]) -> KB: return KB(inline_keyboard=rows)
def home_row() -> list[Btn]: return [Btn(text="🏠 Home", callback_data="home")]
def store(t: dict[str, Any]) -> str:
    return str(t.get("domain") or urlsplit(str(t.get("url") or "")).hostname or "Online store").removeprefix("www.")
def when(x: object | None) -> str:
    n = integer(x) or 0
    return "Not checked yet" if n <= 0 else datetime.fromtimestamp(n, timezone.utc).strftime("%d %b %Y, %H:%M UTC")
def every(n: int) -> str: return f"{n} seconds" if n < 60 else (f"{n // 60} minutes" if n < 3600 else f"{n / 3600:g} hours")

def home(owner: bool = False) -> KB:
    rows = [[Btn(text="🛍️ Add Product", callback_data="add"), Btn(text="📦 My Products", callback_data="products:0")],
            [Btn(text="💎 Premium Plans", callback_data="premium"), Btn(text="👤 My Account", callback_data="account")],
            [Btn(text="❓ Help & Support", callback_data="help")]]
    if owner: rows.append([Btn(text="🛠️ Owner Menu", callback_data="owner")])
    return kb(rows)

async def replace(q: CallbackQuery, text: str, markup: KB | None) -> None:
    m = q.message
    if not isinstance(m, Message): return
    try:
        if m.photo: await m.edit_caption(caption=text, reply_markup=markup)
        else: await m.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower(): await m.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except Exception:
        log.exception("could not edit UI screen"); await m.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)

def add_text(r: AddResult, reason: bool = False) -> str:
    title, price = f"<b>{esc(r.title)}</b>", money(r.price, r.currency)
    if r.status == "ready": return f"🛍️ <b>Is this the right product?</b>\n\n{title}\n💰 Current price: <b>{price}</b>"
    if r.status == "added": return f"🎉 <b>Product added successfully!</b>\n\n{title}\n💰 Current price: {price}\n\nI'll keep watching its price."
    if r.status == "resumed": return f"▶️ <b>Tracking is active again!</b>\n\n{title}\n💰 Current price: {price}"
    if r.status == "exists": return f"📦 You're already tracking {title}.\n💰 Current price: {price}"
    if r.status == "limit": return f"🚫 <b>Your product list is full.</b>\n\nYou've reached {r.limit if r.limit is not None else 'unlimited'} products. Upgrade or remove a product to make room."
    if r.status == "invalid": return f"❌ {esc(r.error) or 'That link is not supported.'}\n\nPlease send a direct product link."
    if r.status == "fetch_failed": return "😕 <b>I couldn't read that product.</b>\n\nTry its direct product page. Some stores block price checks." + (f"\n\n🔧 Owner detail: {esc(r.error)}" if reason and r.error else "")
    if r.status == "rate_limited": return "⏳ Slow down a little — please try another link in a minute."
    return "😕 Something went wrong. Please try again."

async def preview(m: Message, service: TrackerService, state: FSMContext, text: str) -> None:
    who = uid(m)
    if not who: return
    urls = extract_urls(text)
    if not urls:
        await m.answer("🔗 Please send a direct product link.", reply_markup=kb([[Btn(text="❌ Cancel", callback_data="addcancel")]])); return
    if len(urls) > 1: await m.answer("I’ll start with the first link. Add the others one at a time so each is easy to check.")
    r = await service.preview_link(who[0], who[1], urls[0])
    if r.status != "ready":
        await state.clear()
        rows = [[Btn(text="📦 View My Products", callback_data="products:0")]] if r.status == "exists" else [[Btn(text="↻ Try Another Link", callback_data="add")]]
        rows.append(home_row()); await m.answer(add_text(r, service.is_owner(who[0])), reply_markup=kb(rows)); return
    await state.clear(); await state.update_data(url=r.url or urls[0]); await state.set_state(Add.confirm)
    markup = kb([[Btn(text="✅ Yes, Track It", callback_data="addconfirm")], [Btn(text="❌ Cancel", callback_data="addcancel")]])
    if getattr(r, "image_url", None):
        try: await m.answer_photo(str(r.image_url), caption=add_text(r), reply_markup=markup); return
        except Exception: log.debug("product preview image unavailable", exc_info=True)
    await m.answer(add_text(r), reply_markup=markup, link_preview_options=NO_PREVIEW)

def products(tracks: list[dict[str, Any]], page: int, settings: Settings) -> tuple[str, KB]:
    if not tracks: return "📦 <b>Your product list is empty.</b>\n\nAdd a product link and I'll start watching its price.", kb([[Btn(text="🛍️ Add Product", callback_data="add")], home_row()])
    page = max(0, min(page, len(tracks)-1)); t = tracks[page]; tid = int(t["track_id"]); target = t.get("target_price")
    text = f"📦 <b>Product {page+1} of {len(tracks)}</b>\n\n<b>{esc(t.get('title') or 'Untitled product')}</b>\n🏪 Store: {esc(store(t))}\n💰 Current price: <b>{money(t.get('price'), t.get('currency'))}</b>\n🎯 Target: {money(target, t.get('currency')) if target is not None else 'Not set'}\n{'🟢 Tracking active' if t.get('active') else '⏸️ Tracking paused'}"
    rows = [[Btn(text="✏️ Change Target" if target is not None else "🎯 Set Target", callback_data=f"target:{tid}:{page}"), Btn(text="🛒 Buy Now", url=affiliate_url(str(t["url"]), settings))],
            [Btn(text="📊 Details", callback_data=f"details:{tid}:{page}"), Btn(text="🗑️ Remove", callback_data=f"remove:{tid}:{page}")]]
    if len(tracks) > 1:
        nav = ([] if page == 0 else [Btn(text="⬅️ Previous", callback_data=f"products:{page-1}")]) + ([] if page == len(tracks)-1 else [Btn(text="Next ➡️", callback_data=f"products:{page+1}")])
        rows.append(nav)
    rows += [[Btn(text="🛍️ Add Product", callback_data="add")], home_row()]
    return text, kb(rows)

async def show_products(q: CallbackQuery, service: TrackerService, settings: Settings, page: int) -> None:
    await replace(q, *products(await service.list_tracks(q.from_user.id), page, settings))
async def own_track(service: TrackerService, user: int, tid: int | None) -> dict[str, Any] | None:
    return await service.track_details(user, tid) if tid is not None else None

def target_screen(t: dict[str, Any], page: int) -> tuple[str, KB]:
    tid, rows = int(t["track_id"]), []
    try: current = float(t.get("price"))
    except (TypeError, ValueError): current = 0
    for d, icon in ((5,"🔥"),(10,"💰"),(15,"🏷️")):
        if current > 0:
            amount = round(current*(100-d)/100, 2); rows.append([Btn(text=f"{icon} {d}% Lower — {money(amount,t.get('currency'))}", callback_data=f"targetpreset:{tid}:{d}:{page}")])
    rows.append([Btn(text="✍️ Choose My Own Price", callback_data=f"targetcustom:{tid}:{page}")])
    if t.get("target_price") is not None: rows.append([Btn(text="🗑️ Remove Target", callback_data=f"targetclear:{tid}:{page}")])
    rows.append([Btn(text="⬅️ Back", callback_data=f"products:{page}")])
    return f"🎯 <b>When should I alert you?</b>\n\nCurrent price: <b>{money(t.get('price'),t.get('currency'))}</b>\n\nChoose a quick target below, or pick your own price.", kb(rows)
def target_saved(tid: int, page: int) -> KB: return kb([[Btn(text="✏️ Change Target", callback_data=f"target:{tid}:{page}")], [Btn(text="🗑️ Remove Target", callback_data=f"targetclear:{tid}:{page}")], [Btn(text="📦 My Products", callback_data=f"products:{page}")]])

def detail_text(t: dict[str, Any]) -> str:
    lines=["📊 <b>Product details</b>","",f"<b>{esc(t.get('title') or 'Untitled product')}</b>",f"🏪 Store: {esc(store(t))}",f"💰 Current price: <b>{money(t.get('price'),t.get('currency'))}</b>"]
    if t.get("previous_price") is not None: lines.append(f"↩️ Previous price: {money(t['previous_price'],t.get('currency'))}")
    lines += [f"🎯 Target: {money(t['target_price'],t.get('currency')) if t.get('target_price') is not None else 'Not set'}", "🟢 Tracking active" if t.get("active") else "⏸️ Tracking paused", f"🕒 Last checked: {when(t.get('last_checked'))}"]
    if t.get("history"): lines += ["","📈 <b>Recent price history</b>"] + [f"• {money(x.get('price'),t.get('currency'))} — {when(x.get('ts'))}" for x in t["history"][:5]]
    return "\n".join(lines)
async def details(q: CallbackQuery, t: dict[str, Any], page: int, settings: Settings) -> None:
    m=q.message
    if not isinstance(m,Message): return
    tid=int(t["track_id"]); markup=kb([[Btn(text="✏️ Change Target" if t.get("target_price") is not None else "🎯 Set Target",callback_data=f"target:{tid}:{page}"),Btn(text="🛒 Buy Now",url=affiliate_url(str(t["url"]),settings))],[Btn(text="🔄 Refresh Price",callback_data=f"refresh:{tid}:{page}"),Btn(text="🗑️ Remove",callback_data=f"remove:{tid}:{page}")],[Btn(text="🛍️ Add Product",callback_data="add")],home_row()])
    if t.get("image_url"):
        try: await m.answer_photo(str(t["image_url"]),caption=detail_text(t),reply_markup=markup); return
        except Exception: log.debug("details image unavailable",exc_info=True)
    await m.answer(detail_text(t),reply_markup=markup,link_preview_options=NO_PREVIEW)

def premium_text(s: Settings, info: dict[str, Any]) -> str:
    names={PLAN_FREE:"🆓 Free", "pro":"⭐ Pro", "ultra":"🚀 Ultra", PLAN_PREMIUM:"⭐ Active legacy subscription", PLAN_OWNER:"👑 Owner"}
    return f"⭐ <b>Plans</b>\n\nYour current plan: <b>{names.get(info['plan'],'🆓 Free')}</b>\n\n<b>🆓 Free</b>\n• {s.free_link_limit} products\n• Checks about every {every(s.free_interval)}\n\n<b>⭐ Pro</b> — {s.pro_stars} Stars / {s.subscription_days} days\n• {s.pro_link_limit} products\n• Checks about every {every(s.pro_interval)}\n\n<b>🚀 Ultra</b> — {s.ultra_stars} Stars / {s.subscription_days} days\n• {s.ultra_link_limit} products\n• Checks about every {every(s.ultra_interval)}"
def premium_kb(founding: bool = False) -> KB:
    rows=[[Btn(text="⭐ Get Pro",callback_data="buy:pro"),Btn(text="🚀 Get Ultra",callback_data="buy:ultra")],[Btn(text="🎁 Start 7-Day Pro Trial",callback_data="trial")]]
    if founding: rows.append([Btn(text="🌟 Founding Pro — 25 Stars",callback_data="buy:founding")])
    rows += [[Btn(text="📋 Compare Plans",callback_data="premiumcompare")],home_row()]
    return kb(rows)
HELP={"adding":("🛍️ <b>Adding products</b>","Tap <b>Add Product</b>, send a direct product link, then confirm it."),"targets":("🎯 <b>Target prices</b>","Open a product and tap <b>Set Target</b>. Pick a quick discount or enter your exact price."),"alerts":("🔔 <b>Price alerts</b>","When a tracked price drops, I'll send a clear alert with a Buy Now button."),"plans":("⭐ <b>Plans</b>","Pro and Ultra give you more product slots and faster checks."),"support":("👨‍💻 <b>Contact support</b>","Please contact the person or team who shared this bot for account-specific help.")}
def help_kb() -> KB: return kb([[Btn(text="🛍️ Adding Products",callback_data="help:adding"),Btn(text="🎯 Target Prices",callback_data="help:targets")],[Btn(text="🔔 Price Alerts",callback_data="help:alerts"),Btn(text="⭐ Plans",callback_data="help:plans")],[Btn(text="👨‍💻 Contact Support",callback_data="help:support")],home_row()])
def owner_kb() -> KB: return kb([[Btn(text="📊 Statistics",callback_data="owner:stats"),Btn(text="👥 Users",callback_data="owner:users")],[Btn(text="📦 Tracking Overview",callback_data="owner:tracks"),Btn(text="🔧 System Status",callback_data="owner:system")],[Btn(text="🧪 Send Test Notification",callback_data="owner:testalert")],home_row()])

@router.message(CommandStart())
async def start(m:Message,service:TrackerService,state:FSMContext)->None:
    who=uid(m)
    if who: await state.clear(); await service.register(*who); await m.answer(HOME,reply_markup=home(service.is_owner(who[0])))
@router.message(Command("help"))
async def command_help(m:Message,state:FSMContext)->None:
    await state.clear(); await m.answer("❓ <b>Help & Support</b>\n\nWhat would you like help with?",reply_markup=help_kb())
@router.message(Command("add"))
async def command_add(m:Message,command:CommandObject,service:TrackerService,state:FSMContext)->None:
    if (command.args or "").strip(): await preview(m,service,state,command.args or "")
    else: await state.clear(); await state.set_state(Add.url); await m.answer("🛍️ <b>Let's track a product!</b>\n\nSend or share a supported product link.",reply_markup=kb([[Btn(text="❌ Cancel",callback_data="addcancel")]]))
@router.message(Command("list"))
async def command_list(m:Message,service:TrackerService,settings:Settings)->None:
    who=uid(m)
    if who:
        text,markup=products(await service.list_tracks(who[0]),0,settings); await m.answer(text,reply_markup=markup)
@router.message(Command("plan"))
async def command_plan(m:Message,service:TrackerService)->None:
    who=uid(m)
    if who:
        i=await service.plan_info(who[0]); left="Unlimited" if i["limit"] is None else str(max(0,i["limit"]-i["used"])); await m.answer(f"👤 <b>My Account</b>\n\nCurrent plan: <b>{i['plan'].title()}</b>\nProducts being tracked: <b>{i['used']}</b>\nAvailable product slots: <b>{left}</b>",reply_markup=kb([[Btn(text="📦 My Products",callback_data="products:0")],[Btn(text="💎 Upgrade Plan",callback_data="premium")],home_row()]))
@router.message(Command("premium"))
async def command_premium(m:Message,service:TrackerService)->None:
    who=uid(m)
    if who: await m.answer(premium_text(service.settings,await service.plan_info(who[0])),reply_markup=premium_kb())

@router.callback_query()
async def callback(q:CallbackQuery,service:TrackerService,settings:Settings,state:FSMContext,monitor:"PriceMonitor")->None:
    d=q.data or ""; user=q.from_user.id
    if d=="home": await state.clear(); await q.answer(); await replace(q,HOME,home(service.is_owner(user))); return
    if d=="add": await state.clear(); await state.set_state(Add.url); await q.answer(); await replace(q,"🛍️ <b>Let's track a product!</b>\n\nSend or share a supported product link.",kb([[Btn(text="❌ Cancel",callback_data="addcancel")]])); return
    if d=="addcancel": await state.clear(); await q.answer("Cancelled"); await replace(q,HOME,home(service.is_owner(user))); return
    if d=="addconfirm":
        data=await state.get_data()
        if await state.get_state()!=Add.confirm.state or not data.get("url"): await q.answer("That product preview has expired. Please add the link again.",show_alert=True); return
        await state.clear(); r=await service.add_link(user,q.from_user.username,str(data["url"]))
        if r.status not in {"added","resumed","exists"}: await q.answer("I couldn't add that product.",show_alert=True); await replace(q,add_text(r,service.is_owner(user)),kb([[Btn(text="↻ Try Another Link",callback_data="add")],home_row()])); return
        t=next((x for x in await service.list_tracks(user) if x.get("url")==r.url),None); rows=([[Btn(text="🎯 Set Target Price",callback_data=f"target:{t['track_id']}:0")]] if t else [])+[[Btn(text="📦 View My Products",callback_data="products:0")],home_row()]
        await q.answer("Product added"); await replace(q,add_text(r),kb(rows)); return
    if d.startswith("products:"):
        page=integer(d.split(":",1)[1])
        if page is None or page<0: await q.answer("That page is unavailable.",show_alert=True)
        else: await q.answer(); await show_products(q,service,settings,page)
        return
    if d.startswith("target:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None); t=await own_track(service,user,tid)
        if not t or page is None: await q.answer("That product is no longer in your list.",show_alert=True)
        else: await q.answer(); await replace(q,*target_screen(t,page))
        return
    if d.startswith("targetpreset:"):
        p=d.split(":"); tid,disc,page=(integer(p[1]),integer(p[2]),integer(p[3])) if len(p)==4 else (None,None,None); t=await own_track(service,user,tid)
        try: amount=round(float(t["price"])*(100-disc)/100,2) if t and disc in {5,10,15} else None
        except (TypeError,ValueError): amount=None
        if amount is None or page is None or not await service.set_target(user,tid,amount): await q.answer("I couldn't save that target.",show_alert=True)
        else:
            reached=float(t["price"])<=amount
            if reached: await service.claim_target_alert(user,tid)
            await q.answer("Target already reached" if reached else "Target saved")
            message=(f"🎉 <b>PRICE TARGET REACHED!</b>\n\nCurrent price: <b>{money(t['price'],t.get('currency'))}</b>\nYour target: {money(amount,t.get('currency'))}\nThe current price is below your target!" if reached else f"✅ <b>Target price saved!</b>\n\nI'll notify you when the price reaches <b>{money(amount,t.get('currency'))}</b> or less.")
            await replace(q,message,target_saved(tid,page))
        return
    if d.startswith("targetcustom:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None)
        if not await own_track(service,user,tid) or page is None: await q.answer("That product is no longer in your list.",show_alert=True)
        else: await state.clear(); await state.update_data(tid=tid,page=page); await state.set_state(Target.custom); await q.answer(); await replace(q,"✍️ <b>Choose your own target price</b>\n\nSend just the amount — for example <code>10000</code>.",kb([[Btn(text="❌ Cancel",callback_data="targetcancel")]]))
        return
    if d=="targetcancel": page=integer((await state.get_data()).get("page")) or 0; await state.clear(); await q.answer("Cancelled"); await show_products(q,service,settings,page); return
    if d.startswith("targetclear:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None)
        if tid is None or page is None or not await service.set_target(user,tid,None): await q.answer("That product is no longer in your list.",show_alert=True)
        else: await q.answer("Target removed"); await replace(q,"🔔 <b>Target removed.</b>\n\nI'll still let you know about meaningful price drops.",kb([[Btn(text="🎯 Set a New Target",callback_data=f"target:{tid}:{page}")],[Btn(text="📦 My Products",callback_data=f"products:{page}")]]))
        return
    if d.startswith("details:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None); t=await own_track(service,user,tid)
        if not t or page is None: await q.answer("That product is no longer in your list.",show_alert=True)
        else: await q.answer(); await details(q,t,page,settings)
        return
    if d.startswith("refresh:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None)
        if tid is None or page is None: await q.answer("That product is unavailable.",show_alert=True); return
        t,error=await service.refresh_track(user,tid)
        if not t: await q.answer("⚠️ I couldn't refresh this price right now. Try again later.",show_alert=True)
        else: await q.answer("Price refreshed"); await replace(q,detail_text(t),kb([[Btn(text="✏️ Change Target" if t.get("target_price") is not None else "🎯 Set Target",callback_data=f"target:{tid}:{page}"),Btn(text="🛒 Buy Now",url=affiliate_url(str(t["url"]),settings))],[Btn(text="🔄 Refresh Price",callback_data=f"refresh:{tid}:{page}"),Btn(text="🗑️ Remove",callback_data=f"remove:{tid}:{page}")],[Btn(text="🛍️ Add Product",callback_data="add")],home_row()]))
        return
    if d.startswith("removeconfirm:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None)
        if tid is None or page is None or not await service.remove_track(user,tid): await q.answer("That product was already removed.",show_alert=True)
        else: await q.answer("Removed ✅"); await show_products(q,service,settings,page)
        return
    if d.startswith("remove:"):
        p=d.split(":"); tid,page=(integer(p[1]),integer(p[2])) if len(p)==3 else (None,None); t=await own_track(service,user,tid)
        if not t or page is None: await q.answer("That product is no longer in your list.",show_alert=True)
        else: await q.answer(); await replace(q,f"🗑️ <b>Stop tracking this product?</b>\n\n<b>{esc(t.get('title'))}</b>",kb([[Btn(text="✅ Yes, Remove",callback_data=f"removeconfirm:{tid}:{page}")],[Btn(text="↩️ Keep Tracking",callback_data=f"products:{page}")]]))
        return
    if d=="premium":
        offer=await service.offer_info(user); await q.answer(); await replace(q,premium_text(settings,await service.plan_info(user))+("\n\n🌟 <b>Founding offer:</b> Pro for 25 Stars — first 100 eligible buyers." if offer["founding_available"] else ""),premium_kb(bool(offer["founding_available"]))); return
    if d=="premiumcompare": await q.answer(); await replace(q,f"📋 <b>Compare plans</b>\n\n<b>Free</b>\n• {settings.free_link_limit} products\n• Checks every {every(settings.free_interval)}\n\n<b>Premium</b>\n• {settings.premium_link_limit} products\n• Checks every {every(settings.premium_interval)}\n• {settings.premium_days} days for {settings.premium_stars} Telegram Stars",kb([[Btn(text="⭐ Get Premium",callback_data="premiumbuy")],[Btn(text="⬅️ Back",callback_data="premium")]])); return
    if d.startswith("buy:"):
        kind=d.split(":",1)[1]; spec=service.purchase_spec(kind)
        if kind=="founding":
            offer=await service.offer_info(user)
            if not offer["founding_available"]: await q.answer("That founding offer is no longer available.",show_alert=True); return
        if not spec or not isinstance(q.message,Message): await q.answer("Please open Plans again.",show_alert=True); return
        plan,stars,_purchase_kind=spec; await q.answer()
        await q.message.answer_invoice(title=f"{plan.title()} Plan",description=f"{settings.subscription_days} days of {plan.title()}",payload=f"plan:{plan}:{kind}:{user}",currency="XTR",prices=[LabeledPrice(label=f"{plan.title()} plan",amount=stars)],provider_token="")
        return
    if d=="trial":
        until=await service.activate_trial(user)
        if until is None: await q.answer("You have already used your Pro trial.",show_alert=True)
        else: await q.answer("Pro trial started!"); await replace(q,f"🎉 <b>Your Pro trial is active!</b>\n\nIt ends on <b>{datetime.fromtimestamp(until,timezone.utc).strftime('%d %b %Y')}</b>. No payment was taken.",kb([[Btn(text="📦 My Products",callback_data="products:0")],home_row()]))
        return
    if d=="account":
        i=await service.plan_info(user); offers=await service.offer_info(user); left="Unlimited" if i["limit"] is None else str(max(0,i["limit"]-i["used"])); active=i["plan"]!=PLAN_FREE
        label={PLAN_FREE:"Free", "pro":"Pro", "ultra":"Ultra", PLAN_PREMIUM:"Active legacy subscription", PLAN_OWNER:"Owner"}.get(i['plan'],"Free")
        text=f"👤 <b>My Account</b>\n\nPlan: <b>{label}</b>\nStatus: <b>{'Active' if active else 'Free plan'}</b>\nProducts tracked: <b>{i['used']}</b>\nAvailable slots: <b>{left}</b>\nCheck interval: <b>{every(i['interval'])}</b>"+(f"\nExpires: <b>{when(i['premium_until'])}</b>" if active and i['plan']!=PLAN_OWNER else "")+f"\nTrial used: <b>{'Yes' if offers['trial_used'] else 'No'}</b>"
        await q.answer(); await replace(q,text,kb([[Btn(text="📦 My Products",callback_data="products:0")],[Btn(text="⭐ Upgrade Plan",callback_data="premium")],[Btn(text="❓ Support",callback_data="help")],home_row()])); return
    if d=="help": await q.answer(); await replace(q,"❓ <b>Help & Support</b>\n\nWhat would you like help with?",help_kb()); return
    if d.startswith("help:"):
        info=HELP.get(d.split(":",1)[1])
        if not info: await q.answer("That help topic is unavailable.",show_alert=True)
        else: await q.answer(); await replace(q,f"{info[0]}\n\n{info[1]}",kb([[Btn(text="⬅️ Back to Help",callback_data="help")],home_row()]))
        return
    if d.startswith("owner"):
        if not service.is_owner(user): await q.answer("This area is only for the bot owner.",show_alert=True); return
        if d=="owner": await q.answer(); await replace(q,"🛠️ <b>Owner Menu</b>\n\nChoose a dashboard.",owner_kb()); return
        if d=="owner:testalert":
            await q.answer("Test notification sent")
            if isinstance(q.message,Message): await q.message.answer("🧪 <b>TEST NOTIFICATION</b>\n\nThis confirms that Price Tracker can deliver Telegram messages. No product or alert state was changed.")
            return
        stats=await service.stats()
        if d=="owner:stats": text=f"📊 <b>Statistics</b>\n\n👥 Users: <b>{stats['users']}</b>\n🔗 Tracks: <b>{stats['tracks']}</b>\n📦 Products watched: <b>{stats['products']}</b>\n⭐ Stars received: <b>{stats['stars']}</b>"
        elif d=="owner:users": text=f"👥 <b>Users</b>\n\nRegistered users: <b>{stats['users']}</b>\nProducts tracked: <b>{stats['tracks']}</b>"
        elif d=="owner:tracks": text=f"📦 <b>Tracking Overview</b>\n\nActive products: <b>{stats['products']}</b>\nPaused products: <b>{stats['paused']}</b>\nUser tracks: <b>{stats['tracks']}</b>"
        elif d=="owner:system": text=f"🔧 <b>System Status</b>\n\n<pre>{esc(await monitor.summary())}</pre>"
        else: await q.answer("This owner action is unavailable.",show_alert=True); return
        await q.answer(); await replace(q,text,kb([[Btn(text="⬅️ Back",callback_data="owner")],home_row()])); return
    await q.answer("This button is no longer available. Please open Home and try again.",show_alert=True)

@router.message(Target.custom)
async def custom_target(m:Message,service:TrackerService,state:FSMContext)->None:
    who=uid(m); value=parse_price(m.text or "")
    if not who:return
    if value is None or value<=0: await m.answer("Please send a valid amount, for example <code>10000</code>."); return
    data=await state.get_data(); tid,page=data.get("tid"),integer(data.get("page")) or 0; t=await own_track(service,who[0],tid) if isinstance(tid,int) else None
    if not t or not await service.set_target(who[0],tid,value): await state.clear(); await m.answer("I couldn't find that product. Open <b>My Products</b> and try again."); return
    await state.clear(); reached=float(t["price"])<=value
    if reached: await service.claim_target_alert(who[0],tid)
    message=(f"🎉 <b>PRICE TARGET REACHED!</b>\n\nCurrent price: <b>{money(t['price'],t.get('currency'))}</b>\nYour target: {money(value,t.get('currency'))}\nThe current price is below your target!" if reached else f"✅ <b>Target price saved!</b>\n\nI'll notify you when the price reaches <b>{money(value,t.get('currency'))}</b> or less.")
    await m.answer(message,reply_markup=target_saved(tid,page))
@router.message(Add.confirm,F.text)
async def waiting_confirmation(m:Message)->None: await m.answer("Use the buttons under the product preview to confirm or cancel it.")
@router.message(F.text & ~F.text.startswith("/"),StateFilter(None,Add.url))
async def link(m:Message,service:TrackerService,state:FSMContext)->None: await preview(m,service,state,m.text or "")
@router.pre_checkout_query()
async def checkout(q:PreCheckoutQuery,service:TrackerService)->None:
    parts=q.invoice_payload.split(":")
    valid=len(parts)==4 and parts[0]=="plan" and parts[3]==str(q.from_user.id)
    spec=service.purchase_spec(parts[2] if valid else "")
    valid=bool(valid and spec and spec[0]==parts[1] and spec[1]==q.total_amount and q.currency=="XTR")
    await q.answer(ok=valid,error_message=None if valid else "This invoice isn't valid for your account.")
@router.message(F.successful_payment)
async def payment(m:Message,service:TrackerService,settings:Settings)->None:
    p,who=m.successful_payment,uid(m)
    if not p or not who or p.currency!="XTR": return
    parts=p.invoice_payload.split(":")
    if len(parts)!=4 or parts[0]!="plan" or parts[3]!=str(who[0]): return
    plan,kind=parts[1],parts[2]; spec=service.purchase_spec(kind)
    if not spec or spec[0]!=plan or spec[1]!=p.total_amount: return
    if kind=="founding" and not (await service.offer_info(who[0]))["founding_available"]: return
    if not await service.record_payment(who[0],p.telegram_payment_charge_id,p.total_amount,plan,spec[2]): return
    until=await service.activate_plan(who[0],plan,settings.subscription_days,spec[2])
    await m.answer(f"🎉 <b>Thank you!</b> {plan.title()} is active until <b>{datetime.fromtimestamp(until,timezone.utc).strftime('%d %b %Y')}</b>.",reply_markup=home(False))

# Existing command users keep their shortcuts; the button flows above are the
# primary interface for everyone else.
@router.message(Command("target"))
async def command_target(m:Message,command:CommandObject,service:TrackerService)->None:
    who=uid(m); parts=(command.args or "").replace("#","").split()
    if not who or len(parts)!=2 or not parts[0].isdigit():
        await m.answer("🎯 Open <b>My Products</b> and tap <b>Set Target</b>."); return
    amount=None if parts[1].lower()=="off" else parse_price(parts[1])
    if amount is None and parts[1].lower()!="off": await m.answer("I couldn't read that price."); return
    if await service.set_target_number(who[0],int(parts[0]),amount): await m.answer("✅ Target updated.")
    else: await m.answer("I couldn't find that product. Open <b>My Products</b> to choose it.")

@router.message(Command("remove"))
async def command_remove(m:Message,command:CommandObject,service:TrackerService,settings:Settings)->None:
    who=uid(m); number=(command.args or "").lstrip("#").strip()
    if not who or not number.isdigit(): await m.answer("📦 Open <b>My Products</b> and use the 🗑️ Remove button."); return
    if await service.remove_track_number(who[0],int(number)):
        await m.answer("✅ Product removed."); text,markup=products(await service.list_tracks(who[0]),0,settings); await m.answer(text,reply_markup=markup)
    else: await m.answer("I couldn't find that product.")

@router.message(Command("grant"))
async def command_grant(m:Message,command:CommandObject,service:TrackerService)->None:
    who=uid(m); parts=(command.args or "").split()
    if not who or not service.is_owner(who[0]): return
    if len(parts)!=2 or not all(x.lstrip("-").isdigit() for x in parts): await m.answer("Usage: /grant &lt;user_id&gt; &lt;days&gt;"); return
    until=await service.grant_premium(int(parts[0]),int(parts[1])); await m.answer(f"✅ Premium for {parts[0]} until {datetime.fromtimestamp(until,timezone.utc).strftime('%d %b %Y')}")

@router.message(Command("monitor"))
async def command_monitor(m:Message,service:TrackerService,monitor:"PriceMonitor")->None:
    who=uid(m)
    if who and service.is_owner(who[0]): await m.answer(f"<pre>{esc(await monitor.summary())}</pre>")

@router.message(Command("stats"))
async def command_stats(m:Message,service:TrackerService)->None:
    who=uid(m)
    if who and service.is_owner(who[0]):
        s=await service.stats(); await m.answer(f"👥 Users: {s['users']}\n🔗 Tracks: {s['tracks']}\n📦 Products watched: {s['products']} (paused: {s['paused']})\n⭐ Stars earned: {s['stars']}")
