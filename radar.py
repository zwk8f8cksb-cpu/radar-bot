#!/usr/bin/env python3
"""
RADAR DES MARCHÉS - bot de veille PEA / ETF / crypto
=====================================================
Repère ce qui "sort du lot" : volumes anormaux, hausse des discussions,
actualité récente, momentum (sans être déjà trop monté), et pénalise les
signaux de risque (petite capitalisation, pump, faible liquidité).

Sources :
  - Cours / volumes actions & ETF : Yahoo Finance (yfinance)
  - Cours / volumes crypto        : CoinGecko (API publique, clé optionnelle)
  - Actualités                    : Google Actualités (flux RSS)
  - Discussions                   : Reddit (public) et X (si token API)

Variables d'environnement (toutes optionnelles) :
  X_BEARER_TOKEN       -> active le signal X (API payante)
  COINGECKO_API_KEY    -> clé "demo" gratuite, limite moins les erreurs 429
  TELEGRAM_BOT_TOKEN   -> envoi du rapport sur Telegram (avec --send)
  TELEGRAM_CHAT_ID

Usage :
  python radar.py                 # rapport complet (actions, ETF, crypto, thèmes)
  python radar.py --no-social     # sans Reddit / X
  python radar.py --only crypto   # crypto | pea | themes
  python radar.py --send          # envoie aussi le rapport sur Telegram

Planification (cron, tous les jours ouvrés à 7h30) :
  30 7 * * 1-5 cd /chemin/radar_bot && /usr/bin/python3 radar.py --send

AVERTISSEMENT : outil de repérage, pas un conseil en investissement.
Un score élevé = attention inhabituelle, pas rendement garanti. L'éligibilité
PEA doit toujours être vérifiée auprès de l'émetteur ou de ton courtier.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests

# --------------------------------------------------------------------------
# CONFIGURATION (modifiable, ou surchargée par watchlist.json)
# --------------------------------------------------------------------------

# Tickers Yahoo Finance. "PEA à vérifier" : toujours confirmer l'éligibilité.
EQUITIES = {
    "AIR.PA": "Airbus", "MC.PA": "LVMH", "SU.PA": "Schneider Electric",
    "STMPA.PA": "STMicroelectronics", "ASML.AS": "ASML", "SAP.DE": "SAP",
    "SAN.PA": "Sanofi", "TTE.PA": "TotalEnergies", "RMS.PA": "Hermès",
    "DSY.PA": "Dassault Systèmes", "HO.PA": "Thales", "SAF.PA": "Safran",
    "RHM.DE": "Rheinmetall", "SIE.DE": "Siemens", "OR.PA": "L'Oréal",
    "ENR.DE": "Siemens Energy", "EXA.PA": "Exail Technologies",
}

# ETF : ticker -> (nom, indice suivi, frais annuels en % ou None = à compléter)
ETFS = {
    "CW8.PA": ("Amundi MSCI World", "MSCI World", None),
    "PANX.PA": ("Amundi Nasdaq-100", "Nasdaq-100", None),
    "PE500.PA": ("Amundi S&P 500", "S&P 500", None),
    "PAEEM.PA": ("Amundi MSCI Emerging Markets", "MSCI Emerging Markets", None),
    "ESE.PA": ("BNP Paribas S&P 500", "S&P 500", None),
}

CORE_CRYPTOS = ["bitcoin", "ethereum", "solana"]
STABLES = {"usdt", "usdc", "dai", "usde", "fdusd", "tusd", "usds", "pyusd", "usdd"}
CRYPTO_DEEP_ANALYSIS = 14   # nb de cryptos analysées en profondeur (limites API)

THEMES = {
    "IA & semi-conducteurs": "intelligence artificielle semi-conducteurs",
    "Défense & armement": "défense armement budget militaire",
    "Nucléaire & uranium": "nucléaire uranium réacteurs",
    "Luxe": "luxe LVMH Hermès consommation",
    "Santé / obésité": "laboratoires obésité GLP-1 santé",
    "Cybersécurité": "cybersécurité cyberattaque",
    "Énergie & renouvelables": "énergies renouvelables solaire éolien",
    "Régulation crypto": "régulation crypto ETF bitcoin",
}

# Pondération du score /100 (renormalisée si une source manque)
WEIGHTS = {"volume": 30, "mentions": 25, "news": 20, "momentum": 15}
MAX_RISK_PENALTY = 10

POSITIVE = {
    "hausse", "bond", "record", "dépasse", "relève", "contrat", "accord", "rachat",
    "upgrade", "surperforme", "bénéfice", "croissance", "approbation", "partenariat",
    "surge", "beats", "soars", "rally", "record", "wins", "raises", "approval", "jumps",
}
NEGATIVE = {
    "chute", "baisse", "plonge", "avertissement", "enquête", "fraude", "perte",
    "abaisse", "downgrade", "procès", "piratage", "faillite", "dégringole",
    "plunges", "misses", "probe", "lawsuit", "hack", "bankruptcy", "falls", "cuts",
}

UA = {"User-Agent": "radar-bot/1.0 (veille personnelle)"}
OUT_DIR = Path(__file__).parent / "reports"
SITE_DIR = Path(__file__).parent / "docs"   # page web publiée via GitHub Pages


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# MODÈLE
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    kind: str                     # "action" | "etf" | "crypto"
    symbol: str
    name: str
    note: str = ""                # ex : "PEA à vérifier", indice ETF
    price: float | None = None
    chg_1d: float | None = None   # en %
    chg_7d: float | None = None   # en %
    vol_ratio: float | None = None
    mention_growth: float | None = None
    mentions_24h: int = 0
    news_24h: int = 0
    news_growth: float | None = None
    sentiment: float | None = None
    headlines: list[str] = field(default_factory=list)
    earnings_soon: str | None = None
    market_cap: float | None = None
    trending: bool = False
    risk_penalty: float = 0.0
    risks: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


# --------------------------------------------------------------------------
# COLLECTE : ACTUALITÉS, REDDIT, X
# --------------------------------------------------------------------------

def sentiment_of(title: str) -> float:
    words = {w.strip(".,:;!?()'\"").lower() for w in title.split()}
    pos, neg = len(words & POSITIVE), len(words & NEGATIVE)
    return (pos - neg) / max(1, pos + neg)


def news_activity(query: str) -> dict | None:
    """Articles Google Actualités sur 7 jours : volume 24h vs moyenne quotidienne."""
    url = (f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}"
           "&hl=fr&gl=FR&ceid=FR:fr")
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"  ! news '{query[:40]}': {e}")
        return None
    now = time.time()
    n24, prev, sents, heads = 0, 0, [], []
    for e in feed.entries:
        ts = getattr(e, "published_parsed", None)
        if not ts:
            continue
        age_h = (now - calendar.timegm(ts)) / 3600
        if age_h <= 24:
            n24 += 1
        else:
            prev += 1
        if age_h <= 48:
            sents.append(sentiment_of(e.title))
            if len(heads) < 3:
                heads.append(e.title)
    prev_avg = prev / 6
    return {
        "n24": n24,
        "growth": (n24 + 1) / (prev_avg + 1),
        "sentiment": statistics.mean(sents) if sents else None,
        "headlines": heads,
    }


def reddit_activity(query: str) -> tuple[int, float] | None:
    """(posts sur 24h, moyenne quotidienne des jours précédents) - API publique."""
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "new", "t": "week", "limit": 100},
            headers=UA, timeout=15,
        )
        r.raise_for_status()
        posts = r.json()["data"]["children"]
    except Exception as e:  # noqa: BLE001
        log(f"  ! reddit '{query[:40]}': {e}")
        return None
    now = time.time()
    times = [p["data"]["created_utc"] for p in posts]
    n24 = sum(1 for t in times if now - t <= 86400)
    older = [t for t in times if now - t > 86400]
    span_days = 6.0
    if len(times) >= 100:  # résultats tronqués : on mesure la vraie fenêtre couverte
        span_days = max((now - min(times)) / 86400 - 1, 0.5)
    return n24, len(older) / span_days


def x_activity(query: str) -> tuple[int, float] | None:
    """Compteurs de tweets (API X v2 'counts/recent'), nécessite X_BEARER_TOKEN."""
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/counts/recent",
            params={"query": query, "granularity": "hour"},
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        r.raise_for_status()
        buckets = [b["tweet_count"] for b in r.json()["data"]]
    except Exception as e:  # noqa: BLE001
        log(f"  ! X '{query[:40]}': {e}")
        return None
    if len(buckets) < 48:
        return None
    n24 = sum(buckets[-24:])
    prev = buckets[:-24]
    return n24, sum(prev) / max(1, len(prev) / 24)


def attach_social(c: Candidate, reddit_q: str, x_q: str, use_social: bool) -> None:
    if not use_social:
        return
    n24, prev, got = 0, 0.0, False
    for res in (reddit_activity(reddit_q), x_activity(x_q)):
        if res:
            got = True
            n24 += res[0]
            prev += res[1]
    time.sleep(1.0)
    if got:
        c.mentions_24h = n24
        c.mention_growth = (n24 + 1) / (prev + 1)


def attach_news(c: Candidate, query: str) -> None:
    res = news_activity(query)
    time.sleep(0.6)
    if not res:
        return
    c.news_24h = res["n24"]
    c.news_growth = res["growth"]
    c.sentiment = res["sentiment"]
    c.headlines = res["headlines"]


# --------------------------------------------------------------------------
# COLLECTE : ACTIONS & ETF (Yahoo Finance)
# --------------------------------------------------------------------------

def analyze_ticker(ticker: str, kind: str, name: str, note: str) -> Candidate | None:
    import yfinance as yf  # import tardif : le module crypto fonctionne sans

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="3mo", auto_adjust=True)
    except Exception as e:  # noqa: BLE001
        log(f"  ! {ticker}: {e}")
        return None
    if hist is None or len(hist) < 25:
        log(f"  ! {ticker}: historique insuffisant")
        return None
    close, vol = hist["Close"], hist["Volume"]
    c = Candidate(kind=kind, symbol=ticker, name=name, note=note)
    c.price = float(close.iloc[-1])
    c.chg_1d = float(close.iloc[-1] / close.iloc[-2] - 1) * 100
    c.chg_7d = float(close.iloc[-1] / close.iloc[-6] - 1) * 100
    avg_vol = float(vol.iloc[-31:-1].mean())
    if avg_vol > 0:
        c.vol_ratio = float(vol.iloc[-1]) / avg_vol
    turnover = float((vol.iloc[-20:] * close.iloc[-20:]).mean())

    try:  # prochains résultats (si disponibles)
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d = dates[0]
            if 0 <= (d - dt.date.today()).days <= 7:
                c.earnings_soon = d.isoformat()
    except Exception:  # noqa: BLE001
        pass

    if turnover < 1_000_000:
        c.risks.append("liquidité faible (<1 M€/jour échangé)")
        c.risk_penalty += 5
    if abs(c.chg_1d) > 15:
        c.risks.append(f"mouvement brutal sur 1 jour ({c.chg_1d:+.0f} %)")
        c.risk_penalty += 3
    return c


def collect_pea(use_social: bool) -> list[Candidate]:
    out: list[Candidate] = []
    for ticker, name in EQUITIES.items():
        log(f"Action {ticker} ({name})")
        c = analyze_ticker(ticker, "action", name, "PEA à vérifier")
        if not c:
            continue
        attach_news(c, f'"{name}" (bourse OR action OR résultats)')
        attach_social(c, f'"{name}"', f'"{name}" -is:retweet', use_social)
        if c.vol_ratio and c.vol_ratio > 8 and c.news_24h == 0:
            c.risks.append("volume très élevé sans actualité identifiée")
            c.risk_penalty += 2
        out.append(c)
    for ticker, (name, index, ter) in ETFS.items():
        log(f"ETF {ticker} ({name})")
        fees = f"frais {ter:.2f} %" if ter is not None else "frais : voir fiche émetteur"
        c = analyze_ticker(ticker, "etf", name, f"ETF · indice {index} · {fees} · éligibilité à confirmer")
        if not c:
            continue
        attach_news(c, f'ETF "{index}" (flux OR marché OR record)')
        attach_social(c, f'"{index}" ETF', f'"{index}" ETF -is:retweet', use_social)
        out.append(c)
    return out


# --------------------------------------------------------------------------
# COLLECTE : CRYPTO (CoinGecko)
# --------------------------------------------------------------------------

CG = "https://api.coingecko.com/api/v3"


def cg_get(path: str, **params):
    headers = dict(UA)
    if os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["COINGECKO_API_KEY"]
    for attempt in range(3):
        r = requests.get(CG + path, params=params, headers=headers, timeout=20)
        if r.status_code == 429:  # limite de requêtes : on patiente
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("CoinGecko : limite de requêtes atteinte")


def collect_crypto(use_social: bool) -> list[Candidate]:
    markets = cg_get("/coins/markets", vs_currency="eur", order="market_cap_desc",
                     per_page=100, page=1, price_change_percentage="24h,7d")
    markets = [m for m in markets if m["symbol"].lower() not in STABLES and m.get("market_cap")]

    trending_ids: list[str] = []
    try:
        trending_ids = [t["item"]["id"] for t in cg_get("/search/trending").get("coins", [])]
    except Exception as e:  # noqa: BLE001
        log(f"  ! trending: {e}")
    known = {m["id"] for m in markets}
    missing = [i for i in trending_ids if i not in known]
    if missing:
        time.sleep(2)
        try:
            markets += cg_get("/coins/markets", vs_currency="eur", ids=",".join(missing),
                              price_change_percentage="24h,7d")
        except Exception as e:  # noqa: BLE001
            log(f"  ! trending markets: {e}")

    for m in markets:  # proxy d'anomalie de volume : volume 24h / capitalisation
        m["_proxy"] = (m.get("total_volume") or 0) / m["market_cap"] if m.get("market_cap") else 0
    by_proxy = sorted(markets, key=lambda m: m["_proxy"], reverse=True)
    chosen: dict[str, dict] = {}
    for m in markets:
        if m["id"] in CORE_CRYPTOS or m["id"] in trending_ids:
            chosen[m["id"]] = m
    for m in by_proxy:
        if len(chosen) >= CRYPTO_DEEP_ANALYSIS + len(CORE_CRYPTOS):
            break
        chosen.setdefault(m["id"], m)

    out: list[Candidate] = []
    for cid, m in chosen.items():
        sym, name = m["symbol"].upper(), m["name"]
        log(f"Crypto {sym} ({name})")
        c = Candidate(kind="crypto", symbol=sym, name=name,
                      note="top 100" if m.get("market_cap_rank") else "",
                      price=m.get("current_price"), market_cap=m.get("market_cap"),
                      trending=cid in trending_ids)
        c.chg_1d = m.get("price_change_percentage_24h_in_currency") or m.get("price_change_percentage_24h")
        c.chg_7d = m.get("price_change_percentage_7d_in_currency")

        try:  # volume 24h vs moyenne 30 jours
            time.sleep(2.5)
            chart = cg_get(f"/coins/{cid}/market_chart", vs_currency="eur", days=30, interval="daily")
            vols = [v[1] for v in chart["total_volumes"]][:-1]
            avg = statistics.mean(vols[-30:]) if vols else 0
            if avg > 0 and m.get("total_volume"):
                c.vol_ratio = m["total_volume"] / avg
        except Exception as e:  # noqa: BLE001
            log(f"  ! historique volume {sym}: {e}")

        if c.market_cap and c.market_cap < 50_000_000:
            c.risks.append("petite capitalisation (<50 M€) : manipulation plus facile")
            c.risk_penalty += 4
        if m["_proxy"] > 1:
            c.risks.append("volume 24h supérieur à la capitalisation")
            c.risk_penalty += 3
        if c.chg_1d and c.chg_1d > 30:
            c.risks.append(f"hausse brutale sur 24h ({c.chg_1d:+.0f} %)")
            c.risk_penalty += 3

        attach_news(c, f'{name} crypto')
        attach_social(c, f'"{name}" crypto', f'"{name}" crypto -is:retweet', use_social)
        out.append(c)
    return out


# --------------------------------------------------------------------------
# SCORE /100
# --------------------------------------------------------------------------

def momentum_score(chg_7d: float, kind: str) -> float:
    """Récompense une hausse naissante, pas un titre déjà parti."""
    x = chg_7d / (2.5 if kind == "crypto" else 1.0)
    if x <= 0:
        return 0.0
    if x <= 10:
        return x / 10
    if x <= 25:
        return 1.0
    if x <= 40:
        return 1 - (x - 25) / 15 * 0.7
    return 0.3


def compute_score(c: Candidate) -> None:
    comps: dict[str, float] = {}
    if c.vol_ratio is not None:
        comps["volume"] = clip((c.vol_ratio - 1) / 3)                 # x1 -> 0, x4+ -> 1
        if c.vol_ratio >= 2:
            c.reasons.append(f"volume x{c.vol_ratio:.1f} vs moyenne 30 j")
    if c.mention_growth is not None:
        comps["mentions"] = clip((c.mention_growth - 1) / 2) * clip(c.mentions_24h / 5)
        if c.mention_growth >= 1.5 and c.mentions_24h >= 5:
            c.reasons.append(f"discussions x{c.mention_growth:.1f} ({c.mentions_24h} posts/24 h)")
    news = clip(c.news_24h / 5)
    if c.sentiment is not None:
        news *= 0.6 + 0.4 * (c.sentiment + 1) / 2
    if c.earnings_soon:
        news = clip(news + 0.25)
        c.reasons.append(f"résultats prévus le {c.earnings_soon}")
    comps["news"] = news
    if c.news_24h >= 3:
        c.reasons.append(f"{c.news_24h} articles en 24 h")
    if c.chg_7d is not None:
        comps["momentum"] = momentum_score(c.chg_7d, c.kind)
        limit = 60 if c.kind == "crypto" else 30
        if c.chg_7d > limit:
            c.risks.append(f"déjà très monté sur 7 j ({c.chg_7d:+.0f} %) : tendance probablement connue")
    if c.trending:
        c.reasons.append("dans les tendances CoinGecko")
        comps["news"] = clip(comps["news"] + 0.2)

    # Divergence sentiment / marché
    if c.sentiment is not None and c.chg_7d is not None:
        if c.sentiment > 0.3 and c.chg_7d < 0:
            c.reasons.append("sentiment positif mais cours en baisse (divergence)")
        elif c.sentiment < -0.3 and c.chg_7d > 5:
            c.reasons.append("cours en hausse malgré un sentiment négatif")

    wsum = sum(WEIGHTS[k] for k in comps)
    base = sum(WEIGHTS[k] * v for k, v in comps.items()) / wsum * 100 if wsum else 0
    c.components = comps
    c.score = round(clip(base - min(c.risk_penalty, MAX_RISK_PENALTY), 0, 100))


# --------------------------------------------------------------------------
# THÈMES / SECTEURS
# --------------------------------------------------------------------------

def collect_themes(use_social: bool) -> list[dict]:
    rows = []
    for label, query in THEMES.items():
        log(f"Thème {label}")
        res = news_activity(query)
        time.sleep(0.6)
        if not res:
            continue
        row = {"theme": label, "n24": res["n24"], "growth": res["growth"],
               "sentiment": res["sentiment"], "headline": res["headlines"][0] if res["headlines"] else ""}
        if use_social:
            rd = reddit_activity(query)
            time.sleep(1.0)
            if rd:
                row["reddit_growth"] = (rd[0] + 1) / (rd[1] + 1)
        rows.append(row)
    return sorted(rows, key=lambda r: r["growth"], reverse=True)


# --------------------------------------------------------------------------
# RAPPORT
# --------------------------------------------------------------------------

def fmt_pct(x: float | None) -> str:
    return "n/d" if x is None else f"{x:+.1f} %"


def table(cands: list[Candidate], top: int) -> list[str]:
    lines = ["| # | Valeur | Score | 1 j | 7 j | Volume | Détails |",
             "|---|--------|-------|-----|-----|--------|---------|"]
    for i, c in enumerate(sorted(cands, key=lambda c: c.score, reverse=True)[:top], 1):
        vr = f"x{c.vol_ratio:.1f}" if c.vol_ratio else "n/d"
        why = "; ".join(c.reasons) or "pas de signal marqué"
        lines.append(f"| {i} | **{c.name}** ({c.symbol}) | **{c.score}/100** | "
                     f"{fmt_pct(c.chg_1d)} | {fmt_pct(c.chg_7d)} | {vr} | {why} |")
    lines.append("")
    for c in sorted(cands, key=lambda c: c.score, reverse=True)[:top]:
        extra = []
        if c.note:
            extra.append(c.note)
        if c.risks:
            extra.append("⚠️ " + " · ".join(c.risks))
        if c.headlines:
            extra.append("📰 " + c.headlines[0])
        if extra:
            lines.append(f"- **{c.name}** : " + " | ".join(extra))
    lines.append("")
    return lines


def render_report(pea: list[Candidate], crypto: list[Candidate], themes: list[dict],
                  top: int, social: bool) -> str:
    today = dt.date.today().strftime("%d/%m/%Y")
    L = [f"# Radar des marchés - {today}", ""]
    if not social:
        L += ["_Signal réseaux sociaux désactivé : le score repose sur cours, volumes et actualités._", ""]
    elif not os.getenv("X_BEARER_TOKEN"):
        L += ["_X non connecté (pas de token) : discussions mesurées via Reddit uniquement._", ""]
    if pea:
        acts = [c for c in pea if c.kind == "action"]
        etfs = [c for c in pea if c.kind == "etf"]
        L += ["## Actions européennes (PEA à vérifier)", ""] + table(acts, top)
        L += ["## ETF (éligibilité PEA à confirmer)", ""] + table(etfs, top)
    if crypto:
        L += ["## Cryptomonnaies", ""] + table(crypto, top)
    if themes:
        L += ["## Thèmes qui accélèrent (actualité 24 h vs moyenne 6 j)", "",
              "| Thème | Articles 24 h | Accélération | Sentiment |", "|---|---|---|---|"]
        for t in themes:
            s = "n/d" if t["sentiment"] is None else f"{t['sentiment']:+.2f}"
            L.append(f"| {t['theme']} | {t['n24']} | x{t['growth']:.1f} | {s} |")
        L.append("")
    L += ["---",
          "_Outil de repérage, pas un conseil en investissement. Un score élevé signale une attention "
          "inhabituelle, pas un gain garanti ; les pics de buzz sont aussi le terrain des pumps "
          "coordonnés. Vérifie toujours l'éligibilité PEA et fais tes propres recherches._"]
    return "\n".join(L)


PAGE_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--up:#15803d;--down:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#141a22;--text:#e5e7eb;--muted:#9ca3af;--line:#243041;--up:#4ade80;--down:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:8px 0 2px}
.sub{color:var(--muted);font-size:.85rem}
nav{display:flex;gap:8px;overflow-x:auto;margin:14px 0 4px;padding-bottom:4px}
nav a{white-space:nowrap;text-decoration:none;color:var(--text);border:1px solid var(--line);background:var(--card);padding:6px 12px;border-radius:999px;font-size:.85rem}
h2{font-size:1.1rem;margin:26px 0 8px;scroll-margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.name{font-weight:600}
.sym{color:var(--muted);font-size:.8rem;font-weight:400}
.score{font-size:1.7rem;font-weight:700;line-height:1;text-align:right;white-space:nowrap}
.score small{font-size:.75rem;color:var(--muted);font-weight:500}
.bar{height:6px;background:var(--line);border-radius:99px;margin:10px 0;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:.75rem;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.chip.up{color:var(--up)}.chip.down{color:var(--down)}
.why{font-size:.88rem;margin:8px 0 0}
.risk{font-size:.82rem;color:var(--warn);margin:6px 0 0}
.news{font-size:.8rem;color:var(--muted);margin:6px 0 0}
.note{font-size:.75rem;color:var(--muted);margin:4px 0 0}
.empty{color:var(--muted);font-size:.9rem}
footer{color:var(--muted);font-size:.78rem;margin:28px 0 12px}
"""


