"""بحث عن أسعار المنتجات في المواقع المصرية (Amazon / Noon / Jumia / Carrefour / الصيدليات ...).

الاستخدام:
    python price-watch/search.py                 # يدور على كل منتجات watchlist.txt
    python price-watch/search.py --query "..."   # بحث سريع عن منتج واحد

النتيجة بتتكتب في prices/data.json (الصفحة prices/index.html بتقراها)
وبيتضاف سطر لكل سعر في price-watch/history.csv.
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "price-watch"
DATA = ROOT / "prices" / "data.json"
HISTORY = HERE / "history.csv"
DEBUG = HERE / "debug"
CAIRO = timezone(timedelta(hours=3))
MAX_PER_SITE = 12
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- ترجمة الاسم

AR2EN = {
    "جونسون": "johnson", "جونسونز": "johnson", "بيبي": "baby", "اطفال": "baby",
    "شامبو": "shampoo", "بلسم": "conditioner", "زيت": "oil", "ارجان": "argan",
    "لوريال": "loreal", "ايفا": "eva", "هير": "hair", "ماسك": "mask", "شعر": "hair",
    "كريم": "cream", "بامبرز": "pampers", "بامبيرز": "pampers", "مولفيكس": "molfix",
    "حفاضات": "diapers", "حفاضه": "diapers", "مقاس": "size", "صابون": "soap",
    "غسول": "wash", "لوشن": "lotion", "واقي": "sunscreen", "شمس": "", "ديتول": "dettol",
    "نيفيا": "nivea", "دوف": "dove", "بانتين": "pantene", "سيتافيل": "cetaphil",
    "لاروش": "la roche posay", "فيشي": "vichy", "سيروم": "serum", "مزيل": "deodorant",
    "عرق": "", "معجون": "toothpaste", "اسنان": "", "كولجيت": "colgate", "سيجنال": "signal",
    "مناديل": "wipes", "مبلله": "", "فاكيشن": "vacation", "فاكاشين": "vacation",
    "كابيكس": "capixy", "سانسيلك": "sunsilk", "كلير": "clear", "هيد": "head",
    "شولدرز": "shoulders", "جارنييه": "garnier", "الفيف": "elvive", "ايلفيف": "elvive",
    "بيبي جوي": "baby joy", "فاين": "fine", "بودره": "powder", "مرطب": "moisturizer",
}
STOP = {"for", "the", "and", "with", "of", "&", "-", "من", "في", "و"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.translate(str.maketrans("أإآةىـ٠١٢٣٤٥٦٧٨٩", "اااهي" + "\0" + "0123456789")).replace("\0", "")
    s = re.sub(r"[’'`´]", "", s)                       # l'oreal -> loreal
    return re.sub(r"[^\w؀-ۿ.]+", " ", s).strip()


def to_english(name: str) -> str:
    words = norm(name).split()
    out = [AR2EN.get(w, w) for w in words]
    return " ".join(w for w in out if w).strip() or name


def tokens(s: str):
    return [t for t in norm(s).split() if t not in STOP]


def relevance(query: str, title: str) -> float:
    q = tokens(query)
    if not q:
        return 1.0
    t = " " + norm(title) + " "
    hit = sum(1 for w in q if (" " + w + " ") in t or (len(w) > 3 and w in t))
    return hit / len(q)


# ---------------------------------------------------------------- الأسعار والأحجام

def to_number(txt):
    if txt is None:
        return None
    if isinstance(txt, (int, float)):
        return float(txt)
    m = re.search(r"\d[\d,٬]*(?:[.٫]\d+)?", str(txt).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if not m:
        return None
    n = m.group(0).replace(",", "").replace("٬", "").replace("٫", ".")
    try:
        v = float(n)
    except ValueError:
        return None
    return v if 0 < v < 1_000_000 else None


SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|مل|مللي|ملي|ltr|liter|litre|lt|l|لتر|kg|كجم|كيلو|gm|gr|g|grams?|جم|جرام|"
    r"pcs|pieces|piece|pc|count|ct|diapers|wipes|قطعه|قطع|حفاضه|منديل)\b", re.I)
PACK_RE = re.compile(r"(?:pack of\s*|عبوه\s*|\bx\s*)(\d{1,2})\b|\b(\d{1,2})\s*(?:x|×)\s*\d", re.I)


def size_of(title):
    """يرجع (الكمية, الوحدة) — مللي أو جرام أو قطعة."""
    t = norm(title)
    m = SIZE_RE.search(t)
    if not m:
        return None, None
    qty, unit = float(m.group(1).replace(",", ".")), m.group(2).lower()
    if unit in ("ltr", "liter", "litre", "lt", "l", "لتر"):
        qty, unit = qty * 1000, "ml"
    elif unit in ("kg", "كجم", "كيلو"):
        qty, unit = qty * 1000, "g"
    elif unit in ("ml", "مل", "مللي", "ملي"):
        unit = "ml"
    elif unit in ("gm", "gr", "g", "gram", "grams", "جم", "جرام"):
        unit = "g"
    else:
        unit = "pc"
    p = PACK_RE.search(t)
    if p:
        n = int(p.group(1) or p.group(2))
        if 1 < n <= 24:
            qty *= n
    return (qty, unit) if qty > 0 else (None, None)


def unit_price(price, qty, unit):
    if not (price and qty):
        return None, None
    if unit == "pc":
        return round(price / qty, 2), "للقطعة"
    return round(price / qty * 100, 2), "لكل 100 " + ("مل" if unit == "ml" else "جم")


# ---------------------------------------------------------------- سحب الصفحات

AMAZON_JS = """() => [...document.querySelectorAll('div[data-component-type="s-search-result"]')].map(c => {
  const h2 = c.querySelector('h2');
  const a = c.querySelector('a.a-link-normal[href*="/dp/"]') || c.querySelector('h2 a') || c.querySelector('a[href*="/dp/"]');
  return {
    title: (c.querySelector('[data-cy="title-recipe"]')?.innerText || h2?.getAttribute('aria-label') || h2?.innerText || '').replace(/\\s+/g,' ').trim(),
    url: a ? a.href : '',
    price: c.querySelector('.a-price:not(.a-text-price) .a-offscreen')?.textContent || '',
    old: c.querySelector('.a-price.a-text-price .a-offscreen')?.textContent || '',
    img: c.querySelector('img.s-image')?.src || '',
  };
})"""

JUMIA_JS = """() => [...document.querySelectorAll('article.prd')].map(c => {
  const img = c.querySelector('img');
  return {
    title: (c.querySelector('.name')?.innerText || '').trim(),
    url: c.querySelector('a.core')?.href || c.querySelector('a')?.href || '',
    price: c.querySelector('.prc')?.innerText || '',
    old: c.querySelector('.old')?.innerText || '',
    img: img ? (img.dataset.src || img.src) : '',
  };
})"""

# أي موقع: نلاقي لينكات المنتجات، ونطلع لأكبر "كارت" فيه منتج واحد بس، ونقرا منه الاسم والأسعار.
GENERIC_JS = r"""(linkPart) => {
  const clean = h => h.split('#')[0].split('?')[0];
  const isProd = a => a.href && a.href.startsWith('http') && a.href.includes(linkPart) && !/\/(search|catalogsearch|cart|login|account)/i.test(a.href);
  const groups = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    if (!isProd(a)) continue;
    const h = clean(a.href);
    if (!groups.has(h)) groups.set(h, a);
  }
  const priceRe = /(?:EGP|E£|LE|L\.E\.?|ج\.?\s?م\.?|جنيه)\s*([\d٠-٩][\d٠-٩,٬.٫]*)|([\d٠-٩][\d٠-٩,٬.٫]*)\s*(?:EGP|E£|LE|L\.E\.?|ج\.?\s?م\.?|جنيه)/gi;
  const hasPrice = s => new RegExp(priceRe.source, 'i').test(s);
  const out = [];
  for (const [href, a] of groups) {
    let card = a;
    for (let i = 0; i < 8 && card.parentElement && card.parentElement !== document.body; i++) {
      const p = card.parentElement;
      const hs = new Set([...p.querySelectorAll('a[href]')].filter(isProd).map(x => clean(x.href)));
      if (hs.size > 1) break;
      card = p;
    }
    const text = (card.innerText || '').replace(/\([^)]*\/[^)]*\)/g, ' ');
    const prices = [];
    for (const m of text.matchAll(priceRe)) prices.push(m[1] || m[2]);
    const img = card.querySelector('img');
    const lines = text.split('\n').map(s => s.trim()).filter(s => s.length >= 8 && s.length <= 220 && /[A-Za-z؀-ۿ]{3}/.test(s) && !hasPrice(s));
    const title = (a.getAttribute('title') || (img && img.alt) || lines.sort((x, y) => y.length - x.length)[0] || '').trim();
    out.push({ title, url: href, prices, img: img ? (img.currentSrc || img.src || img.dataset.src || '') : '' });
  }
  return out;
}"""


def from_generic(rows):
    out = []
    for r in rows:
        nums = []
        for p in r.get("prices", []):
            v = to_number(p)
            if v and v not in nums:
                nums.append(v)
        if not nums:
            continue
        first = nums[:2]
        price, old = min(first), max(first)
        if old > price * 5:
            old = price
        out.append({"title": r["title"], "url": r["url"], "price": price,
                    "old": old if old > price else None, "img": r.get("img", "")})
    return out


def from_specific(rows):
    out = []
    for r in rows:
        price, old = to_number(r.get("price")), to_number(r.get("old"))
        if not price or not r.get("url"):
            continue
        out.append({"title": r["title"], "url": r["url"], "price": price,
                    "old": old if old and old > price else None, "img": r.get("img", "")})
    return out


async def scroll(page):
    for _ in range(4):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(600)


async def search_site(ctx, site, q):
    page = await ctx.new_page()
    url = site["url"].replace("{q}", quote_plus(q))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(20):                          # صفحة "Just a moment" بتاعة Cloudflare
            title = (await page.title()).lower()
            if not any(w in title for w in ("just a moment", "attention required", "لحظة")):
                break
            await page.wait_for_timeout(1500)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await scroll(page)
        body = (await page.inner_text("body"))[:3000].lower()
        if any(w in body for w in ("captcha", "robot check", "access denied", "are you a human", "pardon our interruption")):
            raise RuntimeError("الموقع حجب البحث (captcha / bot check)")
        rows = []
        if site["type"] == "amazon":
            rows = from_specific(await page.evaluate(AMAZON_JS))
        elif site["type"] == "jumia":
            rows = from_specific(await page.evaluate(JUMIA_JS))
        if not rows:
            link = site.get("link") or {"amazon": "/dp/", "jumia": ".html"}.get(site["type"], "/p/")
            rows = from_generic(await page.evaluate(GENERIC_JS, link))
        if not rows:
            DEBUG.mkdir(exist_ok=True)
            await page.screenshot(path=str(DEBUG / f"{site['key']}.png"), full_page=False)
            title = await page.title()
            raise RuntimeError(f"مفيش نتايج اتقرت (عنوان الصفحة: {title[:80]!r})")
        return rows
    finally:
        await page.close()


def google_shopping(q):
    """اختياري: لو فيه SERPAPI_KEY بيجيب نتايج Google Shopping مصر (بيغطي صيدليات ومحلات كتير)."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return None
    r = requests.get("https://serpapi.com/search.json", timeout=60, params={
        "engine": "google_shopping", "q": q, "gl": "eg", "hl": "en", "api_key": key})
    r.raise_for_status()
    out = []
    for it in r.json().get("shopping_results", [])[:30]:
        price = to_number(it.get("extracted_price") or it.get("price"))
        old = to_number(it.get("extracted_old_price") or it.get("old_price"))
        if not price:
            continue
        out.append({"site": it.get("source") or "Google Shopping", "title": it.get("title", ""),
                    "url": it.get("product_link") or it.get("link") or "", "price": price,
                    "old": old if old and old > price else None, "img": it.get("thumbnail", "")})
    return out