def _score_color(score: float) -> str:
    return "#16a34a" if score >= 70 else "#d97706" if score >= 50 else "#6b7280"


def _chip(label: str, val: float | None) -> str:
    if val is None:
        return ""
    return f'<span class="chip {"up" if val >= 0 else "down"}">{label} {val:+.1f} %</span>'


def _card_html(c: Candidate) -> str:
    e = html.escape
    chips = _chip("1 j", c.chg_1d) + _chip("7 j", c.chg_7d)
    if c.vol_ratio:
        chips += f'<span class="chip">volume ×{c.vol_ratio:.1f}</span>'
    if c.mention_growth is not None and c.mentions_24h >= 5:
        chips += f'<span class="chip">discussions ×{c.mention_growth:.1f}</span>'
    if c.news_24h:
        chips += f'<span class="chip">{c.news_24h} articles/24 h</span>'
    out = [f'<div class="card"><div class="top"><div class="name">{e(c.name)} '
           f'<span class="sym">{e(c.symbol)}</span></div>'
           f'<div class="score" style="color:{_score_color(c.score)}">{c.score:.0f}<small>/100</small></div></div>',
           f'<div class="bar"><i style="width:{c.score:.0f}%;background:{_score_color(c.score)}"></i></div>',
           f'<div class="chips">{chips}</div>']
    out.append(f'<p class="why">{e(" · ".join(c.reasons)) if c.reasons else "Pas de signal marqué aujourd’hui."}</p>')
    if c.risks:
        out.append(f'<p class="risk">⚠️ {e(" · ".join(c.risks))}</p>')
    if c.headlines:
        out.append(f'<p class="news">📰 {e(c.headlines[0])}</p>')
    if c.note:
        out.append(f'<p class="note">{e(c.note)}</p>')
    out.append("</div>")
    return "".join(out)


def render_html(pea: list[Candidate], crypto: list[Candidate], themes: list[dict],
                top: int, social: bool) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y à %H:%M UTC")
    sections: list[tuple[str, str, str]] = []

    def block(anchor: str, title: str, items: list[Candidate]) -> None:
        ranked = sorted(items, key=lambda c: c.score, reverse=True)[:top]
        body = "".join(_card_html(c) for c in ranked) or '<p class="empty">Aucune donnée aujourd’hui.</p>'
        sections.append((anchor, title, body))

    if pea:
        block("actions", "Actions européennes (PEA à vérifier)", [c for c in pea if c.kind == "action"])
        block("etf", "ETF (éligibilité PEA à confirmer)", [c for c in pea if c.kind == "etf"])
    if crypto:
        block("crypto", "Cryptomonnaies", crypto)
    if themes:
        rows = []
        for t in themes:
            width = clip((t["growth"] - 1) / 3) * 100
            sent = "" if t["sentiment"] is None else f' · sentiment {t["sentiment"]:+.2f}'
            rows.append(
                f'<div class="card"><div class="top"><div class="name">{html.escape(t["theme"])}</div>'
                f'<div class="score" style="color:{_score_color(40 + width * 0.5)}">×{t["growth"]:.1f}</div></div>'
                f'<div class="bar"><i style="width:{width:.0f}%;background:{_score_color(40 + width * 0.5)}"></i></div>'
                f'<p class="note">{t["n24"]} articles en 24 h{sent}</p></div>')
        sections.append(("themes", "Thèmes qui accélèrent", "".join(rows)))

    nav = "".join(f'<a href="#{a}">{html.escape(t.split(" (")[0])}</a>' for a, t, _ in sections)
    body = "".join(f'<h2 id="{a}">{html.escape(t)}</h2>{b}' for a, t, b in sections)
    if not social:
        src = "Signal réseaux sociaux désactivé."
    elif not os.getenv("X_BEARER_TOKEN"):
        src = "Discussions mesurées via Reddit uniquement (X non connecté)."
    else:
        src = "Discussions mesurées via Reddit et X."
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="Radar">'
        '<meta name="color-scheme" content="light dark">'
        '<title>Radar des marchés</title>'
        f'<style>{PAGE_CSS}</style></head><body><main>'
        f'<h1>📡 Radar des marchés</h1><div class="sub">Mis à jour le {now}</div>'
        f'<nav>{nav}</nav>{body}'
        f'<footer>{html.escape(src)} Outil de repérage, pas un conseil en investissement : '
        'un score élevé signale une attention inhabituelle, pas un gain garanti, et les pics de buzz '
        'sont aussi le terrain des pumps coordonnés. Vérifie toujours l’éligibilité PEA.</footer>'
        '</main></body></html>'
    )