# ---------------------------------------------------------------- تشغيل

def finish(site_name, rows, q):
    out = []
    for r in rows:
        score = relevance(q, r["title"])
        if score < 0.6:
            continue
        qty, unit = size_of(r["title"])
        up, up_label = unit_price(r["price"], qty, unit)
        disc = round((1 - r["price"] / r["old"]) * 100) if r.get("old") else 0
        out.append({**r, "site": r.get("site") or site_name, "discount": disc,
                    "unit_price": up, "unit_label": up_label, "score": round(score, 2)})
    out.sort(key=lambda x: (-x["score"], x["price"]))
    return out[:MAX_PER_SITE]


async def run_query(ctx, sites, name, q):
    print(f"\n🔎 {name}  →  '{q}'", flush=True)
    results, errors = [], {}

    async def one(site):
        try:
            try:
                rows = await search_site(ctx, site, q)
            except Exception as first:
                print(f"   {site['name']}: محاولة تانية ({str(first).splitlines()[0][:80]})", flush=True)
                await asyncio.sleep(10)
                rows = await search_site(ctx, site, q)
            got = finish(site["name"], rows, q)
            print(f"   {site['name']}: {len(rows)} منتج في الصفحة، {len(got)} مطابق", flush=True)
            results.extend(got)
            if not got:
                errors[site["name"]] = "مفيش منتج مطابق"
        except Exception as e:
            msg = str(e).split("\n")[0][:200]
            print(f"   {site['name']}: ❌ {msg}", flush=True)
            errors[site["name"]] = msg

    await asyncio.gather(*(one(s) for s in sites))
    try:
        gs = google_shopping(q)
        if gs is not None:
            got = finish("Google Shopping", gs, q)
            print(f"   Google Shopping: {len(got)} مطابق", flush=True)
            results.extend(got)
    except Exception as e:
        errors["Google Shopping"] = str(e)[:200]

    results.sort(key=lambda x: x["price"])
    best = results[0] if results else None
    top_disc = max(results, key=lambda x: x["discount"]) if results else None
    with_unit = [r for r in results if r["unit_price"]]
    best_unit = min(with_unit, key=lambda x: x["unit_price"]) if with_unit else None
    return {"name": name, "query": q, "results": results, "errors": errors,
            "best": best, "top_discount": top_disc if top_disc and top_disc["discount"] > 0 else None,
            "best_unit": best_unit}