def build_summary(pea: list[Candidate], crypto: list[Candidate], page_url: str) -> str:
    def top3(items: list[Candidate]) -> list[str]:
        return [f"• {c.name} : {c.score:.0f}/100" for c in
                sorted(items, key=lambda c: c.score, reverse=True)[:3]]
    L = [f"📡 Radar des marchés - {dt.date.today().strftime('%d/%m/%Y')}", ""]
    for title, items in (("Actions PEA (à vérifier)", [c for c in pea if c.kind == "action"]),
                         ("ETF", [c for c in pea if c.kind == "etf"]),
                         ("Crypto", crypto)):
        if items:
            L += [title] + top3(items) + [""]
    L.append(f"Détails : {page_url}")
    L.append("Outil de repérage, pas un conseil en investissement.")
    return "\n".join(L)


def send_telegram(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram : TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants.")
        return
    for i in range(0, len(text), 4000):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[i:i + 4000]}, timeout=15)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def load_watchlist() -> None:
    path = Path(__file__).parent / "watchlist.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    EQUITIES.clear(); EQUITIES.update(data.get("equities", {}))
    ETFS.clear(); ETFS.update({k: tuple(v) for k, v in data.get("etfs", {}).items()})
    log(f"watchlist.json chargée ({len(EQUITIES)} actions, {len(ETFS)} ETF)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Radar des marchés PEA / ETF / crypto")
    ap.add_argument("--top", type=int, default=8, help="nombre de lignes par tableau")
    ap.add_argument("--no-social", action="store_true", help="désactive Reddit / X")
    ap.add_argument("--only", choices=["pea", "crypto", "themes"], help="limite à une section")
    ap.add_argument("--send", action="store_true", help="envoie le rapport sur Telegram")
    args = ap.parse_args()

    load_watchlist()
    social = not args.no_social
    pea: list[Candidate] = []
    crypto: list[Candidate] = []
    themes: list[dict] = []

    if args.only in (None, "pea"):
        pea = collect_pea(social)
    if args.only in (None, "crypto"):
        try:
            crypto = collect_crypto(social)
        except Exception as e:  # noqa: BLE001
            log(f"Crypto indisponible : {e}")
    if args.only in (None, "themes"):
        themes = collect_themes(social)

    for c in pea + crypto:
        compute_score(c)

    report = render_report(pea, crypto, themes, args.top, social)
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"radar_{dt.date.today().isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    log(f"\nRapport enregistré : {path}")

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(render_html(pea, crypto, themes, args.top, social),
                                         encoding="utf-8")
    log(f"Page web générée : {SITE_DIR / 'index.html'}")

    if args.send:
        repo = os.getenv("GITHUB_REPOSITORY", "")   # "pseudo/radar-bot" dans GitHub Actions
        if "/" in repo:
            owner, name = repo.split("/", 1)
            send_telegram(build_summary(pea, crypto, f"https://{owner}.github.io/{name}/"))
        else:
            send_telegram(report)


if __name__ == "__main__":
    main()