def read_watchlist():
    items = []
    for line in (HERE / "watchlist.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, q = line.partition("|")
        name, q = name.strip(), q.strip()
        items.append((name, q or to_english(name)))
    return items


def append_history(stamp, products):
    new = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["التاريخ", "المنتج", "الموقع", "الاسم في الموقع", "السعر", "السعر قبل", "الخصم %", "اللينك"])
        for p in products:
            for r in p["results"]:
                w.writerow([stamp, p["name"], r["site"], r["title"], r["price"], r["old"] or "", r["discount"], r["url"]])


def telegram(products, previous):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    prev_best = {p["name"]: (p.get("best") or {}).get("price") for p in previous}
    lines = ["🛒 <b>أحسن أسعار النهارده</b>"]
    for p in products:
        b = p["best"]
        if not b:
            lines.append(f"\n• {p['name']}: مفيش نتايج")
            continue
        was = prev_best.get(p["name"])
        tag = f" 🔥 نزل من {was:g}" if was and b["price"] < was else ""
        lines.append(f"\n• <b>{p['name']}</b>: {b['price']:g} ج على {b['site']}{tag}\n  <a href=\"{b['url']}\">{b['title'][:70]}</a>")
        d = p["top_discount"]
        if d and d is not b:
            lines.append(f"  أكبر خصم: {d['discount']}% على {d['site']} ({d['price']:g} ج)")
    page = os.environ.get("PRICES_PAGE_URL")
    if page:
        lines.append(f"\n📊 التفاصيل: {page}")
    text, chunks = "", []
    for l in lines:
        if len(text) + len(l) > 3800:
            chunks.append(text)
            text = ""
        text += l + "\n"
    chunks.append(text)
    for c in chunks:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
            "chat_id": chat, "text": c, "parse_mode": "HTML", "disable_web_page_preview": "true"})


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="بحث سريع عن منتج واحد بدل قايمة watchlist")
    ap.add_argument("--sites", help="مواقع معينة بس، مفصولة بفاصلة (مثلاً amazon,jumia)")
    args = ap.parse_args()

    cfg = json.loads((HERE / "sites.json").read_text(encoding="utf-8"))
    sites = [s for s in cfg["sites"] if s.get("enabled", True)]
    if args.sites:
        keep = {k.strip() for k in args.sites.split(",")}
        sites = [s for s in sites if s["key"] in keep]

    if args.query and args.query.strip():
        name = args.query.strip()
        n, _, q = name.partition("|")
        items = [(n.strip(), q.strip() or to_english(n))]
    else:
        items = read_watchlist()

    old = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    stamp = datetime.now(CAIRO).strftime("%Y-%m-%d %H:%M")

    async with async_playwright() as pw:
        # على GitHub بنشغّل Google Chrome الحقيقي بشاشة وهمية (xvfb) — المواقع بتحجب الـ headless أكتر.
        browser = await pw.chromium.launch(
            channel=os.environ.get("BROWSER_CHANNEL") or None,
            headless=os.environ.get("HEADED") != "1",
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900},
                                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9,ar;q=0.8"})
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        prev = {p["name"]: p for p in old.get("watch", []) + old.get("quick", [])}
        products = []
        for name, q in items:
            p = await run_query(ctx, sites, name, q)
            p["checked"] = stamp
            if not p["results"] and prev.get(name, {}).get("results"):
                # كل المواقع فشلت المرة دي: نسيب آخر أسعار اتجابت ونوضح إنها قديمة
                p = {**prev[name], "errors": p["errors"], "stale": True}
            products.append(p)
        await browser.close()

    if args.query:
        quick = [p for p in old.get("quick", []) if p["name"] != products[0]["name"]]
        data = {**old, "quick": (products + quick)[:15]}
        data.setdefault("watch", [])
    else:
        data = {**old, "watch": products, "updated": stamp}
        data.setdefault("quick", [])
    data["sites"] = [s["name"] for s in sites]
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    append_history(stamp, products)

    if not args.query:
        try:
            telegram(products, old.get("watch", []))
        except Exception as e:
            print("Telegram:", e)

    total = sum(len(p["results"]) for p in products)
    print(f"\n✅ خلص: {len(products)} منتج، {total} سعر.")
    if total == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
